"""Run source UI checks against isolated local synthetic providers."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import httpx

root = Path(__file__).resolve().parents[1]
node = os.environ.get('NODE_EXECUTABLE') or shutil.which('node')
if not node:
    raise RuntimeError('Node.js is required for UI checks. Install Node.js or set NODE_EXECUTABLE.')
environment = dict(os.environ)
for fixture, test, port in [('serve_fixture.py', 'ui_smoke.mjs', 8765), ('serve_control_fixture.py', 'control_ui_smoke.mjs', 8766)]:
    process = subprocess.Popen([sys.executable, str(root / 'tests' / fixture)], cwd=root, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=3)
    token = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f'Fixture failed: {fixture}: {process.returncode}')
            try:
                match = re.search(r'name="session-token" content="([^"]+)"', client.get('/').raise_for_status().text)
                if match:
                    token = match[1]
                    break
            except httpx.TransportError:
                pass
            time.sleep(.1)
        if not token:
            raise RuntimeError(f'Fixture did not become ready: {fixture}')
        subprocess.run([str(node), str(root / 'tests' / test)], cwd=root, env=environment, check=True, timeout=150)
    finally:
        if token:
            try:
                client.post('/api/exit', json={}, headers={'X-GrokDesk-Token': token})
            except httpx.HTTPError:
                pass
        client.close()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(5)
