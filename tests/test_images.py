"""Image-generation regressions using only temporary data and a local provider."""
import base64
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from PIL import Image
from app import Database, create_server


SYNTHETIC_KEY = 'synthetic-image-key-never-valid-on-real-services'


class ImageProvider(BaseHTTPRequestHandler):
    payloads = []
    picture = b''

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.payloads.append((self.path, payload))
        if payload['prompt'] == 'synthetic-image-provider-error':
            status = 403
            result = {'error': {'message': 'Synthetic model access denied for ' + SYNTHETIC_KEY}}
        else:
            status = 200
            result = {'data': [{'b64_json': base64.b64encode(self.picture).decode('ascii')}], 'usage': {'total_tokens': 7}}
        raw = json.dumps(result).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ImageWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        picture = io.BytesIO()
        Image.new('RGB', (1, 1), '#86a887').save(picture, format='PNG')
        ImageProvider.picture = picture.getvalue()
        cls.provider = ThreadingHTTPServer(('127.0.0.1', 0), ImageProvider)
        threading.Thread(target=cls.provider.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()
        cls.provider.server_close()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = create_server(self.temporary.name)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}', headers={'X-GrokDesk-Token': self.server.app.token}, timeout=15, trust_env=False)
        self.request('POST', '/settings', {'base_url': f'http://127.0.0.1:{self.provider.server_port}', 'api_key': SYNTHETIC_KEY, 'image_model': 'grok-imagine-synthetic'})

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, method, path, body=None):
        response = self.client.request(method, '/api' + path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def generate(self, conversation, text, **options):
        response = self.client.post('/api/images/generations', json={'conversation_id': conversation['id'], 'text': text, **options})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('text/event-stream', response.headers['content-type'])
        events = {}
        for packet in response.text.split('\n\n'):
            lines = packet.splitlines()
            if len(lines) >= 2 and lines[0].startswith('event: ') and lines[1].startswith('data: '):
                events[lines[0][7:]] = json.loads(lines[1][6:])
        self.assertIn('start', events)
        self.assertIn('done', events)
        return events, self.request('GET', '/conversations/' + conversation['id'])

    def test_generated_image_attachment_and_metadata_persist(self):
        conversation = self.request('POST', '/conversations', {})
        events, conversation = self.generate(conversation, '合成测试：一张绿色图片', image_size='landscape')
        self.assertEqual(events['done']['status'], 'complete')
        self.assertIn('progress', events)
        path, payload = ImageProvider.payloads[-1]
        self.assertEqual(path, '/v1/images/generations')
        self.assertEqual(payload, {'model': 'grok-imagine-synthetic', 'prompt': '合成测试：一张绿色图片', 'n': 1, 'aspect_ratio': '16:9'})
        reloaded = Database(Path(self.temporary.name)).conversation(conversation['id'])
        self.assertEqual(len(reloaded['messages']), 2)
        assistant = reloaded['messages'][-1]
        self.assertEqual((assistant['kind'], assistant['status']), ('image', 'complete'))
        self.assertEqual(assistant['usage']['total_tokens'], 7)
        self.assertEqual(assistant['generation']['images'], [{'width': 1, 'height': 1}])
        self.assertEqual(len(assistant['attachments']), 1)
        attachment = assistant['attachments'][0]
        self.assertEqual(attachment['mime'], 'image/png')
        downloaded = self.client.get('/api/attachments/' + attachment['id'] + '/file')
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, ImageProvider.picture)
        preview = self.client.get('/api/attachments/' + attachment['id'] + '/preview')
        self.assertEqual(preview.status_code, 200)
        with Image.open(io.BytesIO(preview.content)) as image:
            self.assertEqual(image.size, (1, 1))

    def test_provider_failure_preserves_prompt_and_explains_permissions(self):
        conversation = self.request('POST', '/conversations', {})
        events, conversation = self.generate(conversation, 'synthetic-image-provider-error')
        self.assertEqual(events['done']['status'], 'error')
        self.assertEqual(conversation['messages'][0]['content'], 'synthetic-image-provider-error')
        assistant = Database(Path(self.temporary.name)).conversation(conversation['id'])['messages'][-1]
        self.assertEqual(assistant['status'], 'error')
        self.assertEqual(assistant['attachments'], [])
        self.assertIn('HTTP 403', assistant['error'])
        self.assertIn('密钥没有访问权限', assistant['error'])
        self.assertIn('请确认密钥分组包含所选生图模型', assistant['error'])
        self.assertIn('[密钥已隐藏]', assistant['error'])
        self.assertNotIn(SYNTHETIC_KEY, events['done']['error'])
        self.assertEqual(events['done']['error'], assistant['error'])
        self.assertFalse(list((Path(self.temporary.name) / 'attachments').iterdir()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
