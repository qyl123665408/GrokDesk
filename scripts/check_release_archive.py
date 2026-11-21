"""Validate the portable release's layout and data exclusions, then hash it."""
import hashlib
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile


def check_archive(path):
    names = set()
    private = re.compile(
        r'(^|/)(GrokDeskData|attachments|desktop-profile|webview-profile|'
        r'test-output|\.venv|node_modules|\.git)(/|$)|'
        r'(^|/)(settings\.(json|tmp)(\..*)?|history\.sqlite3(-.*)?|'
        r'\.env(\..*)?|credentials\.json|secrets\.json)$', re.I)
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = item.filename.replace('\\', '/')
            parts = PurePosixPath(name).parts
            if name.startswith('/') or ':' in name or '..' in parts:
                raise ValueError('Unsafe release archive path')
            if private.search(name):
                raise ValueError('Private runtime data or development cache in release archive')
            if item.is_dir():
                continue
            if name.casefold() in names:
                raise ValueError('Duplicate release archive path')
            names.add(name.casefold())
        required = {'grokdesk.exe', '_internal/python311.dll',
                    '_internal/web/index.html', 'third_party/notices.txt'}
        if not required.issubset(names):
            raise ValueError('Incomplete portable desktop archive')
        if archive.testzip() is not None:
            raise ValueError('Corrupt release archive member')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    checksum = path.with_suffix(path.suffix + '.sha256')
    checksum.write_text(f'{digest}  {path.name}\n', encoding='ascii')
    print(f'Validated {len(names)} release files; SHA256 {digest}')
    return checksum


if __name__ == '__main__':
    check_archive(Path(sys.argv[1]))
