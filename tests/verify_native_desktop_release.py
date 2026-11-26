"""Validate the packaged native desktop window with isolated local test data.

This intentionally rejects the former Chrome/Edge app-mode launcher. It opens
only the supplied release executable and sends model requests only to its own
loopback synthetic provider. No WebView remote debugging is enabled.
"""
import argparse
import ctypes
from ctypes import wintypes as W
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

import httpx

parser = argparse.ArgumentParser(description=__doc__)
root = Path(__file__).resolve().parents[1]
parser.add_argument('--binary', type=Path, default=root / 'dist' / 'GrokDesk' / 'GrokDesk.exe')
parser.add_argument('--expected-version', default='1.5.1')
args = parser.parse_args()
binary = args.binary.resolve()
assert binary.is_file(), f'Packaged executable is missing: {binary}'
output = root / 'test-output'
output.mkdir(exist_ok=True)
folder = Path(tempfile.mkdtemp(prefix='native-desktop-release-', dir=output))
user = ctypes.WinDLL('user32', use_last_error=True)
callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
user.EnumWindows.argtypes, user.EnumWindows.restype = [callback_type, W.LPARAM], W.BOOL
user.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
user.GetWindowThreadProcessId.restype = W.DWORD
user.GetWindowTextW.argtypes, user.GetWindowTextW.restype = [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int
user.GetClassNameW.argtypes, user.GetClassNameW.restype = [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int
user.IsWindowVisible.argtypes, user.IsWindowVisible.restype = [W.HWND], W.BOOL
user.IsIconic.argtypes, user.IsIconic.restype = [W.HWND], W.BOOL
user.GetWindowRect.argtypes, user.GetWindowRect.restype = [W.HWND, ctypes.POINTER(W.RECT)], W.BOOL
user.ShowWindow.argtypes, user.ShowWindow.restype = [W.HWND, ctypes.c_int], W.BOOL
user.PostMessageW.argtypes, user.PostMessageW.restype = [W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL
processes, clients = [], []


class SyntheticProvider(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *unused):
        pass

    def do_GET(self):
        data = json.dumps({'data': [{'id': 'native-desktop-synthetic'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        assert self.path.rstrip('/') == '/v1/chat/completions', self.path
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert payload['model'] == 'native-desktop-synthetic'
        type(self).calls += 1
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        chunk = {'choices': [{'delta': {'content': '原生桌面隔离测试回复。'}}]}
        self.wfile.write(('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\ndata: [DONE]\n\n').encode())
        self.wfile.flush()
        self.close_connection = True


def log_text():
    try:
        return (folder / 'startup.log').read_text('utf-8', errors='replace')
    except OSError:
        return ''


def owned_windows(process):
    found = []

    @callback_type
    def visit(hwnd, unused):
        pid = W.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process.pid and user.IsWindowVisible(hwnd):
            title, window_class, rect = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(256), W.RECT()
            user.GetWindowTextW(hwnd, title, len(title))
            user.GetClassNameW(hwnd, window_class, len(window_class))
            user.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append({'hwnd': int(hwnd), 'pid': pid.value, 'title': title.value,
                          'class': window_class.value, 'width': rect.right - rect.left,
                          'height': rect.bottom - rect.top})
        return True

    assert user.EnumWindows(visit, 0), 'Cannot enumerate desktop windows.'
    return found


def native_window(process, timeout=35):
    deadline = time.monotonic() + timeout
    found = []
    while time.monotonic() < deadline:
        assert process.poll() is None, f'Native EXE exited early ({process.returncode}): {log_text()[-3000:]}'
        found = owned_windows(process)
        candidates = [item for item in found if 'grok' in item['title'].lower()
                      and not item['class'].startswith('Chrome_WidgetWin')
                      and item['class'] != '#32770' and item['width'] >= 500 and item['height'] >= 300]
        if candidates:
            assert len(candidates) == 1, f'Expected one native main window: {candidates!r}'
            return candidates[0]
        time.sleep(.1)
    raise AssertionError(f'No visible native main window belongs to EXE PID {process.pid}; own windows={found!r}; log={log_text()[-3000:]}')


def launch():
    before = len(log_text())
    process = subprocess.Popen([str(binary), '--port', '0', '--data-dir', str(folder)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    processes.append(process)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        assert process.poll() is None, f'Packaged EXE exited during startup ({process.returncode}): {log_text()[-3000:]}'
        found = re.search(r'service ready at (http://127\.0\.0\.1:\d+)', log_text()[before:])
        if found:
            client = httpx.Client(base_url=found[1], trust_env=False, timeout=8)
            clients.append(client)
            html = client.get('/').raise_for_status().text
            token = re.search(r'name="session-token" content="([^"]+)"', html)
            assert token, 'Local UI did not include its session token.'
            client.headers['X-GrokDesk-Token'] = token[1]
            bootstrap = client.get('/api/bootstrap').raise_for_status().json()
            assert bootstrap['version'] == args.expected_version, bootstrap
            assert Path(bootstrap['data_dir']).resolve() == folder.resolve(), 'EXE used non-test data.'
            return process, client, native_window(process)
        time.sleep(.1)
    raise AssertionError(f'Native EXE did not start: {log_text()[-3000:]}')


def duplicate_restores(process, client, window, minimize=False):
    hwnd = window['hwnd']
    if minimize:
        user.ShowWindow(hwnd, 6)  # SW_MINIMIZE, only the isolated EXE's own HWND.
        deadline = time.monotonic() + 5
        while not user.IsIconic(hwnd) and time.monotonic() < deadline:
            time.sleep(.05)
        assert user.IsIconic(hwnd), 'Native window could not be minimized.'
    previous_log = log_text()
    ready_count = previous_log.count('service ready at ')
    activation_count = previous_log.count('Native desktop window activated')
    duplicate = subprocess.Popen([str(binary), '--port', '0', '--data-dir', str(folder)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    processes.append(duplicate)
    assert duplicate.wait(timeout=15) == 0, 'Duplicate launch did not exit cleanly.'
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert process.poll() is None, 'Primary EXE exited before consuming duplicate activation.'
        if log_text().count('Native desktop window activated') > activation_count:
            break
        time.sleep(.1)
    else:
        raise AssertionError(f'Primary native window did not acknowledge duplicate activation: {log_text()[-3000:]}')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if user.IsWindowVisible(hwnd) and not user.IsIconic(hwnd):
            break
        time.sleep(.1)
    assert process.poll() is None, 'Primary EXE exited after duplicate launch.'
    assert user.IsWindowVisible(hwnd) and not user.IsIconic(hwnd), 'Duplicate launch failed to restore the native window.'
    assert native_window(process, timeout=2)['hwnd'] == hwnd, 'Duplicate launch created a different main window.'
    assert log_text().count('service ready at ') == ready_count, 'Duplicate launch started another service.'
    assert client.get('/api/bootstrap').raise_for_status().json()['version'] == args.expected_version


def close_and_verify_exit(process, client, window):
    assert any(item['hwnd'] == window['hwnd'] for item in owned_windows(process)), 'Close target is no longer owned by the isolated EXE.'
    assert user.PostMessageW(window['hwnd'], 0x0010, 0, 0), 'Cannot send WM_CLOSE to native window.'
    assert process.wait(timeout=25) == 0, 'Closing native window did not cleanly exit the EXE.'
    assert not owned_windows(process), 'Native window remained after EXE exit.'
    with httpx.Client(trust_env=False, timeout=.7) as probe:
        try:
            probe.get(str(client.base_url))
        except httpx.TransportError:
            pass
        else:
            raise AssertionError('Closing native window left its HTTP service running.')


provider = ThreadingHTTPServer(('127.0.0.1', 0), SyntheticProvider)
threading.Thread(target=provider.serve_forever, daemon=True).start()
try:
    process, client, first_window = launch()
    settings = {'base_url': f'http://127.0.0.1:{provider.server_port}', 'api_key': 'synthetic-native-desktop-key-only', 'model': 'native-desktop-synthetic'}
    client.post('/api/settings', json=settings).raise_for_status()
    assert client.get('/api/models').raise_for_status().json()['models'] == ['native-desktop-synthetic']
    conversation = client.post('/api/conversations', json={}).raise_for_status().json()
    payload = {'conversation_id': conversation['id'], 'model': 'native-desktop-synthetic', 'text': 'Isolated native desktop test', 'attachments': []}
    response = client.post('/api/chat', json=payload).raise_for_status()
    assert 'event: done' in response.text and SyntheticProvider.calls == 1, response.text
    messages = client.get('/api/conversations/' + conversation['id']).raise_for_status().json()['messages']
    assert messages[-1]['content'] == '原生桌面隔离测试回复。' and messages[-1]['status'] == 'complete'
    duplicate_restores(process, client, first_window)
    duplicate_restores(process, client, first_window, minimize=True)
    close_and_verify_exit(process, client, first_window)
    client.close()
    process, client, restarted_window = launch()
    restored = client.get('/api/conversations/' + conversation['id']).raise_for_status().json()
    assert restored['id'] == conversation['id'] and restored['messages'] == messages, 'Native restart lost isolated history.'
    close_and_verify_exit(process, client, restarted_window)
    print(json.dumps({'passed': True, 'version': args.expected_version, 'binary': str(binary),
                      'native_window': first_window, 'test_data': str(folder), 'checks': [
                          'visible native main HWND belongs to packaged EXE PID',
                          'Chrome app-mode window is rejected', 'isolated loopback synthetic model roundtrip',
                          'duplicate launch restores existing native window and reuses one service',
                          'minimized window restores on duplicate launch', 'WM_CLOSE stops native EXE and HTTP service',
                          'native restart preserves SQLite conversation history']}, ensure_ascii=False, indent=2))
finally:
    for client in clients:
        client.close()
    for process in processes:
        if process.poll() is None:
            for window in owned_windows(process):
                user.PostMessageW(window['hwnd'], 0x0010, 0, 0)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=8)
    provider.shutdown()
    provider.server_close()
