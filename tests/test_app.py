"""Isolated integration tests. All API replies and credentials below are synthetic."""
import base64
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from app import Database, create_server


class SyntheticProvider(BaseHTTPRequestHandler):
    payloads = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        raw = json.dumps({'data': [{'id': 'grok-desk-synthetic'}, {'id': 'synthetic-other'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.payloads.append(payload)
        current = payload['messages'][-1]['content']
        current = current if isinstance(current, str) else current[0]['text']
        if current == 'synthetic-provider-error':
            raw = json.dumps({'error': {'message': 'Synthetic credential rejection'}}).encode()
            self.send_response(401)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if current == 'synthetic-json-response':
            raw = json.dumps({'choices': [{'message': {'content': 'Synthetic non-stream response'}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        try:
            chunks = ['# 本地测试回复\n\n', '已收到中文消息和附件。\n\n', '| 检查项 | 结果 |\n|---|---|\n| 图片与文档 | 已接收 |\n\n', '\x60\x60\x60python\nprint("hello")\n\x60\x60\x60']
            if current == 'synthetic-slow-response':
                chunks = ['本地停止测试 ' + str(i) + '\n' for i in range(100)]
            for content in chunks:
                self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': {'content': content}}]}, ensure_ascii=False) + '\n\n').encode())
                self.wfile.flush()
                time.sleep(0.06 if current == 'synthetic-slow-response' else 0.01)
            self.wfile.write(b'data: {"choices":[],"usage":{"total_tokens":42}}\n\ndata: [DONE]\n\n')
        except (OSError, ValueError):
            pass


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = ThreadingHTTPServer(('127.0.0.1', 0), SyntheticProvider)
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
        self.request('POST', '/settings', {'base_url': f'http://127.0.0.1:{self.provider.server_port}', 'api_key': 'synthetic-key-never-valid-on-real-services', 'model': 'grok-desk-synthetic'})

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, method, path, body=None):
        response = self.client.request(method, '/api' + path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def upload(self, name, content):
        return self.request('POST', '/attachments', {'name': name, 'data': base64.b64encode(content).decode()})

    def office(self, name, xml):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr(name, xml)
        return data.getvalue()

    def send(self, conversation, text, attachments=None, regenerate=False):
        response = self.client.post('/api/chat', json={'conversation_id': conversation['id'], 'model': 'grok-desk-synthetic', 'text': text, 'attachments': attachments or [], 'regenerate': regenerate})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('event: done', response.text)
        return self.request('GET', '/conversations/' + conversation['id'])

    def test_images_documents_multiturn_and_persistence(self):
        public = self.request('GET', '/bootstrap')['settings']
        self.assertTrue(public['has_api_key'])
        self.assertNotIn('encrypted_key', public)
        self.assertNotIn('synthetic-key-never-valid', (Path(self.temporary.name) / 'settings.json').read_text('utf-8'))
        self.assertEqual(self.request('GET', '/models')['models'][0], 'grok-desk-synthetic')
        text = self.upload('中文说明.txt', '这是合成测试文档，数量为三。'.encode())
        png = io.BytesIO()
        Image.new('RGB', (48, 32), '#86a887').save(png, format='PNG')
        picture = self.upload('测试图片.png', png.getvalue())
        word = self.upload('测试.docx', self.office('word/document.xml', '<document><p><r><t>Synthetic Word content</t></r></p></document>'))
        sheet = self.upload('测试.xlsx', self.office('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>Synthetic cell</t></is></c><c r="B1"><v>37</v></c></row></sheetData></worksheet>'))
        slides = self.upload('测试.pptx', self.office('ppt/slides/slide1.xml', '<slide><p><r><t>Synthetic slide</t></r></p></slide>'))
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 20 150 Td (Synthetic PDF text) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        pdf_data = io.BytesIO()
        writer.write(pdf_data)
        pdf = self.upload('测试.pdf', pdf_data.getvalue())
        conversation = self.request('POST', '/conversations', {'model': 'grok-desk-synthetic'})
        ids = [item['id'] for item in [text, picture, word, sheet, slides, pdf]]
        conversation = self.send(conversation, '请查看这些合成测试附件', ids)
        self.assertEqual(conversation['messages'][-1]['status'], 'complete')
        self.assertEqual(conversation['messages'][-1]['usage']['total_tokens'], 42)
        content = SyntheticProvider.payloads[-1]['messages'][-1]['content']
        self.assertTrue(any(part['type'] == 'image_url' and part['image_url']['url'].startswith('data:image/png;base64,') for part in content))
        document_text = '\n'.join(part['text'] for part in content if part['type'] == 'text')
        for expected in ['这是合成测试文档', 'Synthetic Word', 'Synthetic cell', '37', 'Synthetic slide', 'Synthetic PDF']:
            self.assertIn(expected, document_text)
        conversation = self.send(conversation, '继续这段合成测试对话')
        self.assertEqual(len(conversation['messages']), 4)
        self.assertEqual(len(SyntheticProvider.payloads[-1]['messages']), 4)
        conversation = self.send(conversation, '', regenerate=True)
        self.assertEqual(len(conversation['messages']), 4)
        reloaded = Database(Path(self.temporary.name)).conversation(conversation['id'])
        self.assertEqual(len(reloaded['messages'][0]['attachments']), 6)
        self.request('PATCH', '/conversations/' + conversation['id'], {'title': '重新命名的合成测试', 'pinned': True})
        self.assertEqual(len(self.request('GET', '/conversations?q=合成测试')), 1)
        downloaded = self.client.get('/api/attachments/' + text['id'] + '/file')
        self.assertEqual(downloaded.content.decode(), '这是合成测试文档，数量为三。')
        self.request('DELETE', '/conversations/' + conversation['id'])
        self.assertFalse(list((Path(self.temporary.name) / 'attachments').iterdir()))

    def test_provider_failure_retains_message_and_retry(self):
        conversation = self.request('POST', '/conversations', {})
        conversation = self.send(conversation, 'synthetic-provider-error')
        self.assertEqual(conversation['messages'][-1]['status'], 'error')
        self.assertIn('HTTP 401', conversation['messages'][-1]['error'])
        self.assertEqual(conversation['messages'][0]['content'], 'synthetic-provider-error')
        conversation = self.send(conversation, 'synthetic-json-response')
        self.assertEqual(conversation['messages'][-1]['content'], 'Synthetic non-stream response')

    def test_stop_preserves_partial_response(self):
        conversation = self.request('POST', '/conversations', {})
        result = {}
        def generate():
            with httpx.Client(base_url=self.client.base_url, headers=dict(self.client.headers), timeout=15, trust_env=False) as client:
                result['response'] = client.post('/api/chat', json={'conversation_id':conversation['id'], 'text':'synthetic-slow-response', 'model':'grok-desk-synthetic'})
        thread = threading.Thread(target=generate)
        thread.start()
        assistant = None
        for _ in range(100):
            messages = self.request('GET', '/conversations/' + conversation['id'])['messages']
            if messages and messages[-1]['role'] == 'assistant':
                assistant = messages[-1]['id']
                if messages[-1]['content']:
                    break
            time.sleep(0.03)
        self.assertIsNotNone(assistant)
        self.request('POST', '/chat/' + assistant + '/stop', {})
        thread.join(5)
        self.assertFalse(thread.is_alive())
        last = self.request('GET', '/conversations/' + conversation['id'])['messages'][-1]
        self.assertEqual(last['status'], 'stopped')
        self.assertTrue(last['content'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
