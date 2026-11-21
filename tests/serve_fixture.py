"""Local UI fixture. Never points at a real provider or uses a real API key."""
import json
import argparse
import os
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_server
import httpx
from test_app import SyntheticProvider
from http.server import ThreadingHTTPServer
from PIL import Image, ImageDraw

folder = Path(__file__).resolve().parents[1] / 'test-output'
folder.mkdir(exist_ok=True)
parser = argparse.ArgumentParser()
parser.add_argument('--release', action='store_true')
args = parser.parse_args()
provider = ThreadingHTTPServer(('127.0.0.1', 0), SyntheticProvider)
threading.Thread(target=provider.serve_forever, daemon=True).start()
directory = Path(tempfile.mkdtemp(prefix='release-ui-data-' if args.release else 'ui-data-', dir=folder))
if args.release:
    binary = Path(__file__).resolve().parents[1] / 'dist' / 'GrokDesk' / 'GrokDesk.exe'
    process = subprocess.Popen([str(binary), '--no-window', '--port', '18793', '--data-dir', str(directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    client = httpx.Client(base_url='http://127.0.0.1:18793', trust_env=False)
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError('The packaged desktop application exited unexpectedly.')
        try:
            html = client.get('/').text
            token = html.partition('<meta name="session-token" content="')[2].split('"', 1)[0]
            if token:
                break
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError('The packaged desktop application did not start.')
    client.headers['X-GrokDesk-Token'] = token
    client.post('/api/settings', json={'base_url':f'http://127.0.0.1:{provider.server_port}', 'api_key':'synthetic-ui-key-not-valid-on-any-real-provider', 'model':'grok-desk-synthetic'}).raise_for_status()
    class ReleaseFixture:
        def serve_forever(self):
            try:
                while process.poll() is None:
                    time.sleep(0.3)
            finally:
                client.close()
                provider.shutdown()
                provider.server_close()
        def server_close(self):
            if process.poll() is None:
                process.terminate()
else:
    process = None
    server = create_server(directory, 8765)
    server.app.settings.update({'base_url': f'http://127.0.0.1:{provider.server_port}', 'api_key':'synthetic-ui-key-not-valid-on-real-services', 'model':'grok-desk-synthetic'})
image = Image.new('RGB', (640, 400), '#e8eee4')
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((80, 80, 560, 320), radius=24, fill='#306146')
draw.text((140, 170), 'SYNTHETIC UI TEST IMAGE', fill='#f5f6ec')
image.save(folder / 'sample.png')
(folder / 'sample.txt').write_text('这是一份仅用于本地界面验证的合成文档。项目：Grok Desk。待办：检查图片、附件与历史保存。', 'utf-8')
url = 'http://127.0.0.1:18793' if args.release else 'http://127.0.0.1:8765'
(folder / 'server.json').write_text(json.dumps({'url':url, 'provider_url':f'http://127.0.0.1:{provider.server_port}'}), 'utf-8')
print(('Packaged synthetic UI fixture: ' if args.release else 'Synthetic UI fixture: ') + url, flush=True)
try:
    if args.release:
        ReleaseFixture().serve_forever()
    else:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        server.app.shutdown_requested.wait()
        server.shutdown()
except KeyboardInterrupt:
    pass
finally:
    if not args.release:
        server.server_close()
        provider.shutdown()
        provider.server_close()
