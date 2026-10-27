"""Package a PyInstaller application and public configuration without user data."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent.parent
EXTRAS = ('README.md', 'LICENSE', 'profile.polymas.json')
FORBIDDEN_NAMES = {'data', 'diagnostics', '.local-browsers', 'profile.json',
                   '.env', '.venv', '__pycache__', 'chrome.exe', 'msedge.exe'}


def package(version: str, output_dir: Path, app_dir: Path) -> Path:
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('Version must look like v1.0.0 or v1.0.0-beta.1')
    app_dir = app_dir.resolve()
    if not (app_dir / 'ZhiHuiShuHelper.exe').is_file() or not (app_dir / '_internal').is_dir():
        raise FileNotFoundError('The PyInstaller application must be built before packaging')
    files = []
    for path in app_dir.rglob('*'):
        if path.is_symlink():
            raise ValueError('Symlinks are not allowed in release input')
        if path.is_file():
            rel = path.relative_to(app_dir)
            if any(part.casefold() in FORBIDDEN_NAMES for part in rel.parts) or path.suffix.lower() in {'.dpapi', '.sqlite', '.db'}:
                raise ValueError(f'Private data or bundled browser found: {rel}')
            files.append((path, rel.as_posix()))
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f'ZhiHuiShuHelper-{version}-windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path, name in sorted(files, key=lambda item: item[1]):
            bundle.write(path, f'ZhiHuiShuHelper/{name}')
        for name in EXTRAS:
            path = ROOT / name
            if path.is_symlink() or not path.is_file():
                raise FileNotFoundError(f'Missing or unsafe release input: {name}')
            bundle.write(path, f'ZhiHuiShuHelper/{name}')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', help='Release tag, for example v1.1.0')
    parser.add_argument('--app-dir', type=Path, required=True, help='PyInstaller onedir output')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    print(package(args.version, args.output, args.app_dir))
