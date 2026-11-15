# Standalone patch-only EXE: no ROM, translation editor or emulator included.
from pathlib import Path
import os
import json

root = Path(SPECPATH).parent
default_payload = root / 'payload' if (root / 'payload/manifest.json').is_file() else root / 'release/v0.81/payload'
payload = Path(os.environ.get('GUNDAM_PAYLOAD_DIR', str(default_payload)))
release_version = json.loads((payload / 'manifest.json').read_text('utf-8'))['version']
a = Analysis(
    [str(root / 'tools/release_patcher.py')],
    pathex=[], binaries=[(str(payload / 'chdman.exe'), 'payload')],
    datas=[(str(payload / 'manifest.json'), 'payload'), (str(payload / 'sectors.bin.zlib'), 'payload'), (str(payload / 'THIRD_PARTY_NOTICES.md'), 'payload'), (str(payload / 'licenses'), 'payload/licenses')],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='DC-Gundam-Side-Story-0079-Kr-Patcher-v'+release_version,
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    version=str(root / 'tools/patcher-version.txt'),
)
