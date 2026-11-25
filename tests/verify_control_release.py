"""Check packaged control modes with local screenshots and wait-only actions.

The isolated EXE captures the real desktop, but sends images only to this
script's 127.0.0.1 provider. No screenshots are logged or saved. The provider
can return only wait/done; no clicks, typing, or key input are requested.
"""
import json
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


PORT = 18796
MODEL = 'synthetic-packaged-control-vision'
SYNTHETIC_KEY = 'synthetic-packaged-control-key-never-valid'


class LocalProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            assert self.path == '/v1/chat/completions'
            assert self.headers.get('Authorization') == 'Bearer ' + SYNTHETIC_KEY
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert payload['stream'] is False and payload['model'] == MODEL
            content = payload['messages'][-1]['content']
            images = [part for part in content if part.get('type') == 'image_url']
            assert len(images) == 1
            assert images[0]['image_url']['url'].startswith('data:image/jpeg;base64,')
            text = next(part['text'] for part in content if part.get('type') == 'text')
            history = json.loads(text.split('最近已执行操作：', 1)[1].split('\n最新截图：', 1)[0])
            assert isinstance(history, list)
            assert all(action.get('type') == 'wait' for action in history)
            with self.server.counter_lock:
                self.server.requests += 1
            action = ({'type': 'done', 'summary': 'Synthetic release control complete'} if history
                      else {'type': 'wait', 'seconds': 0.1, 'reason': 'Synthetic wait only; no desktop input'})
            status = 200
            result = {'choices': [{'message': {'content': json.dumps(action)}}]}
        except Exception as error:
            with self.server.counter_lock:
                self.server.errors.append(type(error).__name__)
            status, result = 400, {'error': {'message': 'Synthetic provider validation failed'}}
        raw = json.dumps(result).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (OSError, ValueError):
            pass


class EventRun:
    def __init__(self, base_url, headers, mode):
        self.base_url, self.headers, self.mode = base_url, dict(headers), mode
        self.run_id = None
        self.events = queue.Queue()
        self.thread = threading.Thread(target=self.consume, daemon=True)

    def consume(self):
        try:
            with httpx.Client(base_url=self.base_url, headers=self.headers, timeout=25, trust_env=False) as client:
                with client.stream('POST', '/api/control', json={
                    'task': 'Synthetic release check: wait once, then finish.',
                    'model': MODEL, 'mode': self.mode, 'max_steps': 3,
                }) as response:
                    if response.status_code != 200:
                        raise AssertionError('Control start returned HTTP ' + str(response.status_code))
                    event = ''
                    for line in response.iter_lines():
                        if line.startswith('event: '):
                            event = line[7:]
                        elif line.startswith('data: '):
                            data = json.loads(line[6:])
                            if event == 'screen':
                                assert data['url'].startswith('data:image/jpeg;base64,')
                                assert data['width'] > 0 and data['height'] > 0
                                # Keep only dimensions in the queue; never persist pixels.
                                data = {'width': data['width'], 'height': data['height']}
                            self.events.put((event, data))
        except Exception as error:
            self.events.put(('failure', str(error)))

    def start(self):
        self.thread.start()
        data = self.wait('start')
        self.run_id = data['run_id']
        assert data['mode'] == self.mode and data['max_steps'] == 3
        return self

    def wait(self, expected, forbidden=()):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                event, data = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            assert event != 'failure', data
            assert event not in forbidden, 'Unexpected event: ' + event
            if event == expected:
                return data
            assert event != 'done', 'Task finished before ' + expected + ': ' + str(data)
        raise AssertionError('Timed out waiting for control event: ' + expected)

    def assert_waiting_for_approval(self):
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            try:
                event, data = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return
            assert event not in ('failure', 'executed', 'done'), (event, data)

    def finish(self):
        self.thread.join(5)
        assert not self.thread.is_alive(), 'Control response did not close'
        while not self.events.empty():
            event, data = self.events.get_nowait()
            assert event != 'failure', data


def require_wait(action, approval):
    assert action['action']['type'] == 'wait'
    assert action['action']['seconds'] == 0.1
    assert action['requires_approval'] is approval
    assert action['step'] == 1


def main():
    root = Path(__file__).resolve().parents[1]
    binary = root / 'dist' / 'GrokDesk' / 'GrokDesk.exe'
    assert binary.is_file(), 'Packaged GrokDesk.exe is missing'
    # Refuse to attach to a service belonging to another session.
    with socket.socket() as probe:
        if os.name == 'nt':
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(('127.0.0.1', PORT))
    output = root / 'test-output'
    output.mkdir(exist_ok=True)
    provider = ThreadingHTTPServer(('127.0.0.1', 0), LocalProvider)
    provider.requests, provider.errors, provider.counter_lock = 0, [], threading.Lock()
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    process, client, owned_server = None, None, False
    runs = []
    temporary = tempfile.TemporaryDirectory(prefix='control-release-', dir=output)
    folder = Path(temporary.name)
    try:
        environment = {key: value for key, value in os.environ.items()
                       if key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
        environment['NO_PROXY'] = '127.0.0.1,localhost'
        process = subprocess.Popen([str(binary), '--no-window', '--port', str(PORT), '--data-dir', str(folder)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        base_url = f'http://127.0.0.1:{PORT}'
        client = httpx.Client(base_url=base_url, timeout=5, trust_env=False)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.poll() is None, 'Packaged service exited during startup'
            try:
                html = client.get('/').raise_for_status().text
                found = re.search(r'<meta name="session-token" content="([^"]+)"', html)
                if found:
                    client.headers['X-GrokDesk-Token'] = found[1]
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError('Packaged service did not start')
        bootstrap = client.get('/api/bootstrap').raise_for_status().json()
        assert Path(bootstrap['data_dir']).resolve() == folder.resolve(), 'Unexpected service data directory'
        owned_server = True
        assert bootstrap['version'] == '1.5.0', bootstrap['version']
        modes = re.search(r'<select\b[^>]*\bid="controlMode"[^>]*>(.*?)</select>', html, re.S)
        assert modes, 'Packaged control mode selector is missing'
        options = re.findall(r'<option\b[^>]*value="([^"]+)"[^>]*>\s*([^<]+?)\s*</option>', modes[1])
        assert options == [('confirm', '需要确认'), ('auto', '完全权限')], options
        client.post('/api/settings', json={
            'base_url': f'http://127.0.0.1:{provider.server_port}', 'api_key': SYNTHETIC_KEY, 'model': MODEL,
        }).raise_for_status()

        automatic = EventRun(base_url, client.headers, 'auto')
        runs.append(automatic)
        automatic.start()
        require_wait(automatic.wait('action'), False)
        assert automatic.wait('executed')['step'] == 1
        done = automatic.wait('done')
        assert done['status'] == 'complete' and done['steps'] == 1 and not done['error'], done
        automatic.finish()

        confirmed = EventRun(base_url, client.headers, 'confirm')
        runs.append(confirmed)
        confirmed.start()
        action = confirmed.wait('action')
        require_wait(action, True)
        confirmed.assert_waiting_for_approval()
        client.post(f'/api/control/{confirmed.run_id}/approve', json={
            'action_id': action['action_id'], 'approved': True,
        }).raise_for_status()
        assert confirmed.wait('executed')['step'] == 1
        done = confirmed.wait('done')
        assert done['status'] == 'complete' and done['steps'] == 1 and not done['error'], done
        confirmed.finish()

        stopped = EventRun(base_url, client.headers, 'confirm')
        runs.append(stopped)
        stopped.start()
        require_wait(stopped.wait('action'), True)
        stopped.assert_waiting_for_approval()
        client.post(f'/api/control/{stopped.run_id}/stop', json={}).raise_for_status()
        done = stopped.wait('done', forbidden=('executed',))
        assert done['status'] == 'stopped' and done['steps'] == 0 and not done['error'], done
        stopped.finish()
        assert provider.errors == [], provider.errors
        assert provider.requests == 5, provider.requests
        print(json.dumps({'passed': True, 'version': bootstrap['version'], 'local_provider_requests': provider.requests,
                          'checks': ['packaged EXE startup with isolated data', 'exactly two permission modes',
                                     'real screen capture sent only to localhost', 'auto wait without approval',
                                     'confirmation blocks until approved', 'stop during approval executes zero steps',
                                     'no clicks, typing, key input, or screenshot files']}, ensure_ascii=False, indent=2))
    finally:
        if client and owned_server and process and process.poll() is None:
            for run in runs:
                if run.run_id and run.thread.is_alive():
                    try:
                        client.post(f'/api/control/{run.run_id}/stop', json={})
                    except httpx.HTTPError:
                        pass
            try:
                client.post('/api/exit', json={}).raise_for_status()
            except httpx.HTTPError:
                pass
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for run in runs:
            run.thread.join(3)
        if client:
            client.close()
        provider.shutdown()
        provider.server_close()
        temporary.cleanup()


if __name__ == '__main__':
    main()
