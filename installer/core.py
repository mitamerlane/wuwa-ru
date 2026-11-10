"""Installer logic without any GUI: safe extraction, shortcuts, uninstall entry.

The installer only copies the application itself. It never touches the game
installation, never starts the game and never requires administrator rights.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

APP_NAME = 'WuWa Russian Manager'
APP_EXE = 'WuWaRussian.exe'
UNINSTALLER = 'Uninstall.exe'
MANIFEST = '.install-manifest.json'
DEFAULT_SUBDIR = 'WuWaRussian'
REG_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\WuWaRussianManager'
MAX_PAYLOAD_BYTES = 4 * 1024 ** 3
SEED = 'seed'  # payload folder with starting user data, copied only where nothing exists yet
USER_DATA = ('Data', 'Projects', 'Backups', 'Exports', 'Imports', 'Logs')  # removed only on request
INSTALLED_RECORD = ('Data', 'Native', 'installed.json')  # the launcher's record of a PAK in the game

Progress = Callable[[int, int, str], None]


class InstallError(Exception):
    """User-presentable (Russian) installation failure."""


class Cancelled(InstallError):
    pass


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / name


def default_target() -> Path:
    local = os.environ.get('LOCALAPPDATA') or str(Path.home())
    return Path(local) / 'Programs' / DEFAULT_SUBDIR


def read_manifest(target: Path) -> dict | None:
    try:
        return json.loads((target / MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def resolve_target(raw: str) -> Path:
    """Validate the chosen folder; non-empty foreign folders get a subfolder."""
    text = (raw or '').strip().strip('"')
    if not text:
        raise InstallError('Укажи папку для установки.')
    path = Path(text)
    if not path.is_absolute():
        raise InstallError('Нужен полный путь, например C:\\Games\\WuWaRussian.')
    if path.parent == path:
        raise InstallError('Нельзя устанавливать в корень диска — выбери папку.')
    path = Path(os.path.normpath(path))
    if path.exists():
        if not path.is_dir():
            raise InstallError('По этому пути лежит файл, а не папка.')
        if any(path.iterdir()) and read_manifest(path) is None:
            path = path / DEFAULT_SUBDIR
            if path.exists() and any(path.iterdir()) and read_manifest(path) is None:
                raise InstallError('Папка не пуста и не похожа на прошлую установку. '
                                   'Выбери другую.')
    return path


def update_target(raw: str) -> Path:
    """`--update DIR` from the launcher: only a folder this installer already installed into."""
    target = Path((raw or '').strip().strip('"')).expanduser()
    if not target.is_absolute() or not target.is_dir():
        raise InstallError('Папка для обновления не найдена.')
    manifest = read_manifest(target)
    if not isinstance(manifest, dict) or manifest.get('app') != APP_NAME:
        raise InstallError('В этой папке нет установленного WuWa Russian — обновлять нечего.')
    return target.resolve()


def wait_for_exit(pid: int, timeout: float) -> bool:
    """True once process `pid` has exited (or never existed); False if still running at `timeout`."""
    if pid <= 0:
        return True
    if sys.platform != 'win32':
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.1)
        return False
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout * 1000)) == 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def check_writable(target: Path) -> None:
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / '.write-test'
        probe.write_bytes(b'ok')
        probe.unlink()
    except OSError as exc:
        raise InstallError(f'В эту папку нельзя записывать ({exc.strerror or exc}). '
                           'Выбери папку внутри профиля или на другом диске.') from exc


def _safe_member(name: str) -> PurePosixPath:
    p = PurePosixPath(name.replace('\\', '/'))
    if p.is_absolute() or '..' in p.parts or (p.parts and ':' in p.parts[0]):
        raise InstallError(f'Повреждённый пакет: небезопасный путь «{name}».')
    return p


def payload_info(payload: Path) -> dict:
    with zipfile.ZipFile(payload) as zf:
        try:
            return json.loads(zf.read('payload.json').decode('utf-8'))
        except (KeyError, ValueError) as exc:
            raise InstallError('Пакет установки повреждён (нет payload.json).') from exc


def payload_size(payload: Path) -> int:
    with zipfile.ZipFile(payload) as zf:
        return sum(i.file_size for i in zf.infolist() if not i.is_dir())


def install(payload: Path, target: Path, progress: Progress | None = None,
            is_cancelled: Callable[[], bool] = lambda: False,
            installer_copy: Path | None = None) -> dict:
    progress = progress or (lambda *_: None)
    check_writable(target)
    old = read_manifest(target) or {}
    with zipfile.ZipFile(payload) as zf:
        members = [i for i in zf.infolist() if not i.is_dir() and i.filename != 'payload.json']
        total = sum(i.file_size for i in members)
        if total > MAX_PAYLOAD_BYTES:
            raise InstallError('Пакет слишком большой, установка отменена.')
        free = shutil.disk_usage(target).free
        if free < total * 1.1:
            raise InstallError(f'Мало места на диске: нужно ≈{total // 2**20 + 1} МБ, '
                               f'свободно {free // 2**20} МБ.')
        written: list[str] = []
        seeded: list[str] = []
        done = 0
        try:
            for info in members:
                if is_cancelled():
                    raise Cancelled('Установка отменена.')
                rel = _safe_member(info.filename)
                seed = rel.parts[0] == SEED and len(rel.parts) > 1
                if seed:
                    # Starting user data (ready translation): written only when absent,
                    # never listed in the manifest, so upgrades and uninstall leave it alone.
                    rel = PurePosixPath(*rel.parts[1:])
                dest = target.joinpath(*rel.parts)
                done += info.file_size
                if seed and dest.exists():
                    progress(done, total, rel.as_posix())
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + '.part')
                with zf.open(info) as src, open(tmp, 'wb') as out:
                    shutil.copyfileobj(src, out, 1024 * 256)
                os.replace(tmp, dest)
                (seeded if seed else written).append(rel.as_posix())
                progress(done, total, rel.as_posix())
        except BaseException:
            for rel in written:
                if rel not in old.get('files', []):
                    (target / rel).unlink(missing_ok=True)
            for rel in seeded:
                (target / rel).unlink(missing_ok=True)
            raise
    # Remove files that belonged to the previous version only (never user data).
    for rel in set(old.get('files', [])) - set(written):
        try:
            _safe_member(rel)
            (target / rel).unlink(missing_ok=True)
        except (InstallError, OSError):
            pass
    info = payload_info(payload)
    if installer_copy and installer_copy.is_file():
        shutil.copy2(installer_copy, target / UNINSTALLER)
        written.append(UNINSTALLER)
    manifest = {'schema_version': 1, 'app': APP_NAME, 'version': info.get('version'),
                'files': sorted(set(written)),
                'seeded': sorted(set(seeded) | set(old.get('seeded', [])))}
    (target / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                   encoding='utf-8')
    return manifest


def _ps(script: str) -> None:
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                    '-Command', script], check=True, capture_output=True,
                   creationflags=flags, timeout=60)


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _shortcut(link: Path, target_exe: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    _ps('$s=(New-Object -ComObject WScript.Shell).CreateShortcut(' + _q(link) + ');'
        '$s.TargetPath=' + _q(target_exe) + ';$s.WorkingDirectory=' + _q(target_exe.parent) +
        ';$s.IconLocation=' + _q(Path(f'{target_exe},0')) + ';$s.Save()')


def refresh_shell_icons(paths: list[Path]) -> None:
    """An EXE overwritten in place keeps its old picture in Explorer's icon cache: tell the shell."""
    if sys.platform != 'win32':
        return
    import ctypes
    shell = ctypes.windll.shell32
    for path in paths:
        shell.SHChangeNotify(0x00002000, 0x0005, ctypes.c_wchar_p(str(path)), None)  # UPDATEITEM, PATHW
    shell.SHChangeNotify(0x08000000, 0x1000, None, None)  # ASSOCCHANGED, FLUSH


def desktop_dir() -> Path:
    return Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Desktop'


def start_menu_dir() -> Path:
    return (Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows'
            / 'Start Menu' / 'Programs')


def create_shortcuts(target: Path, desktop: bool, start_menu: bool) -> list[str]:
    made: list[str] = []
    exe = target / APP_EXE
    for wanted, link in ((desktop, desktop_dir() / f'{APP_NAME}.lnk'),
                         (start_menu, start_menu_dir() / f'{APP_NAME}.lnk')):
        if wanted:
            _shortcut(link, exe)
            made.append(str(link))
    return made


def register_uninstall(target: Path, version: str | None) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
        for name, value in (('DisplayName', APP_NAME), ('DisplayVersion', version or ''),
                            ('InstallLocation', str(target)),
                            ('DisplayIcon', str(target / APP_EXE)),
                            ('UninstallString', f'"{target / UNINSTALLER}" --uninstall'),
                            ('Publisher', 'WuWa Russian')):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, 'NoModify', 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, 'NoRepair', 0, winreg.REG_DWORD, 1)


def installed_location() -> Path | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            return Path(winreg.QueryValueEx(key, 'InstallLocation')[0])
    except OSError:
        return None


def is_uninstaller(exe: Path) -> bool:
    """Uninstall.exe (by its name) always opens the uninstall wizard, flag or not."""
    return exe.stem.casefold() == Path(UNINSTALLER).stem.casefold()


def uninstall_target(exe: Path, frozen: bool) -> Path | None:
    """The folder to clean: next to a frozen Uninstall.exe, else the registered installation."""
    if frozen and (exe.parent / MANIFEST).is_file():
        return exe.parent
    return installed_location()


def translation_in_game(target: Path) -> bool:
    """The launcher recorded a translation package in the game folder (it is not ours to delete here)."""
    return target.joinpath(*INSTALLED_RECORD).is_file()


def user_data_dirs(target: Path) -> list[Path]:
    """User data folders that really are folders here: links and junctions are never followed."""
    found = []
    for name in USER_DATA:
        path = target / name
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            continue
        if path.is_dir():
            found.append(path)
    return found


def uninstall(target: Path, progress: Progress | None = None, remove_data: bool = False) -> int:
    """Remove the files recorded in the manifest; user data only when `remove_data` is set."""
    progress = progress or (lambda *_: None)
    manifest = read_manifest(target)
    if manifest is None:
        raise InstallError('В этой папке нет записи об установке — удалять нечего.')
    files = manifest.get('files', [])
    data = user_data_dirs(target) if remove_data else []
    total = len(files) + len(data) + 1
    removed = 0
    for index, rel in enumerate(files, 1):
        progress(index, total, rel)
        try:
            p = _safe_member(rel)
        except InstallError:
            continue
        f = target.joinpath(*p.parts)
        if p.name == UNINSTALLER and getattr(sys, 'frozen', False):
            continue  # the running image cannot delete itself
        if f.is_file():
            try:
                f.unlink()
            except PermissionError as exc:
                raise InstallError('Файл занят: закрой WuWa Russian Manager и повтори '
                                   f'удаление ({f.name}).') from exc
            removed += 1
    for link in (desktop_dir() / f'{APP_NAME}.lnk', start_menu_dir() / f'{APP_NAME}.lnk'):
        link.unlink(missing_ok=True)
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
    except OSError:
        pass
    for index, path in enumerate(data, len(files) + 1):
        progress(index, total, path.name)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise InstallError(f'Не удалось удалить {path.name}: {exc.strerror or exc}. '
                               'Закрой WuWa Russian и повтори.') from exc
    (target / MANIFEST).unlink(missing_ok=True)
    progress(total, total, '')
    for d in sorted((x for x in target.rglob('*') if x.is_dir() and not x.is_symlink()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        target.rmdir()  # only when nothing is left; a frozen Uninstall.exe removes it after exit
    except OSError:
        pass
    return removed
