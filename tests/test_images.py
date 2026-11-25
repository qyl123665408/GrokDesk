"""Image-generation regressions using only temporary data and a local provider."""
import base64
import io
import json
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from PIL import Image
from app import Database, create_server, uses_xai_image_protocol


SYNTHETIC_KEY = 'synthetic-image-key-never-valid-on-real-services'


class ImageProvider(BaseHTTPRequestHandler):
    payloads = []
    picture = b''

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw_body = self.rfile.read(int(self.headers['Content-Length']))
        content_type = self.headers.get('Content-Type', '')
        if content_type.startswith('multipart/form-data;'):
            message = BytesParser(policy=default).parsebytes(
                b'Content-Type: ' + content_type.encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw_body
            )
            payload, files = {}, []
            for part in message.iter_parts():
                name = part.get_param('name', header='content-disposition')
                filename = part.get_filename()
                if filename:
                    files.append((name, filename, part.get_payload(decode=True)))
                else:
                    payload[name] = part.get_payload(decode=True).decode('utf-8')
            self.payloads.append((self.path, payload, files))
        else:
            payload = json.loads(raw_body)
            self.payloads.append((self.path, payload, []))
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
        path, payload, files = ImageProvider.payloads[-1]
        self.assertEqual(path, '/v1/images/generations')
        self.assertEqual(payload, {'model': 'grok-imagine-synthetic', 'prompt': '合成测试：一张绿色图片', 'n': 1, 'aspect_ratio': '16:9'})
        self.assertEqual(files, [])
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

    def test_reference_image_uses_openai_compatible_edits_endpoint(self):
        self.request('POST', '/settings', {'image_model': 'grok-imagine-compatible-proxy'})
        picture = io.BytesIO()
        Image.new('RGB', (2, 3), '#86a887').save(picture, format='PNG')
        attachment = self.request('POST', '/attachments', {'name': 'reference.png', 'data': base64.b64encode(picture.getvalue()).decode()})
        conversation = self.request('POST', '/conversations', {})
        events, conversation = self.generate(conversation, '把背景改成蓝色', attachments=[attachment['id']])
        self.assertEqual(events['done']['status'], 'complete')
        path, payload, files = ImageProvider.payloads[-1]
        self.assertEqual(path, '/v1/images/edits')
        self.assertEqual(payload, {'model': 'grok-imagine-compatible-proxy', 'prompt': '把背景改成蓝色', 'n': '1'})
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0][0], 'image[]')
        self.assertEqual(files[0][1], 'reference.png')
        with Image.open(io.BytesIO(files[0][2])) as sent_image:
            self.assertEqual(sent_image.size, (2, 3))
            self.assertEqual(sent_image.convert('RGB').getpixel((0, 0)), (134, 168, 135))
        self.assertEqual(conversation['messages'][0]['attachments'][0]['id'], attachment['id'])
        self.assertEqual(conversation['messages'][-1]['generation']['edit'], True)
        self.assertEqual(conversation['messages'][-1]['generation']['input_attachments'], [attachment['id']])

    def test_reference_image_requires_unconsumed_image_attachment(self):
        attachment = self.request('POST', '/attachments', {'name': 'reference.png', 'data': base64.b64encode(ImageProvider.picture).decode()})
        conversation = self.request('POST', '/conversations', {})
        response = self.client.post('/api/images/generations', json={'conversation_id': conversation['id'], 'text': 'edit', 'attachments': ['missing-id']})
        self.assertEqual(response.status_code, 400)
        self.assertIn('已使用或不是受支持的图片', response.json()['error'])
        self.assertIsNotNone(self.server.app.db.query('SELECT id FROM attachments WHERE id=? AND message_id IS NULL', (attachment['id'],)))

    def test_xai_host_uses_json_edit_payload_for_custom_model_names(self):
        self.request('POST', '/settings', {'image_model': 'custom-image-model'})
        attachment = self.request('POST', '/attachments', {'name': 'reference.png', 'data': base64.b64encode(ImageProvider.picture).decode()})
        conversation = self.request('POST', '/conversations', {})
        with patch('app.uses_xai_image_protocol', return_value=True):
            events, _ = self.generate(conversation, '在右侧添加一朵云', attachments=[attachment['id']])
        self.assertEqual(events['done']['status'], 'complete')
        path, payload, files = ImageProvider.payloads[-1]
        self.assertEqual(path, '/v1/images/edits')
        self.assertEqual(payload['model'], 'custom-image-model')
        self.assertEqual(payload['prompt'], '在右侧添加一朵云')
        self.assertEqual(payload['image']['type'], 'image_url')
        self.assertTrue(payload['image']['url'].startswith('data:image/png;base64,'))
        self.assertEqual(files, [])

    def test_xai_protocol_is_selected_from_endpoint_host(self):
        self.assertTrue(uses_xai_image_protocol('https://api.x.ai/v1'))
        self.assertTrue(uses_xai_image_protocol('https://regional.api.x.ai/v1'))
        self.assertFalse(uses_xai_image_protocol('https://compatible-proxy.example/v1'))
        self.assertFalse(uses_xai_image_protocol('http://127.0.0.1:1234/v1'))

    def test_multiple_reference_images_are_sent_and_reused_when_regenerating(self):
        self.request('POST', '/settings', {'image_model': 'grok-imagine-compatible-proxy'})
        pictures = []
        for index, color in enumerate(('#86a887', '#7587a8')):
            picture = io.BytesIO()
            Image.new('RGB', (2 + index, 3), color).save(picture, format='PNG')
            pictures.append(picture.getvalue())
        attachments = [
            self.request('POST', '/attachments', {'name': f'reference-{index}.png', 'data': base64.b64encode(raw).decode()})
            for index, raw in enumerate(pictures, start=1)
        ]
        conversation = self.request('POST', '/conversations', {})
        events, conversation = self.generate(conversation, '合成两张参考图', attachments=[item['id'] for item in attachments])
        self.assertEqual(events['done']['status'], 'complete')
        path, payload, files = ImageProvider.payloads[-1]
        self.assertEqual(path, '/v1/images/edits')
        self.assertEqual(payload['prompt'], '合成两张参考图')
        self.assertEqual([item[0] for item in files], ['image[]', 'image[]'])
        signatures = []
        for index, item in enumerate(files):
            with Image.open(io.BytesIO(item[2])) as sent_image:
                self.assertEqual(sent_image.size, (2 + index, 3))
                signatures.append((sent_image.size, sent_image.convert('RGBA').tobytes()))

        events, conversation = self.generate(conversation, '', regenerate=True)
        self.assertEqual(events['done']['status'], 'complete')
        path, payload, files = ImageProvider.payloads[-1]
        self.assertEqual(path, '/v1/images/edits')
        self.assertEqual(payload['prompt'], '合成两张参考图')
        regenerated_signatures = []
        for item in files:
            with Image.open(io.BytesIO(item[2])) as sent_image:
                regenerated_signatures.append((sent_image.size, sent_image.convert('RGBA').tobytes()))
        self.assertEqual(regenerated_signatures, signatures)
        self.assertEqual(conversation['messages'][-1]['generation']['input_attachments'], [item['id'] for item in attachments])

    def test_missing_reference_image_returns_client_error(self):
        attachment = self.request('POST', '/attachments', {'name': 'missing.png', 'data': base64.b64encode(ImageProvider.picture).decode()})
        conversation = self.request('POST', '/conversations', {})
        record = self.server.app.db.query('SELECT image_name FROM attachments WHERE id=?', (attachment['id'],), True)
        (self.server.app.uploads / record['image_name']).unlink()

        response = self.client.post('/api/images/generations', json={'conversation_id': conversation['id'], 'text': 'edit', 'attachments': [attachment['id']]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('参考图片文件不存在或无法读取', response.json()['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
