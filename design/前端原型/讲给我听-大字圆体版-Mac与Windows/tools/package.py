"""Package only the portable static prototype, then verify and extract it."""
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

root = Path(__file__).resolve().parent.parent
files = [root / name for name in ['index.html', '打开静态原型.cmd', '打开静态原型-Mac.command', 'README.md', '给队友的说明.md', '视觉调整说明.md', 'package.json']]
for folder in ['src', 'assets']:
    files.extend(p for p in (root / folder).rglob('*') if p.is_file())
files.extend(root / 'tools' / name for name in ['acceptance.cjs', 'smoke.cjs', 'repair-regression.cjs', 'link-audit.cjs', 'visual-check.cjs', 'visual-gallery.cjs', 'prepare.cjs', 'package.py'])
files.extend(root / 'output' / name for name in ['acceptance.json', 'repair-regression.json', 'link-audit.json', 'link-inventory.json', '链接检查说明.md', '首页.png', '我的教室.png', '笔记本视口.png'])
visual = root / 'output' / 'visual-refresh'
files.extend(visual / name for name in ['index.html', 'overview.html', 'verification.json', '六页总览.png', '笔记本-我的教室.png'])
files.extend(sorted((visual / 'png').glob('*.png')))
archive = root / '讲给我听-大字圆体版-Mac与Windows.zip'
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
    for source in files:
        name = source.relative_to(root).as_posix()
        if source.suffix == '.command':
            info = zipfile.ZipInfo.from_file(source, name)
            info.create_system = 3
            info.external_attr = 0o100755 << 16
            z.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        else:
            z.write(source, name)
temp = root / 'temp'
temp.mkdir(exist_ok=True)
extracted = Path(tempfile.mkdtemp(prefix='shared-', dir=temp))
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    assert len(z.namelist()) == len(files)
    launcher = z.getinfo('打开静态原型-Mac.command')
    assert launcher.create_system == 3
    assert (launcher.external_attr >> 16) & 0o777 == 0o755
    assert b'\r' not in z.read(launcher)
    for source in files:
        name = source.relative_to(root).as_posix()
        assert z.read(name) == source.read_bytes(), name
        assert (extracted / name).resolve().is_relative_to(extracted.resolve())
    z.extractall(extracted)
for source in files:
    assert (extracted / source.relative_to(root)).read_bytes() == source.read_bytes()
result = {'archive': str(archive), 'files': len(files), 'bytes': archive.stat().st_size,
          'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
          'extracted': str(extracted), 'all_bytes_verified': True}
(root / 'output' / 'package-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
print(json.dumps(result, ensure_ascii=False, indent=2))
