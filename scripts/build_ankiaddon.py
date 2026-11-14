"""Build dist/<package>.ankiaddon for AnkiWeb or manual install.

Usage: python scripts/build_ankiaddon.py

Only runtime files go in: no tests, caches, logs, or your meta.json
(which holds your tokens).
"""

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Files and folders that make up the add-on at runtime
INCLUDE = [
    '*.py',
    'model.css',
    'config.json',
    'config.md',
    'manifest.json',
    'LICENSE',
    'schemas/*.json',
]


def main() -> int:
    package = json.loads((ROOT / 'manifest.json').read_text())['package']
    files = sorted({p for pattern in INCLUDE for p in ROOT.glob(pattern)})
    if not any(p.name == '__init__.py' for p in files):
        print('error: __init__.py not found', file=sys.stderr)
        return 1
    out = ROOT / 'dist' / f'{package}.ankiaddon'
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(ROOT).as_posix())
    print(f'Built {out.relative_to(ROOT)} ({len(files)} files)')
    for path in files:
        print(f'  {path.relative_to(ROOT).as_posix()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
