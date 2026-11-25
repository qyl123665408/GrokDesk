"""Exercise the PyInstaller executable and its bundled local web UI end to end."""
import base64
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--binary', type=Path, default=root / 'dist' / 'GrokDesk' / 'GrokDesk.exe')
binary = parser.parse_args().binary.resolve()
assert binary.is_file() and binary.stat().st_size > 1_000_000, 'portable application was not packaged'


class FakeProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.reply({'data': [{'id': 'grok-desk-release-smoke'}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        try:
            chunks = [{'choices': [{'delta': {'content': '合成桌面测试通过。'}}]}, {'choices': [], 'usage': {'total_tokens': 12}}]
            for chunk in chunks:
                self.wfile.write(('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n').encode('utf-8'))
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (OSError, ValueError):
            pass

    def reply(self, data):
        raw = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


provider = ThreadingHTTPServer(('127.0.0.1', 0), FakeProvider)
threading.Thread(target=provider.serve_forever, daemon=True).start()
directory = Path(tempfile.mkdtemp(prefix='release-smoke-', dir=root / 'test-output'))
process = subprocess.Popen([str(binary), '--no-window', '--port', '18795', '--data-dir', str(directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
client = httpx.Client(base_url='http://127.0.0.1:18795', timeout=10, trust_env=False)
try:
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError('Standalone executable exited unexpectedly.')
        try:
            token = client.get('/').text.partition('<meta name="session-token" content="')[2].split('"', 1)[0]
            if token:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:
        raise RuntimeError('Standalone executable did not start its local server.')
    client.headers['X-GrokDesk-Token'] = token
    html = client.get('/').raise_for_status().text
    assert '__SESSION_TOKEN__' not in html
    assert '<title>Grok Desk</title>' in html
    assert 'DOMPurify' in client.get('/vendor/purify.min.js').text
    assert 'marked' in client.get('/vendor/marked.umd.js').text
    base = f'http://127.0.0.1:{provider.server_port}'
    saved = client.post('/api/settings', json={'base_url': base, 'api_key': 'synthetic-release-key-only', 'model': 'grok-desk-release-smoke'}).raise_for_status().json()
    assert saved['has_api_key']
    assert 'api_key' not in saved and 'encrypted_key' not in saved
    assert client.get('/api/models').raise_for_status().json()['models'] == ['grok-desk-release-smoke']
    conversation = client.post('/api/conversations', json={}).raise_for_status().json()
    response = client.post('/api/chat', json={'conversation_id': conversation['id'], 'model': 'grok-desk-release-smoke', 'text': 'release smoke', 'attachments': []}).raise_for_status()
    assert '合成桌面测试通过。' in response.text and 'event: done' in response.text, response.text
    conversation = client.get('/api/conversations/' + conversation['id']).raise_for_status().json()
    assert conversation['messages'][-1]['content'] == '合成桌面测试通过。'
    assert conversation['messages'][-1]['status'] == 'complete'
    settings_file = (directory / 'settings.json').read_text('utf-8')
    assert 'synthetic-release-key-only' not in settings_file
    assert (directory / 'history.sqlite3').is_file()
    print(json.dumps({'passed': True, 'checks': ['portable packaged executable startup', 'bundled offline assets', 'session token and authorized local API', 'Windows DPAPI key save', 'synthetic model discovery', 'SSE message roundtrip', 'local SQLite history', 'secret absent from settings']}, ensure_ascii=False, indent=2))
finally:
    client.close()
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    provider.shutdown()
    provider.server_close()
