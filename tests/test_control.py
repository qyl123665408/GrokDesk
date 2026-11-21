"""Control integration tests using synthetic screenshots and a local model server."""
import base64
import io
import json
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from PIL import Image
from app import create_server
from computer_control import Screen


class ControlProvider(BaseHTTPRequestHandler):
    actions = deque()
    payloads = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.payloads.append(payload)
        action = self.actions.popleft() if self.actions else {'type': 'done', 'summary': 'Synthetic task complete'}
        raw = json.dumps({'choices': [{'message': {'content': json.dumps(action)}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeDesktop:
    def __init__(self):
        png = io.BytesIO()
        Image.new('RGB', (320, 200), 'white').save(png, format='PNG')
        self.screen = Screen('data:image/png;base64,' + base64.b64encode(png.getvalue()).decode(), 320, 200, 0, 0, 320, 200, 0)
        self.actions = []

    def capture(self):
        return self.screen

    def execute(self, action, screen, cancel):
        if not cancel.is_set():
            self.actions.append(dict(action))
            return True
        return False


class ControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = ThreadingHTTPServer(('127.0.0.1', 0), ControlProvider)
        threading.Thread(target=cls.provider.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()
        cls.provider.server_close()

    def setUp(self):
        ControlProvider.actions = deque()
        ControlProvider.payloads = []
        self.temporary = tempfile.TemporaryDirectory()
        self.server = create_server(self.temporary.name)
        self.desktop = FakeDesktop()
        self.server.app.controller = self.desktop
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.headers = {'X-GrokDesk-Token': self.server.app.token}
        self.client = httpx.Client(base_url=self.url, headers=self.headers, timeout=12, trust_env=False)
        self.client.post('/api/settings', json={'base_url': f'http://127.0.0.1:{self.provider.server_port}', 'api_key': 'synthetic-control-key', 'model': 'synthetic-vision'}).raise_for_status()
        self.threads = []
        self.events = queue.Queue()

    def tearDown(self):
        self.server.app.shutdown_requested.set()
        for run in list(self.server.app.control_runs.values()):
            run.stop()
        for thread in self.threads:
            thread.join(4)
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def start(self, mode='confirm', max_steps=5):
        def consume():
            try:
                with httpx.Client(base_url=self.url, headers=self.headers, timeout=12, trust_env=False) as client:
                    with client.stream('POST', '/api/control', json={'task': 'Synthetic local task', 'model': 'synthetic-vision', 'mode': mode, 'max_steps': max_steps}) as response:
                        if response.status_code != 200:
                            self.events.put(('failure', (response.status_code, response.read().decode())))
                            return
                        event = ''
                        for line in response.iter_lines():
                            if line.startswith('event: '):
                                event = line[7:]
                            elif line.startswith('data: '):
                                self.events.put((event, json.loads(line[6:])))
            except Exception as error:
                self.events.put(('failure', repr(error)))
        thread = threading.Thread(target=consume, daemon=True)
        self.threads.append(thread)
        thread.start()
        return self.wait('start')['run_id']

    def wait(self, expected):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            event, data = self.events.get(timeout=max(.01, deadline-time.monotonic()))
            self.assertNotEqual(event, 'failure', data)
            if event == expected:
                return data
            if event == 'done' and expected != 'done':
                self.fail(f'Task finished before {expected}: {data}')
        self.fail('Expected control event: ' + expected)

    def approve(self, run, action, approved=True):
        return self.client.post(f'/api/control/{run}/approve', json={'action_id': action['action_id'], 'approved': approved})

    def test_confirmation_blocks_input_and_approval_cannot_replay(self):
        ControlProvider.actions.extend([{'type': 'click', 'x': 20, 'y': 30}, {'type': 'done', 'summary': 'complete'}])
        run = self.start()
        action = self.wait('action')
        self.assertTrue(action['requires_approval'])
        self.assertEqual(self.desktop.actions, [])
        wrong = self.client.post(f'/api/control/{run}/approve', json={'action_id': '0'*32, 'approved': True})
        self.assertIn(wrong.status_code, (400, 404, 409))
        self.assertEqual(self.desktop.actions, [])
        self.approve(run, action).raise_for_status()
        self.assertIn(self.approve(run, action).status_code, (400, 404, 409))
        done = self.wait('done')
        self.assertFalse(done.get('error'), done)
        self.assertEqual(len(self.desktop.actions), 1)
        messages = ControlProvider.payloads[0]['messages']
        self.assertTrue(any(isinstance(m.get('content'), list) and any(p.get('type') == 'image_url' for p in m['content']) for m in messages))

    def test_reject_stops_without_input(self):
        ControlProvider.actions.append({'type': 'type', 'text': 'never enter'})
        run = self.start()
        action = self.wait('action')
        self.approve(run, action, False).raise_for_status()
        self.wait('done')
        self.assertEqual(self.desktop.actions, [])

    def test_auto_executes_without_confirmation(self):
        ControlProvider.actions.extend([{'type': 'click', 'x': 20, 'y': 30}, {'type': 'type', 'text': '测试文本'}, {'type': 'done', 'summary': 'complete'}])
        self.start('auto')
        action = self.wait('action')
        self.assertFalse(action['requires_approval'])
        done = self.wait('done')
        self.assertFalse(done.get('error'), done)
        self.assertEqual([a['type'] for a in self.desktop.actions], ['click', 'type'])

    def test_stop_waiting_for_approval_and_prevent_concurrent_task(self):
        ControlProvider.actions.append({'type': 'click', 'x': 20, 'y': 30})
        run = self.start()
        self.wait('action')
        conflict = self.client.post('/api/control', json={'task': 'second', 'model': 'synthetic-vision', 'mode': 'auto', 'max_steps': 2})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.client.post(f'/api/control/{run}/stop', json={}).raise_for_status()
        self.wait('done')
        self.assertEqual(self.desktop.actions, [])

    def test_invalid_model_action_never_executes(self):
        ControlProvider.actions.append({'type': 'click', 'x': 9999, 'y': 30})
        self.start('auto')
        done = self.wait('done')
        self.assertTrue(done.get('error'), done)
        self.assertEqual(self.desktop.actions, [])

    def test_mode_validation_and_session_token(self):
        with httpx.Client(base_url=self.url, timeout=5, trust_env=False) as client:
            self.assertEqual(client.post('/api/control', json={'task': 'blocked'}).status_code, 403)
        response = self.client.post('/api/control', json={'task': 'blocked', 'mode': 'unknown'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.desktop.actions, [])

    def test_step_limit_stops_continuous_actions(self):
        ControlProvider.actions.extend([{'type': 'click', 'x': 20, 'y': 30}] * 4)
        self.start('auto', max_steps=1)
        done = self.wait('done')
        self.assertEqual(len(self.desktop.actions), 1)
        self.assertEqual(done['steps'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
