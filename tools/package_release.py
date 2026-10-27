"""Create a clean Windows source distribution from an explicit file list."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    'README.md', 'LICENSE', 'requirements.txt',
    'desktop.py', 'mastery.py', 'private_state.py', 'runtime_control.py',
    'profile.polymas.json', 'profile.example.json',
    'Install.cmd', 'Setup.ps1', 'Start.cmd', 'Launch-Gui.ps1',
    'Start-CLI.cmd', 'Run.ps1',
)


def package(version: str, output_dir: Path) -> Path:
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('Version must look like v1.0.0 or v1.0.0-beta.1')
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f'ZhiHuiShuHelper-{version}-windows.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in FILES:
            path = ROOT / name
            if path.is_symlink() or not path.is_file():
                raise FileNotFoundError(f'Missing or unsafe release input: {name}')
            bundle.write(path, f'ZhiHuiShuHelper/{name}')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', help='Release tag, for example v1.0.0')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    print(package(args.version, args.output))
