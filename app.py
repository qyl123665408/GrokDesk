from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from xml.etree import ElementTree

import httpx
from PIL import Image, ImageOps
from pypdf import PdfReader
from desktop import DesktopInstance, open_window, run_desktop_window
from computer_control import (ControlError, ControlRun, DesktopController,
                              DEFAULT_STEPS, MAX_STEPS, control_messages,
                              parse_action)

APP_NAME = 'Grok Desk'
APP_VERSION = '1.4.0'
ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
MAX_FILE = 20 * 1024 * 1024
MAX_TEXT = 120_000
TEXT_EXTENSIONS = set('.txt .md .csv .tsv .json .jsonl .xml .yaml .yml .log .py .js .ts .tsx .jsx .html .css .sql .c .cpp .h .cs .java .go .rs .sh .ps1 .toml .ini .srt .vtt'.split())
IMAGE_EXTENSIONS = set('.jpg .jpeg .png .webp .gif .bmp .tif .tiff'.split())
MAX_GENERATED_IMAGE = 40 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


class UserError(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_byte))]


def protect_key(value, decrypt=False):
    if not value:
        return ''
    if os.name != 'nt':
        raise UserError('此桌面版使用 Windows 加密保存密钥，请在 Windows 上运行。')
    raw = base64.b64decode(value) if decrypt else value.encode('utf-8')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise UserError('无法读取 Windows 加密的密钥，请在设置中重新填写。')
    try:
        output = ctypes.string_at(result.data, result.size)
        return output.decode('utf-8') if decrypt else base64.b64encode(output).decode('ascii')
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def normalize_base(value):
    value = str(value).strip().rstrip('/')
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UserError('请填写完整的 API 地址，例如 https://ergouzi.life。')
    value = re.sub(r'/(chat/completions|images/generations|models)$', '', value)
    return value if value.endswith('/v1') else value + '/v1'


class Settings:
    defaults = dict(base_url='https://ergouzi.life', model='', image_model='', image_size='auto', image_count=1, encrypted_key='', system_prompt='你是一位有帮助的中文助手。优先用中文回答，除非用户另有要求。', temperature=0.7, max_tokens=4096, context_turns=20, theme='light', font_size=15)

    def __init__(self, folder):
        self.path = folder / 'settings.json'
        self.lock = threading.RLock()
        self.value = dict(self.defaults)
        if self.path.exists():
            try:
                self.value.update(json.loads(self.path.read_text('utf-8')))
            except (ValueError, OSError):
                pass

    def public(self):
        with self.lock:
            return {**{k: v for k, v in self.value.items() if k != 'encrypted_key'}, 'has_api_key': bool(self.value['encrypted_key'])}

    def credentials(self):
        with self.lock:
            config = dict(self.value)
        config['api_key'] = protect_key(config.pop('encrypted_key'), decrypt=True)
        if not config['api_key']:
            raise UserError('先打开连接设置，填写 API 密钥。')
        config['endpoint'] = normalize_base(config['base_url'])
        return config

    def update(self, data):
        with self.lock:
            updated = dict(self.value)
            for key in self.defaults:
                if key in data and key != 'encrypted_key':
                    updated[key] = data[key]
            normalize_base(updated['base_url'])
            for key, lower, upper, cast in [('temperature', 0, 2, float), ('max_tokens', 256, 65536, int), ('context_turns', 1, 100, int), ('font_size', 12, 22, int), ('image_count', 1, 4, int)]:
                try:
                    updated[key] = cast(updated[key])
                except (ValueError, TypeError):
                    raise UserError('生成参数格式不正确。')
                if not lower <= updated[key] <= upper:
                    raise UserError('生成参数超出允许范围。')
            for key in ['base_url', 'model', 'image_model', 'image_size', 'system_prompt', 'theme']:
                if not isinstance(updated[key], str):
                    raise UserError('设置内容格式不正确。')
            if updated['image_size'] not in ('auto', 'square', 'landscape', 'portrait'):
                raise UserError('请选择有效的图片比例。')
            if data.get('api_key', '').strip():
                updated['encrypted_key'] = protect_key(data['api_key'].strip())
            if data.get('clear_api_key'):
                updated['encrypted_key'] = ''
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), 'utf-8')
            temporary.replace(self.path)
            self.value = updated
        return self.public()


class Database:
    def __init__(self, folder):
        self.path = folder / 'history.sqlite3'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'complete', error TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, usage TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS attachments (id TEXT PRIMARY KEY, message_id TEXT REFERENCES messages(id) ON DELETE CASCADE, name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, stored_name TEXT NOT NULL, image_name TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS message_conversation ON messages(conversation_id);
                CREATE INDEX IF NOT EXISTS attachment_message ON attachments(message_id);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(messages)')}
            if 'kind' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")
            if 'generation' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN generation TEXT NOT NULL DEFAULT '{}'")
            db.execute("UPDATE messages SET status='stopped', error='上次生成被中断，可以重新生成。' WHERE status='streaming'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def query(self, sql, args=(), one=False):
        with self.connect() as db:
            rows = db.execute(sql, args).fetchall()
        return (dict(rows[0]) if rows else None) if one else [dict(row) for row in rows]

    def execute(self, sql, args=()):
        with self.connect() as db:
            db.execute(sql, args)

    def conversation(self, identifier):
        item = self.query('SELECT * FROM conversations WHERE id=?', (identifier,), True)
        if not item:
            raise UserError('对话不存在。', 404)
        item['messages'] = self.query('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid', (identifier,))
        for message in item['messages']:
            message['attachments'] = self.query('SELECT id,name,mime,size,image_name FROM attachments WHERE message_id=?', (message['id'],))
            message['usage'] = json.loads(message['usage'])
            message['generation'] = json.loads(message['generation'])
        return item

    def new_conversation(self, model=''):
        identifier, stamp = uuid.uuid4().hex, now()
        self.execute('INSERT INTO conversations(id,title,model,created_at,updated_at) VALUES (?,?,?,?,?)', (identifier, '新对话', model, stamp, stamp))
        return self.conversation(identifier)


def xml_text(data):
    root = ElementTree.fromstring(data)
    return '\n'.join(''.join(node.itertext()) for node in root.iter() if node.tag.rsplit('}', 1)[-1] in ('p', 'row'))


def extract_document(raw, extension):
    if extension == '.pdf':
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and not reader.decrypt(''):
            raise UserError('PDF 有密码，请先解锁后上传。')
        parts, length, has_text = [], 0, False
        for index, page in enumerate(reader.pages):
            content = page.extract_text() or ''
            has_text = has_text or bool(content.strip())
            parts.append(f'[第 {index + 1} 页]\n{content}')
            length += len(parts[-1])
            if length > MAX_TEXT:
                raise UserError('文档文字超过 12 万字，请分段上传。')
        if not has_text:
            raise UserError('这个 PDF 是扫描件，请把需要的页面导出成图片发送。')
        return '\n\n'.join(parts)
    if extension in ('.docx', '.xlsx', '.pptx'):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
                raise UserError('解压后的文档太大，请拆分后上传。')
            if extension == '.docx':
                return xml_text(archive.read('word/document.xml'))
            if extension == '.pptx':
                names = sorted((name for name in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', name)), key=lambda name: int(re.search(r'(\d+)\.xml', name).group(1)))
                return '\n\n'.join(f'[幻灯片 {i + 1}]\n{xml_text(archive.read(name))}' for i, name in enumerate(names))
            namespace = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings = [''.join(node.itertext()) for node in ElementTree.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', namespace)]
            names = sorted(name for name in archive.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', name))
            sheets = []
            for i, name in enumerate(names):
                rows = []
                for row in ElementTree.fromstring(archive.read(name)).findall('.//s:row', namespace):
                    cells = []
                    for cell in row.findall('s:c', namespace):
                        value = cell.find('s:v', namespace)
                        value = value.text if value is not None and value.text else ''
                        if cell.get('t') == 's' and value.isdigit():
                            value = strings[int(value)]
                        if cell.get('t') == 'inlineStr':
                            value = ''.join(cell.itertext())
                        cells.append(f'{cell.get("r", "")}: {value}')
                    rows.append(' | '.join(cells))
                sheets.append(f'[工作表 {i + 1}]\n' + '\n'.join(rows))
            return '\n\n'.join(sheets)
    for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
        try:
            text = raw.decode(encoding)
            if '\x00' not in text:
                return text
        except UnicodeError:
            continue
    raise UserError('无法识别文本编码，请另存为 UTF-8 后上传。')


@dataclass
class Job:
    conversation_id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    response: object = None

    def stop(self):
        self.cancel.set()
        if self.response is not None:
            try:
                self.response.close()
            except Exception:
                pass


class Application:
    def __init__(self, data_dir):
        self.folder = Path(data_dir)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.uploads = self.folder / 'attachments'
        self.uploads.mkdir(exist_ok=True)
        self.settings = Settings(self.folder)
        self.db = Database(self.folder)
        self.token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.controller = DesktopController()
        self.control_runs = {}
        self.control_lock = threading.RLock()
        self.shutdown_requested = threading.Event()

    def upload(self, body):
        filename = str(body.get('name', '')).replace('\\', '/').split('/')[-1][:180]
        extension = Path(filename).suffix.lower()
        if extension not in TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {'.pdf', '.docx', '.xlsx', '.pptx'}:
            raise UserError('暂不支持这个格式。可以发送图片、PDF、DOCX、XLSX、PPTX、文本或代码文件。')
        try:
            raw = base64.b64decode(body.get('data', ''), validate=True)
        except (ValueError, TypeError):
            raise UserError('附件内容无效，请重新选择文件。')
        if not raw or len(raw) > MAX_FILE:
            raise UserError('单个文件需大于 0 字节且不超过 20 MB。')
        identifier = uuid.uuid4().hex
        stored_name = identifier + extension
        image_name, text = '', ''
        mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        try:
            if extension in IMAGE_EXTENSIONS:
                with Image.open(io.BytesIO(raw)) as image:
                    image.seek(0)
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((4096, 4096))
                    image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
                    image_name = identifier + '.vision.png'
                    image.save(self.uploads / image_name, format='PNG', optimize=True)
            else:
                text = extract_document(raw, extension).strip()
                if not text:
                    raise UserError('文件中没有可读取的文字。扫描文档请转换成图片发送。')
                if len(text) > MAX_TEXT:
                    raise UserError('文档文字超过 12 万字，请分段上传。')
            (self.uploads / stored_name).write_bytes(raw)
            self.db.execute('INSERT INTO attachments(id,name,mime,size,stored_name,image_name,text,created_at) VALUES (?,?,?,?,?,?,?,?)', (identifier, filename, mime, len(raw), stored_name, image_name, text, now()))
        except UserError:
            if image_name:
                (self.uploads / image_name).unlink(missing_ok=True)
            raise
        except Exception:
            if image_name:
                (self.uploads / image_name).unlink(missing_ok=True)
            raise UserError('文件读取失败，请检查文件是否损坏、加密或格式不符。')
        return dict(id=identifier, name=filename, mime=mime, size=len(raw), image_name=image_name, text_length=len(text))

    def models(self):
        config = self.settings.credentials()
        with httpx.Client(timeout=httpx.Timeout(40, connect=15), follow_redirects=False) as client:
            response = client.get(config['endpoint'] + '/models', headers={'Authorization': 'Bearer ' + config['api_key']})
            if response.status_code >= 400:
                raise UserError(upstream_error(response.status_code, response.text, config['api_key']))
            try:
                models = response.json().get('data', [])
                return sorted({item['id'] for item in models if isinstance(item, dict) and item.get('id')}, key=lambda name: ('grok' not in name.lower(), name.lower()))
            except (ValueError, TypeError, AttributeError):
                raise UserError('服务没有返回模型列表，请检查 API 地址，或手动填写模型名称。')

    def api_messages(self, conversation, config):
        output = []
        if config['system_prompt'].strip():
            output.append({'role': 'system', 'content': config['system_prompt'].strip()})
        messages = [item for item in conversation['messages'] if item['role'] == 'user' or (item['role'] == 'assistant' and item['content'])]
        starts = [index for index, item in enumerate(messages) if item['role'] == 'user']
        start = starts[-int(config['context_turns'])] if len(starts) >= int(config['context_turns']) else 0
        for message in messages[start:]:
            if message['role'] == 'assistant':
                output.append({'role': 'assistant', 'content': message['content']})
                continue
            content = [{'type': 'text', 'text': message['content'] or '请分析这些附件。'}]
            for attachment in message['attachments']:
                record = self.db.query('SELECT * FROM attachments WHERE id=?', (attachment['id'],), True)
                if record['image_name']:
                    content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode((self.uploads / record['image_name']).read_bytes()).decode('ascii')}})
                else:
                    content.append({'type': 'text', 'text': f'\n[附件：{record["name"]}]\n{record["text"]}\n[附件结束]'})
            output.append({'role': 'user', 'content': content if len(content) > 1 else content[0]['text']})
        return output

    def cleanup_files(self, records):
        for record in records:
            for name in (record['stored_name'], record['image_name']):
                if name:
                    (self.uploads / name).unlink(missing_ok=True)

    def image_payload(self, model, prompt, size, count):
        if not model or len(model) > 200:
            raise UserError('请在生图模式中选择或填写有权限的生图模型。')
        if not prompt or len(prompt) > 32_000:
            raise UserError('请输入图片描述，最多 32000 个字符。')
        if size not in ('auto', 'square', 'landscape', 'portrait'):
            raise UserError('请选择有效的图片比例。')
        if type(count) is not int or not 1 <= count <= 4:
            raise UserError('每次可以生成 1–4 张图片。')
        lower = model.lower()
        if 'dall-e-3' in lower and count != 1:
            raise UserError('DALL·E 3 每次只支持 1 张，请修改生成数量。')
        payload = dict(model=model, prompt=prompt, n=count)
        if size != 'auto':
            if 'grok' in lower:
                if 'imagine' not in lower:
                    raise UserError('此 Grok 生图模型请使用「默认比例」。')
                payload['aspect_ratio'] = {'square': '1:1', 'landscape': '16:9', 'portrait': '9:16'}[size]
            else:
                wide = '1792x1024' if 'dall-e-3' in lower else '1536x1024'
                tall = '1024x1792' if 'dall-e-3' in lower else '1024x1536'
                payload['size'] = {'square': '1024x1024', 'landscape': wide, 'portrait': tall}[size]
        return payload

    def image_bytes(self, item, job, client):
        if item.get('b64_json'):
            encoded = item['b64_json']
            if not isinstance(encoded, str) or len(encoded) > MAX_GENERATED_IMAGE * 4 // 3 + 4:
                raise UserError('生成图片超过 40 MB，无法保存。')
            try:
                raw = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise UserError('服务返回的图片编码无效。')
        elif item.get('url'):
            url = item['url']
            if not isinstance(url, str):
                raise UserError('服务返回的图片地址无效。')
            if url.startswith('data:image/') and ';base64,' in url:
                return self.image_bytes({'b64_json': url.split(';base64,', 1)[1]}, job, client)
            # Image hosts receive no API key. Store the actual image immediately,
            # since provider URLs commonly expire.
            raw = bytearray()
            for redirect in range(4):
                parsed = urlparse(url)
                if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
                    raise UserError('服务返回的图片地址不是有效的 HTTP 地址。')
                with client.stream('GET', url) as response:
                    job.response = response
                    if response.is_redirect:
                        url = str(response.url.join(response.headers.get('location', '')))
                        continue
                    if response.status_code != 200:
                        raise UserError('图片已生成，但下载失败。请保留这条记录，稍后检查服务商结果；重新生成可能再次计费。')
                    for part in response.iter_bytes():
                        if job.cancel.is_set():
                            return b''
                        raw.extend(part)
                        if len(raw) > MAX_GENERATED_IMAGE:
                            raise UserError('生成图片超过 40 MB，无法保存。')
                    break
            else:
                raise UserError('图片下载的跳转次数过多。')
            raw = bytes(raw)
        else:
            raise UserError('服务没有返回图片数据或下载地址，请确认所选模型支持生图接口。')
        if not raw or len(raw) > MAX_GENERATED_IMAGE:
            raise UserError('服务返回了空图片或图片超过 40 MB。')
        return raw

    def store_generated(self, raw, index, message_id):
        identifier = uuid.uuid4().hex
        preview_name = identifier + '.preview.png'
        stored_name = ''
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ('PNG', 'JPEG', 'WEBP', 'GIF') or image.width * image.height > 40_000_000:
                    raise UserError('服务返回的图片格式或像素大小不受支持。')
                extension = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp', 'GIF': '.gif'}[image.format]
                mime = Image.MIME[image.format]
                width, height = image.size
                image.load()
                preview = ImageOps.exif_transpose(image).convert('RGBA' if 'A' in image.getbands() else 'RGB')
                preview.thumbnail((1600, 1600))
                preview.save(self.uploads / preview_name, 'PNG')
            stored_name = identifier + extension
            (self.uploads / stored_name).write_bytes(raw)
            name = f'Grok-生成图片-{index}{extension}'
            self.db.execute('INSERT INTO attachments(id,message_id,name,mime,size,stored_name,image_name,created_at) VALUES (?,?,?,?,?,?,?,?)', (identifier, message_id, name, mime, len(raw), stored_name, preview_name, now()))
            return {'width': width, 'height': height}
        except Exception as error:
            (self.uploads / preview_name).unlink(missing_ok=True)
            if stored_name:
                (self.uploads / stored_name).unlink(missing_ok=True)
            if isinstance(error, UserError):
                raise
            raise UserError('服务返回的内容无法读取为图片。')


def upstream_error(status, text, key=''):
    tips = {401: '密钥无效或已过期，请在连接设置中更换。', 402: '服务余额不足，请到服务平台查看账户余额。', 403: '密钥没有访问权限，请检查分组和模型权限。', 404: '没有找到接口或模型，请检查 API 地址和模型名称。', 413: '本次附件或上下文过大，请减少附件或新建对话。', 429: '服务限流或余额不足，请稍后重试并检查平台额度。'}
    detail = ''
    try:
        error = json.loads(text).get('error', {})
        detail = error.get('message', '') if isinstance(error, dict) else str(error)
    except (ValueError, AttributeError):
        pass
    if key:
        detail = detail.replace(key, '[密钥已隐藏]')
    tip = tips.get(status, '模型服务暂时没有完成请求，请稍后重试。')
    return f'{tip} (HTTP {status})' + (f'\n{detail[:600]}' if detail else '')


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    @property
    def app(self):
        return self.server.app

    def log_message(self, *args):
        pass

    def send_data(self, data, status=200, mime='application/json; charset=utf-8', extra=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        elif isinstance(data, str):
            data = data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if self.close_connection:
            self.send_header('Connection', 'close')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise UserError('请求格式不正确。')
        if not 0 < length <= 30 * 1024 * 1024:
            raise UserError('请求内容为空或超过大小限制。', 413)
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise UserError('请求内容不是有效 JSON。')
        if not isinstance(body, dict):
            raise UserError('请求格式不正确。')
        return body

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PATCH(self):
        self.dispatch('PATCH')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def dispatch(self, method):
        try:
            self.route(method)
        except UserError as error:
            self.close_connection = True
            self.send_data({'error': str(error)}, error.status)
        except (httpx.TimeoutException, httpx.ConnectError):
            self.send_data({'error': '连接模型服务超时或失败，请检查网络和 API 地址。'}, 502)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self.send_data({'error': '操作没有完成，请重试；如果持续出现，请重启程序。'}, 500)

    def route(self, method):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if not path.startswith('/api/'):
            if method != 'GET':
                raise UserError('找不到这个页面。', 404)
            if path in ('/', '/index.html'):
                html = (ROOT / 'web' / 'index.html').read_text('utf-8').replace('__SESSION_TOKEN__', self.app.token)
                self.send_data(html, mime='text/html; charset=utf-8')
                return
            file = (ROOT / 'web' / path.lstrip('/')).resolve()
            if not file.is_relative_to((ROOT / 'web').resolve()) or not file.is_file():
                raise UserError('文件不存在。', 404)
            self.send_data(file.read_bytes(), mime=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
            return
        token = self.headers.get('X-GrokDesk-Token', '') or query.get('token', [''])[0]
        if not secrets.compare_digest(token, self.app.token):
            if method in ('POST', 'PATCH', 'DELETE'):
                # Consume a bounded request body before closing, so Windows sends
                # the error response instead of resetting an unread connection.
                self.read_json()
            raise UserError('页面连接已过期，请重新打开软件。', 403)
        if path == '/api/bootstrap' and method == 'GET':
            self.send_data({'settings': self.app.settings.public(), 'data_dir': str(self.app.folder), 'version': APP_VERSION})
        elif path == '/api/exit' and method == 'POST':
            self.read_json()
            self.send_data({'ok': True})
            self.app.shutdown_requested.set()
        elif path == '/api/settings' and method == 'POST':
            self.send_data(self.app.settings.update(self.read_json()))
        elif path == '/api/models' and method == 'GET':
            self.send_data({'models': self.app.models()})
        elif path == '/api/control' and method == 'POST':
            self.control(self.read_json())
        elif re.fullmatch('/api/control/[a-f0-9]{32}/(approve|stop)', path) and method == 'POST':
            body = self.read_json()
            with self.app.control_lock:
                run = self.app.control_runs.get(path.split('/')[3])
            if path.endswith('/stop'):
                if run:
                    run.stop()
                self.send_data({'ok': True})
            else:
                action_id, approved = body.get('action_id'), body.get('approved')
                if not isinstance(action_id, str) or not re.fullmatch('[a-f0-9]{32}', action_id) or type(approved) is not bool:
                    raise UserError('确认操作的请求格式不正确。')
                if not run or not run.approve(action_id, approved):
                    raise UserError('这个操作已失效或已经处理，请等待新的操作。', 409)
                self.send_data({'ok': True})
        elif path == '/api/conversations' and method == 'GET':
            search = query.get('q', [''])[0].strip()
            self.send_data(self.app.db.query('SELECT c.*, (SELECT content FROM messages WHERE conversation_id=c.id ORDER BY rowid DESC LIMIT 1) AS preview, (SELECT COUNT(*) FROM messages WHERE conversation_id=c.id) AS message_count FROM conversations c WHERE c.title LIKE ? OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND m.content LIKE ?) ORDER BY c.pinned DESC,c.updated_at DESC', (f'%{search}%', f'%{search}%')))
        elif path == '/api/conversations' and method == 'POST':
            body = self.read_json()
            self.send_data(self.app.db.new_conversation(str(body.get('model', ''))))
        elif re.fullmatch('/api/conversations/[a-f0-9]{32}', path):
            identifier = path.split('/')[-1]
            self.app.db.conversation(identifier)
            if method == 'GET':
                self.send_data(self.app.db.conversation(identifier))
            elif method == 'PATCH':
                body = self.read_json()
                for key in ('title', 'pinned', 'model'):
                    if key in body:
                        value = int(bool(body[key])) if key == 'pinned' else str(body[key]).strip()[:200]
                        if key == 'title' and not value:
                            raise UserError('对话名称不能为空。')
                        self.app.db.execute(f'UPDATE conversations SET {key}=? WHERE id=?', (value, identifier))
                self.send_data(self.app.db.conversation(identifier))
            elif method == 'DELETE':
                with self.app.jobs_lock:
                    if any(job.conversation_id == identifier for job in self.app.jobs.values()):
                        raise UserError('请先停止这段对话的生成，再删除。', 409)
                records = self.app.db.query('SELECT a.* FROM attachments a JOIN messages m ON a.message_id=m.id WHERE m.conversation_id=?', (identifier,))
                self.app.db.execute('DELETE FROM conversations WHERE id=?', (identifier,))
                self.app.cleanup_files(records)
                self.send_data({'ok': True})
            else:
                raise UserError('不支持这个操作。', 405)
        elif path == '/api/attachments' and method == 'POST':
            self.send_data(self.app.upload(self.read_json()))
        elif re.fullmatch('/api/attachments/[a-f0-9]{32}(/file|/preview)?', path):
            identifier = path.split('/')[3]
            record = self.app.db.query('SELECT * FROM attachments WHERE id=?', (identifier,), True)
            if not record:
                raise UserError('附件不存在。', 404)
            if method == 'DELETE' and record['message_id'] is None:
                self.app.db.execute('DELETE FROM attachments WHERE id=?', (identifier,))
                self.app.cleanup_files([record])
                self.send_data({'ok': True})
            elif method == 'GET':
                preview = path.endswith('/preview') and record['image_name']
                filename = record['image_name'] if preview else record['stored_name']
                extra = {} if preview else {'Content-Disposition': "attachment; filename*=UTF-8''" + quote(record['name'])}
                self.send_data((self.app.uploads / filename).read_bytes(), mime='image/png' if preview else record['mime'], extra=extra)
            else:
                raise UserError('这个附件不能删除。', 409)
        elif path == '/api/chat' and method == 'POST':
            self.chat(self.read_json())
        elif path == '/api/images/generations' and method == 'POST':
            self.generate_images(self.read_json())
        elif re.fullmatch('/api/chat/[a-f0-9]{32}/stop', path) and method == 'POST':
            self.read_json()
            identifier = path.split('/')[3]
            with self.app.jobs_lock:
                job = self.app.jobs.get(identifier)
            if job:
                job.stop()
            self.send_data({'ok': True})
        else:
            raise UserError('没有找到这个操作。', 404)

    def event(self, event, data):
        packet = f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode('utf-8')
        self.wfile.write(packet)
        self.wfile.flush()

    def control_proposal(self, config, payload, run):
        # Only the request thread writes SSE. A bounded worker lets it detect a
        # disconnected page and service Stop while the provider is thinking.
        complete = threading.Event()
        outcome = {}

        def request():
            try:
                with httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=False) as client:
                    with run.lock:
                        if run.cancel.is_set():
                            return
                        run.response = client
                    with client.stream('POST', config['endpoint'] + '/chat/completions',
                                       headers={'Authorization': 'Bearer ' + config['api_key']}, json=payload) as response:
                        with run.lock:
                            run.response = response
                        if run.cancel.is_set():
                            return
                        raw = bytearray()
                        for part in response.iter_bytes(chunk_size=16 * 1024):
                            if run.cancel.is_set():
                                return
                            raw.extend(part)
                            if len(raw) > 128 * 1024:
                                raise ControlError('模型返回内容过大，已停止电脑操作。')
                        if response.status_code >= 400:
                            raise UserError(upstream_error(response.status_code, raw.decode('utf-8', 'replace'), config['api_key']))
                        try:
                            result = json.loads(raw)
                            if not isinstance(result, dict):
                                raise ValueError()
                            if result.get('error'):
                                raise UserError(upstream_error(400, json.dumps(result), config['api_key']))
                            content = result['choices'][0]['message']['content']
                            if isinstance(content, list):
                                content = ''.join(item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text')
                            if not isinstance(content, str) or not content.strip():
                                raise ValueError()
                            outcome['content'] = content
                        except (ValueError, KeyError, IndexError, TypeError):
                            raise ControlError('模型没有返回有效的操作内容，请选择支持看图的聊天模型。')
            except Exception as error:
                outcome['error'] = error
            finally:
                with run.lock:
                    run.response = None
                complete.set()

        threading.Thread(target=request, name='GrokDesk-control-model', daemon=True).start()
        heartbeat_at = time.monotonic()
        deadline = heartbeat_at + 150
        while not complete.wait(0.2):
            if run.cancel.is_set():
                return None
            current = time.monotonic()
            if current > deadline:
                raise ControlError('等待模型操作超时，请检查连接后重新开始任务。')
            if current - heartbeat_at >= 5:
                self.event('heartbeat', {})
                heartbeat_at = current
        if run.cancel.is_set():
            return None
        if 'error' in outcome:
            raise outcome['error']
        return outcome.get('content')

    def control(self, body):
        task, mode = body.get('task'), body.get('mode')
        max_steps = body.get('max_steps', DEFAULT_STEPS)
        if not isinstance(task, str) or not task.strip() or len(task) > 8000:
            raise UserError('请输入 1–8000 字的电脑操作任务。')
        if mode not in ('confirm', 'auto'):
            raise UserError('请选择逐步确认或完全权限模式。')
        if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS:
            raise UserError(f'任务步数需为 1–{MAX_STEPS} 的整数。')
        config = self.app.settings.credentials()
        model = body.get('model') or config.get('model')
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise UserError('请选择支持看图的聊天模型。')
        task, model = task.strip(), model.strip()
        identifier = uuid.uuid4().hex
        run = ControlRun(mode, max_steps)
        with self.app.control_lock:
            if self.app.control_runs:
                raise UserError('已有电脑操作任务在运行，请先停止当前任务。', 409)
            self.app.control_runs[identifier] = run
        started, steps, status, error, summary = False, 0, 'limit', '', '已达到本次任务步数上限。'
        history = []
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.close_connection = True
            started = True
            self.event('start', {'run_id': identifier, 'task': task, 'mode': mode, 'max_steps': max_steps})
            # Let the user bring their target window forward before capturing.
            if run.cancel.wait(3):
                status, summary = 'stopped', '任务已停止。'
                return
            while steps < max_steps:
                if run.cancel.is_set():
                    status, summary = 'stopped', '任务已停止。'
                    break
                screen = self.app.controller.capture()
                self.event('screen', {'url': screen.data_url, 'width': screen.width, 'height': screen.height, 'step': steps + 1})
                payload = {'model': model, 'messages': control_messages(task, screen, history),
                           'stream': False, 'temperature': 0.1, 'max_tokens': 1600}
                content = self.control_proposal(config, payload, run)
                if run.cancel.is_set():
                    status, summary = 'stopped', '任务已停止。'
                    break
                action = parse_action(content, screen.width, screen.height)
                if action['type'] == 'done':
                    status, summary = 'complete', action['summary']
                    break
                action_id = run.pending(action)
                self.event('action', {'action_id': action_id, 'action': action,
                                     'step': steps + 1, 'requires_approval': mode == 'confirm'})
                if mode == 'confirm':
                    # Inference time also counts toward screenshot freshness.
                    deadline = min(time.monotonic() + 175, screen.captured_at + 175)
                    heartbeat_at = time.monotonic()
                    while not run.decision_ready.wait(0.2):
                        current = time.monotonic()
                        if current >= deadline:
                            raise ControlError('等待确认已超时，当前截图操作已失效。请重新开始任务。')
                        if current - heartbeat_at >= 5:
                            self.event('heartbeat', {})
                            heartbeat_at = current
                    if not run.consume(action_id):
                        status, summary = 'stopped', '操作已取消，任务已停止。'
                        break
                if run.cancel.is_set():
                    status, summary = 'stopped', '任务已停止。'
                    break
                # Flush immediately before input. A lost page must never leave
                # an approved or automatic action running in the background.
                self.event('heartbeat', {})
                if run.cancel.wait(0.15) or not self.app.controller.execute(action, screen, run.cancel):
                    status, summary = 'stopped', '任务已停止。'
                    break
                steps += 1
                history.append(action)
                self.event('executed', {'action_id': action_id, 'action': action, 'step': steps})
                if run.cancel.wait(0.6):
                    status, summary = 'stopped', '任务已停止。'
                    break
        except Exception as exception:
            if not started:
                raise
            if run.cancel.is_set() or isinstance(exception, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                status, summary = 'stopped', '任务已停止。'
            else:
                status, summary = 'error', '电脑操作未完成。'
                if isinstance(exception, (UserError, ControlError)):
                    error = str(exception)
                elif isinstance(exception, httpx.TimeoutException):
                    error = '等待模型超时，请检查网络后重新开始任务。'
                elif isinstance(exception, httpx.HTTPError):
                    error = '模型连接中断，请检查网络、API 地址与模型权限。'
                else:
                    error = '电脑操作遇到错误，任务已停止。请重试或更换支持看图的模型。'
        finally:
            run.stop()
            if started:
                try:
                    self.event('done', {'status': status, 'error': error, 'steps': steps, 'summary': summary})
                except (OSError, ValueError):
                    pass
            with self.app.control_lock:
                self.app.control_runs.pop(identifier, None)

    def generate_images(self, body):
        config = self.app.settings.credentials()
        conversation_id = str(body.get('conversation_id', ''))
        conversation = self.app.db.conversation(conversation_id)
        if body.get('attachments'):
            raise UserError('当前生图功能根据文字描述生成图片。请先移除附件；分析附件可以切回聊天。')
        model = str(body.get('model') or config['image_model']).strip()
        prompt = str(body.get('text', '')).strip()
        size = body.get('image_size', config['image_size'])
        count = body.get('image_count', config['image_count'])
        if body.get('regenerate'):
            last = conversation['messages'][-1] if conversation['messages'] else None
            if not last or last['kind'] != 'image' or last['role'] != 'assistant':
                raise UserError('没有可以重新生成的图片。')
            original = next((item for item in reversed(conversation['messages'][:-1]) if item['role'] == 'user' and item['kind'] == 'image'), None)
            if not original:
                raise UserError('没有找到原始图片描述。')
            prompt = original['content']
            model = original['model']
            size = original['generation'].get('size', 'auto')
            count = original['generation'].get('count', 1)
        payload = self.app.image_payload(model, prompt, size, count)
        generation = {'model': model, 'size': size, 'count': count}
        assistant_id, user_id = uuid.uuid4().hex, uuid.uuid4().hex
        job = Job(conversation_id)
        with self.app.jobs_lock:
            if any(active.conversation_id == conversation_id for active in self.app.jobs.values()):
                raise UserError('这段对话正在生成，请等待完成或停止。', 409)
            self.app.jobs[assistant_id] = job
        started = False
        content, error, status, usage, saved = '', '', 'complete', {}, 0
        try:
            with self.app.db.connect() as db:
                # Keep earlier pictures when regenerating, even if the next request fails.
                for identifier, role, text, state in [(user_id, 'user', prompt, 'complete'), (assistant_id, 'assistant', '', 'streaming')]:
                    db.execute('INSERT INTO messages(id,conversation_id,role,content,status,created_at,model,kind,generation) VALUES (?,?,?,?,?,?,?,?,?)', (identifier, conversation_id, role, text, state, now(), model, 'image', json.dumps(generation)))
                if not conversation['messages']:
                    db.execute('UPDATE conversations SET title=? WHERE id=?', (prompt.replace('\n', ' ')[:36], conversation_id))
                db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (now(), conversation_id))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            started = True
            self.event('start', {'assistant_id': assistant_id, 'conversation': self.app.db.conversation(conversation_id)})
            with httpx.Client(timeout=httpx.Timeout(300, connect=20), follow_redirects=False) as client:
                job.response = client
                if not job.cancel.is_set():
                    with client.stream('POST', config['endpoint'] + '/images/generations', headers={'Authorization': 'Bearer ' + config['api_key']}, json=payload) as response:
                        job.response = response
                        raw = bytearray()
                        for part in response.iter_bytes():
                            if job.cancel.is_set():
                                break
                            raw.extend(part)
                            if len(raw) > MAX_GENERATED_IMAGE * count * 4 // 3 + 1024 * 1024:
                                raise UserError('服务返回的图片数据过大。')
                        if not job.cancel.is_set():
                            if response.status_code >= 400:
                                raise UserError(upstream_error(response.status_code, raw.decode('utf-8', 'replace'), config['api_key']) + '\n请确认密钥分组包含所选生图模型。')
                            try:
                                result = json.loads(raw)
                            except ValueError:
                                raise UserError('生图接口没有返回有效 JSON，请检查 API 地址与生图模型。')
                            if not isinstance(result, dict):
                                raise UserError('生图接口返回的内容格式不正确。')
                            if result.get('error'):
                                raise UserError(upstream_error(400, json.dumps(result), config['api_key']))
                            data = result.get('data')
                            if not isinstance(data, list) or not data or len(data) > 4:
                                raise UserError('服务没有返回图片，请确认该模型支持 /images/generations 接口。')
                            usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
                    if not job.cancel.is_set():
                        for item in data:
                            if job.cancel.is_set():
                                break
                            if not isinstance(item, dict):
                                raise UserError('服务返回的图片数据格式不正确。')
                            image = self.app.image_bytes(item, job, client)
                            if job.cancel.is_set():
                                break
                            dimensions = self.app.store_generated(image, saved + 1, assistant_id)
                            saved += 1
                            generation.setdefault('images', []).append(dimensions)
                            content = f'已生成 {saved} 张图片。'
                            self.app.db.execute('UPDATE messages SET content=?,generation=? WHERE id=?', (content, json.dumps(generation), assistant_id))
                            self.event('progress', {'conversation': self.app.db.conversation(conversation_id)})
            if job.cancel.is_set():
                status = 'stopped'
                error = '已停止等待，已收到的图片会保留。服务商已开始的任务仍可能计费。'
        except Exception as exception:
            if not started:
                raise
            if job.cancel.is_set():
                status, error = 'stopped', '已停止等待，已收到的图片会保留。服务商已开始的任务仍可能计费。'
            else:
                status = 'error'
                if isinstance(exception, UserError):
                    error = str(exception)
                elif isinstance(exception, httpx.TimeoutException):
                    error = '等待生图超时。请先检查服务商任务记录，避免直接重试产生重复费用。'
                elif isinstance(exception, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                    error = '页面连接中断。已经收到的图片已保存，请重新打开这段对话查看。'
                else:
                    error = '图片请求或下载中断。已有图片已保存，请检查服务商任务记录后再决定是否重试。'
        finally:
            if started:
                self.app.db.execute('UPDATE messages SET content=?,status=?,error=?,usage=?,generation=? WHERE id=?', (content, status, error, json.dumps(usage), json.dumps(generation), assistant_id))
                self.app.db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (now(), conversation_id))
                try:
                    self.event('done', {'assistant_id': assistant_id, 'status': status, 'error': error, 'usage': usage})
                except (OSError, ValueError):
                    pass
            with self.app.jobs_lock:
                self.app.jobs.pop(assistant_id, None)

    def chat(self, body):
        config = self.app.settings.credentials()
        model = str(body.get('model') or config['model']).strip()
        if not model:
            raise UserError('请先获取模型列表，并选择一个模型。')
        conversation_id = str(body.get('conversation_id', ''))
        conversation = self.app.db.conversation(conversation_id)
        text = str(body.get('text', '')).strip()
        ids = body.get('attachments', [])
        if not isinstance(ids, list) or len(ids) > 8 or len(set(ids)) != len(ids):
            raise UserError('每条消息最多添加 8 个不同附件。')
        if len(text) > 120_000:
            raise UserError('消息太长，请分段发送。')
        assistant_id = uuid.uuid4().hex
        job = Job(conversation_id)
        with self.app.jobs_lock:
            if any(active.conversation_id == conversation_id for active in self.app.jobs.values()):
                raise UserError('这段对话正在生成回复，请等待完成或停止。', 409)
            self.app.jobs[assistant_id] = job
        content, usage, error, status = '', {}, '', 'complete'
        streaming_started = False
        try:
            if body.get('regenerate'):
                if not conversation['messages']:
                    raise UserError('还没有可以重新生成的消息。')
                last = conversation['messages'][-1]
                if last['kind'] == 'image':
                    raise UserError('请在生图模式中重新生成图片。')
                if last['role'] == 'assistant':
                    self.app.db.execute('DELETE FROM messages WHERE id=?', (last['id'],))
            else:
                if not text and not ids:
                    raise UserError('请输入消息或添加附件。')
                attachments = []
                for identifier in ids:
                    record = self.app.db.query('SELECT * FROM attachments WHERE id=? AND message_id IS NULL', (identifier,), True)
                    if not record:
                        raise UserError('附件已使用或不存在，请重新添加。')
                    attachments.append(record)
                if sum(len(record['text']) for record in attachments) > 160_000:
                    raise UserError('这次附件的文字总量太大，请分批发送。')
                user_id = uuid.uuid4().hex
                with self.app.db.connect() as db:
                    db.execute('INSERT INTO messages(id,conversation_id,role,content,created_at,model) VALUES (?,?,?,?,?,?)', (user_id, conversation_id, 'user', text, now(), model))
                    for identifier in ids:
                        db.execute('UPDATE attachments SET message_id=? WHERE id=?', (user_id, identifier))
                    if not conversation['messages']:
                        title = (text or attachments[0]['name']).replace('\n', ' ')[:36]
                        db.execute('UPDATE conversations SET title=? WHERE id=?', (title, conversation_id))
            conversation = self.app.db.conversation(conversation_id)
            payload = {'model': model, 'messages': self.app.api_messages(conversation, config), 'stream': True, 'temperature': config['temperature'], 'max_tokens': config['max_tokens']}
            self.app.db.execute('INSERT INTO messages(id,conversation_id,role,status,created_at,model) VALUES (?,?,?,?,?,?)', (assistant_id, conversation_id, 'assistant', 'streaming', now(), model))
            self.app.db.execute('UPDATE conversations SET updated_at=?, model=? WHERE id=?', (now(), model, conversation_id))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            streaming_started = True
            self.close_connection = True
            self.event('start', {'assistant_id': assistant_id, 'conversation': self.app.db.conversation(conversation_id)})
            saved_at = time.monotonic()
            with httpx.Client(timeout=httpx.Timeout(150, connect=20), follow_redirects=False) as client:
                with client.stream('POST', config['endpoint'] + '/chat/completions', headers={'Authorization': 'Bearer ' + config['api_key'], 'Content-Type': 'application/json'}, json=payload) as response:
                    job.response = response
                    if job.cancel.is_set():
                        status = 'stopped'
                    elif response.status_code >= 400:
                        raise UserError(upstream_error(response.status_code, response.read().decode('utf-8', 'replace'), config['api_key']))
                    elif 'text/event-stream' not in response.headers.get('content-type', ''):
                        try:
                            result = json.loads(response.read())
                            if result.get('error'):
                                raise UserError(upstream_error(400, json.dumps(result), config['api_key']))
                            content = result['choices'][0]['message'].get('content') or ''
                            usage = result.get('usage', {})
                            self.event('delta', {'text': content})
                        except (ValueError, KeyError, IndexError, TypeError):
                            raise UserError('服务没有返回有效回复，请检查 API 地址和模型。')
                    else:
                        for line in response.iter_lines():
                            if job.cancel.is_set():
                                status = 'stopped'
                                break
                            if not line.startswith('data:'):
                                continue
                            data = line[5:].strip()
                            if data == '[DONE]':
                                break
                            try:
                                chunk = json.loads(data)
                            except ValueError:
                                continue
                            if chunk.get('error'):
                                raise UserError(upstream_error(400, data, config['api_key']))
                            if chunk.get('usage'):
                                usage = chunk['usage']
                            choices = chunk.get('choices') or []
                            if choices:
                                delta = choices[0].get('delta', {}).get('content') or ''
                                if isinstance(delta, list):
                                    delta = ''.join(part.get('text', '') for part in delta if isinstance(part, dict))
                                if delta:
                                    content += delta
                                    self.event('delta', {'text': delta})
                            if time.monotonic() - saved_at > 0.5:
                                self.app.db.execute('UPDATE messages SET content=? WHERE id=?', (content, assistant_id))
                                saved_at = time.monotonic()
            if job.cancel.is_set():
                status = 'stopped'
            if not content and status != 'stopped':
                raise UserError('模型返回了空回复。请重试，或增加最大回复长度。')
        except Exception as exception:
            if not streaming_started:
                raise
            if job.cancel.is_set() or isinstance(exception, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                status = 'stopped'
            else:
                status = 'error'
                if isinstance(exception, UserError):
                    error = str(exception)
                elif isinstance(exception, httpx.TimeoutException):
                    error = '等待回复超时。已有内容已保存，可以重试。'
                else:
                    error = '连接中断或模型服务不可用。已有内容已保存，可以重试。'
        finally:
            if streaming_started:
                self.app.db.execute('UPDATE messages SET content=?,status=?,error=?,usage=? WHERE id=?', (content, status, error, json.dumps(usage), assistant_id))
                self.app.db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (now(), conversation_id))
                try:
                    self.event('done', {'assistant_id': assistant_id, 'status': status, 'error': error, 'usage': usage})
                except (OSError, ValueError):
                    pass
            with self.app.jobs_lock:
                self.app.jobs.pop(assistant_id, None)


def create_server(data_dir, port=0):
    server = Server(('127.0.0.1', port), Handler)
    try:
        server.app = Application(data_dir)
    except BaseException:
        server.server_close()
        raise
    return server


def default_data_directory():
    local = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'GrokDesk'
    program = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    portable = program / 'GrokDeskData'
    # Preserve an existing portable installation's history and encrypted key.
    # Only inspect marker paths; do not open settings or database contents here.
    has_portable_data = any((portable / name).is_file() for name in ('history.sqlite3', 'settings.json'))
    candidates = [portable, local] if has_portable_data else [local, portable]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ('.write-probe-' + uuid.uuid4().hex)
            probe.write_bytes(b'')
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise RuntimeError('没有可写的数据目录，请把程序放到可写文件夹后重试。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Grok Desk 本地桌面聊天软件')
    parser.add_argument('--port', type=int, default=18791)
    parser.add_argument('--data-dir', default='')
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--no-window', action='store_true')
    args = parser.parse_args(argv)
    if not args.data_dir:
        args.data_dir = str(default_data_directory())
    instance, server, thread, log_handler = None, None, None, None
    logger = logging.getLogger('grokdesk.desktop')
    try:
        if os.name == 'nt' and not args.no_window:
            instance = DesktopInstance(args.data_dir)
            if not instance.is_primary:
                instance.activate(browser=args.browser)
                return
        folder = Path(args.data_dir)
        folder.mkdir(parents=True, exist_ok=True)
        log_handler = RotatingFileHandler(folder / 'startup.log', maxBytes=200_000, backupCount=2, encoding='utf-8')
        log_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(log_handler)
        logger.setLevel(logging.INFO)
        try:
            server = create_server(args.data_dir, args.port)
        except OSError as error:
            if args.port != 18791 or error.errno not in (errno.EADDRINUSE, 10048):
                raise
            server = create_server(args.data_dir, 0)
        url = f'http://127.0.0.1:{server.server_port}'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info('Grok Desk %s service ready at %s', APP_VERSION, url)
        if sys.stdout:
            print(f'{APP_NAME}: {url}', flush=True)
        if not args.no_window and not args.browser:
            logger.info('Opening independent desktop window (WebView2)')
            run_desktop_window(url, server.app.folder, server.app.shutdown_requested, instance=instance)
            return
        launchers = []

        def show_window(browser=False, restore=False):
            # Chromium may hand off to another process and exit immediately.
            # Its lifetime must never determine the lifetime of this service.
            try:
                launcher = open_window(url, server.app.folder, browser=browser, restore=restore)
            except Exception:
                logger.exception('Could not open a window; local service remains available at %s', url)
                return
            if launcher is not None:
                launchers.append(launcher)
            logger.info('Window requested (%s), launcher pid=%s', 'browser' if browser else 'desktop', getattr(launcher, 'pid', 'shell'))

        if not args.no_window:
            show_window(browser=True)
        while not server.app.shutdown_requested.wait(0.2):
            if instance:
                for mode in instance.pending():
                    show_window(browser=True, restore=True)
            # Reap finished launchers without waiting for or closing browser windows.
            for process in launchers[:]:
                status = process.poll()
                if status is not None:
                    launchers.remove(process)
                    logger.info('Browser launcher exited with status %s; local service remains active', status)
                    if status != 0:
                        show_window(browser=True)
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception('Local service failed')
        raise
    finally:
        if server:
            with server.app.control_lock:
                controls = list(server.app.control_runs.values())
            for run in controls:
                run.stop()
            with server.app.jobs_lock:
                jobs = list(server.app.jobs.values())
            for job in jobs:
                job.stop()
            for _ in range(30):
                with server.app.jobs_lock:
                    if not server.app.jobs:
                        break
                time.sleep(0.1)
            if thread and thread.is_alive():
                server.shutdown()
                thread.join(timeout=3)
            server.server_close()
            logger.info('Local service stopped')
        if instance:
            instance.close()
        if log_handler:
            logger.removeHandler(log_handler)
            log_handler.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        if os.name == 'nt' and getattr(sys, 'frozen', False):
            ctypes.windll.user32.MessageBoxW(None, 'Grok Desk 启动失败。请查看数据目录中的 startup.log，或尝试「浏览器备用启动.vbs」。', APP_NAME, 16)
        else:
            raise
