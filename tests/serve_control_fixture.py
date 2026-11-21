"""Local UI fixture with synthetic desktop actions and screenshots only."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_server
from test_app import SyntheticProvider
from test_control import FakeDesktop


class Provider(SyntheticProvider):
    def do_POST(self):
        import io
        length = int(self.headers['Content-Length'])
        raw = self.rfile.read(length)
        payload = json.loads(raw)
        if not payload.get('messages', [{}])[0].get('content', '').startswith('你是用户电脑的操作助手'):
            self.rfile = io.BytesIO(raw)
            return super().do_POST()
        text = payload['messages'][-1]['content'][0]['text']
        history = json.loads(text.split('最近已执行操作：', 1)[1].split('\n最新截图', 1)[0])
        if not history:
            action = {'type': 'click', 'x': 20, 'y': 30, 'reason': 'Synthetic click, no actual input'}
        elif '[auto]' in text and len(history) == 1:
            action = {'type': 'type', 'text': '合成测试输入', 'reason': 'Synthetic typing, no actual input'}
        else:
            action = {'type': 'done', 'summary': '合成电脑任务完成'}
        raw = json.dumps({'choices': [{'message': {'content': json.dumps(action)}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


root = Path(__file__).resolve().parents[1]
folder = root / 'test-output'
folder.mkdir(exist_ok=True)
directory = tempfile.mkdtemp(prefix='control-ui-', dir=folder)
provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
threading.Thread(target=provider.serve_forever, daemon=True).start()
server = create_server(directory, 8766)
server.app.controller = FakeDesktop()
server.app.settings.update({'base_url': f'http://127.0.0.1:{provider.server_port}', 'api_key': 'synthetic-ui-only', 'model': 'grok-desk-synthetic'})
print('Synthetic computer control UI fixture: http://127.0.0.1:8766', flush=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    server.app.shutdown_requested.wait()
finally:
    server.shutdown()
    server.server_close()
    provider.shutdown()
    provider.server_close()
