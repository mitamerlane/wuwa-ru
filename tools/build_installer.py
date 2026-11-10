"""Build the one-file installer: python tools/build_installer.py [--skip-app]

1. Freezes the application (packaging/WuWaRussian.spec) into build/dist.
2. Packs it with payload.json into build/installer/payload.zip.
3. Freezes installer/main.py (onefile) with the payload -> dist/WuWaRussian-Setup-<ver>.exe.
Run with an interpreter that has PySide6 and PyInstaller installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wuwaru import __version__  # noqa: E402


def build_app() -> Path:
    subprocess.run([sys.executable, '-m', 'PyInstaller', str(ROOT / 'packaging/WuWaRussian.spec'),
                    '--noconfirm', '--distpath', str(ROOT / 'build/dist'),
                    '--workpath', str(ROOT / 'build/work')], check=True, cwd=ROOT)
    return ROOT / 'build/dist/WuWaRussian'


def make_payload(app_dir: Path, out: Path, seed: Path | None = None, art: Path | None = None) -> None:
    """App files, plus `seed/` (ready translation) that the installer copies only where absent."""
    if not (app_dir / 'WuWaRussian.exe').is_file():
        raise SystemExit(f'{app_dir}/WuWaRussian.exe не найден')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr('payload.json', json.dumps({'schema_version': 1, 'version': __version__,
                                                'translation': seed is not None}))
        for f in sorted(app_dir.rglob('*')):
            rel = f.relative_to(app_dir)
            if f.is_file() and rel.parts[0] not in USER_DIRS:
                zf.write(f, rel.as_posix())
        if art is not None:  # launcher background when Steam has no cover cached
            zf.write(art, 'background.jpg')
        if seed is not None:
            for f in sorted(seed.rglob('*')):
                if f.is_file():
                    zf.write(f, 'seed/' + f.relative_to(seed).as_posix())


USER_DIRS = {'Data', 'Backups', 'Exports', 'Imports', 'Logs', 'Projects'}  # never from a dev build
STEAM_APP_ID = '3513350'
ART_LIMIT = 8 * 1024 * 1024


def steam_roots() -> list[Path]:
    roots = [os.environ.get('STEAM_DIR', ''),
             os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'Steam'),
             os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'Steam')]
    return [Path(r) for r in roots if r]


def find_key_art(roots: list[Path]) -> Path | None:
    """Newest library_hero.jpg of Wuthering Waves in the local Steam cache (read-only)."""
    best = None
    for root in roots:
        base = root / 'appcache' / 'librarycache' / STEAM_APP_ID
        if not base.is_dir() or base.is_symlink():
            continue
        for path in [base / 'library_hero.jpg', *base.glob('*/library_hero.jpg')]:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                st = path.stat()
            except OSError:
                continue
            if 0 < st.st_size <= ART_LIMIT and (best is None or st.st_mtime > best[0]):
                best = (st.st_mtime, path)
    return best[1] if best else None


def stage_background(explicit: str | None, out: Path) -> Path | None:
    """Copy the key art next to the payload; it is embedded in the EXE, never committed."""
    src = Path(explicit) if explicit else find_key_art(steam_roots())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    if src is None:
        print('Фон: кадр игры не найден, будет нарисованный фон (--background PATH)')
        return None
    if not src.is_file() or src.stat().st_size > ART_LIMIT:
        raise SystemExit(f'Фон {src}: файла нет или он больше 8 МиБ')
    shutil.copyfile(src, out)
    print(f'Фон: {src}')
    return out


ICONS = ROOT / 'packaging' / 'icons'


def freeze_wizard(name: str, icon: Path, distpath: Path, data: list[str]) -> Path:
    """One-file build of installer/main.py (setup or uninstaller) with its own EXE icon."""
    subprocess.run([sys.executable, '-m', 'PyInstaller', str(ROOT / 'installer/main.py'),
                    '--noconfirm', '--onefile', '--windowed', '--name', name, '--icon', str(icon),
                    '--paths', str(ROOT), *data,
                    '--exclude-module', 'PySide6.QtNetwork', '--exclude-module', 'PySide6.QtQml',
                    '--exclude-module', 'PySide6.QtQuick', '--exclude-module', 'PySide6.QtPdf',
                    '--exclude-module', 'PySide6.QtSvg', '--exclude-module', 'PySide6.QtOpenGL',
                    '--distpath', str(distpath), '--workpath', str(ROOT / 'build/installer-work'),
                    '--specpath', str(ROOT / 'build/installer-work')], check=True, cwd=ROOT)
    return distpath / f'{name}.exe'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--skip-app', action='store_true', help='use existing build/dist/WuWaRussian')
    ap.add_argument('--app-dir', help='frozen app folder to pack (default build/dist/WuWaRussian)')
    ap.add_argument('--background', help='key art JPG/PNG (default: Steam library hero of the game)')
    ap.add_argument('--bundle', help='ready translation from tools/build_translation_bundle.py '
                                     '(default build/bundle if present)')
    args = ap.parse_args()
    if args.app_dir:
        app_dir = Path(args.app_dir)
    else:
        app_dir = ROOT / 'build/dist/WuWaRussian' if args.skip_app else build_app()
    bundle = Path(args.bundle) if args.bundle else ROOT / 'build/bundle'
    seed = bundle / 'seed'
    if not seed.is_dir():
        if args.bundle:
            raise SystemExit(f'Нет готового перевода: {seed}')
        print('Перевод: build/bundle нет, установщик будет без готового перевода')
        seed = None
    else:
        print(f'Перевод: {seed}')
    payload = ROOT / 'build/installer/payload.zip'
    art = stage_background(args.background, ROOT / 'build/installer/background.jpg')
    make_payload(app_dir, payload, seed, art)
    art_data = ['--add-data', f'{art};.'] if art else []
    # 1) Uninstall.exe: the same wizard without payload; its name opens the uninstall flow.
    uninstaller = freeze_wizard('Uninstall', ICONS / 'uninstall.ico', ROOT / 'build/installer',
                                [*art_data, '--add-data', f"{ICONS / 'uninstall.ico'};."])
    # 2) Setup: payload, the uninstaller it installs, both window icons.
    name = f'WuWaRussian-Setup-{__version__}'
    exe = freeze_wizard(name, ICONS / 'setup.ico', ROOT / 'dist',
                        ['--add-data', f'{payload};.', '--add-data', f'{uninstaller};.', *art_data,
                         '--add-data', f"{ICONS / 'setup.ico'};.", '--add-data', f"{ICONS / 'uninstall.ico'};."])
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (ROOT / 'dist' / f'{name}.exe.sha256').write_text(f'{digest}  {exe.name}\n')
    print(f'OK {exe} ({exe.stat().st_size // 2**20} MiB) sha256={digest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
