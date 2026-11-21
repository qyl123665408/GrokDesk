import importlib.metadata
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
folder = root / 'third_party'
folder.mkdir(exist_ok=True)
notices = ['Grok Desk bundled third-party notices', 'Installed distributions and license files follow.']
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get('Name', 'unknown')
    if name.lower() in ('pip', 'setuptools', 'wheel'):
        continue
    notices.append(f'{name} {distribution.version}: ' + (distribution.metadata.get('License-Expression') or distribution.metadata.get('License') or 'See included license files.'))
    for item in distribution.files or []:
        if any(part.lower().startswith(('license', 'copying', 'notice')) for part in item.parts):
            source = Path(distribution.locate_file(item))
            if source.is_file():
                target = folder / name / Path(*item.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
for name in ['LICENSE-marked.md', 'LICENSE-dompurify.txt']:
    shutil.copy2(root / 'web' / 'vendor' / name, folder / name)
(folder / 'NOTICES.txt').write_text('\n\n'.join(notices), 'utf-8')
