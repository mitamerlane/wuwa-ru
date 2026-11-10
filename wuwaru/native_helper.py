"""Elevated helper: executes one fixed write plan inside the found game installation.

The non-elevated application writes a request with `write_request` and starts this
module through UAC (`run_elevated`). The helper trusts nothing from the request: it
re-discovers the installation, re-checks processes, the Steam update state, the Mount
hash and version fingerprint, and the package hashes before any write. Only
`install_package`, `apply_mount` and `remove`, in the shapes produced by
`native_launch.plan_play` / `plan_remove`, are accepted.

`Data/Native` is writable by the non-elevated user, so every file the helper opens
there is opened first and then verified by handle (final path, regular file, one
link); the helper never renames or deletes anything in it. Inside the game folder
(Administrators only) the package is copied from the already-open staging handle into
a temp file the helper creates, verified and moved with `os.replace`.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

from wuwaru.discovery import STEAM_APP_ID, discover
from wuwaru.native_launch import (MOUNT, MOUNT_STEM, PAK_RELATIVE, accepted_fingerprints, fingerprint,
                                  game_processes, process_names, read_bounded, replace_atomic,
                                  update_guard, with_entry, without_entry)
from wuwaru.native_package import sha256
from wuwaru.paths import UnsafePath, inside, reject_links

REQUEST_LIMIT = 64 * 1024
PACKAGE_LIMIT = 1024 * 1024
BLOCK = 1024 * 1024
SEE_MASK_NOCLOSEPROCESS = 0x00000040
INFINITE = 0xFFFFFFFF
WAIT_FAILED = 0xFFFFFFFF
SW_HIDE = 0
ERROR_CANCELLED = 1223
_FLAGS = getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOINHERIT', 0)

SHAPES = (('apply_mount',), ('install_package', 'apply_mount'), ('remove',))
KEYS = {'install_package': {'op', 'staging', 'sha256', 'sha1'},
        'apply_mount': {'op', 'expected_sha256', 'entry'},
        'remove': {'op', 'pak_sha256', 'expected_sha256'}}
SHA256 = re.compile(r'[0-9a-f]{64}')
SHA1 = re.compile(r'[0-9A-F]{40}')
ENTRY = re.compile(re.escape(MOUNT_STEM) + r',900,([0-9A-F]{40}),,,')


def _matches(pattern: re.Pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _validate(operations) -> list[dict]:
    if not isinstance(operations, list) or not all(isinstance(op, dict) for op in operations):
        raise ValueError('План записи должен быть списком операций')
    names = tuple(op.get('op') for op in operations)
    if names not in SHAPES:
        raise ValueError(f'Неподдерживаемый план записи: {names!r}')
    for op in operations:
        if set(op) != KEYS[op['op']]:
            raise ValueError(f"Неожиданные поля операции {op['op']}: {sorted(op)}")
        if 'sha256' in op and not _matches(SHA256, op['sha256']):
            raise ValueError('Некорректный SHA-256 пакета')
        if 'expected_sha256' in op and not _matches(SHA256, op['expected_sha256']):
            raise ValueError('Некорректный SHA-256 Mount')
        if 'pak_sha256' in op and op['pak_sha256'] is not None and not _matches(SHA256, op['pak_sha256']):
            raise ValueError('Некорректный SHA-256 удаляемого пакета')
        if 'sha1' in op and not _matches(SHA1, op['sha1']):
            raise ValueError('Некорректный SHA-1 пакета')
        if 'staging' in op and not isinstance(op['staging'], str):
            raise ValueError('Некорректный путь staging')
        if 'entry' in op and not _matches(ENTRY, op['entry']):
            raise ValueError('Строка Mount не относится к пакету перевода')
    if names == ('install_package', 'apply_mount'):
        if ENTRY.fullmatch(operations[1]['entry']).group(1) != operations[0]['sha1']:
            raise ValueError('Строка Mount не совпадает с устанавливаемым пакетом')
    return operations


def _unique_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError('Повторяющиеся ключи в запросе')
    return dict(pairs)


def _parse(raw: bytes) -> dict:
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_keys)
    if not isinstance(data, dict) or set(data) != {'schema_version', 'game', 'operations'}:
        raise ValueError('Неожиданные поля запроса')
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ValueError(f"Неподдерживаемая версия схемы запроса: {data['schema_version']!r}")
    if not isinstance(data['game'], str) or not Path(data['game']).is_absolute():
        raise ValueError('Корень игры должен быть абсолютным путём')
    _validate(data['operations'])
    return data


def _write_atomic(path: Path, data: bytes) -> None:
    """Non-elevated writes only (request, Mount backup)."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _encode(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def write_request(native: Path, game: Path, operations: list[dict], mount: bytes) -> Path:
    """Runs in the non-elevated app. `mount` is the Mount the plan was made from."""
    _validate(operations)
    if not isinstance(mount, bytes):
        raise TypeError('mount must be bytes')
    digest = hashlib.sha256(mount).hexdigest()
    if any(op.get('expected_sha256', digest) != digest for op in operations):
        raise ValueError('Mount не совпадает с тем, по которому составлен план')
    original = without_entry(mount)
    native = Path(native)
    native.mkdir(parents=True, exist_ok=True)
    backup = native / 'mount-original.txt'
    if not (backup.is_file() and backup.read_bytes() == original):
        _write_atomic(backup, original)
    requests = native / 'requests'
    requests.mkdir(exist_ok=True)
    path = requests / f'{uuid.uuid4()}.json'
    _write_atomic(path, _encode({'schema_version': 1, 'game': str(Path(game).absolute()),
                                 'operations': operations}))
    return path


def _discovered_roots() -> list[Path]:
    return [item.root for item in discover().installations]


def _final_path(descriptor: int) -> str:
    """GetFinalPathNameByHandleW for an open descriptor: where the handle really points."""
    import msvcrt
    function = ctypes.WinDLL('kernel32', use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
    function.restype = ctypes.c_ulong
    handle = msvcrt.get_osfhandle(descriptor)
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = function(handle, buffer, size, 0)  # FILE_NAME_NORMALIZED | VOLUME_NAME_DOS
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        if length < size:
            return buffer.value
        size = length + 1


def _normal(path: str) -> str:
    if path.startswith('\\\\?\\UNC\\'):
        path = '\\\\' + path[8:]
    elif path.startswith('\\\\?\\'):
        path = path[4:]
    return os.path.normcase(os.path.abspath(path))


def _open_verified(path: Path, flags: int) -> int:
    """Open first, then prove the handle is the expected regular single-link file.

    A link check before the open alone is racy: a junction swapped in between would
    redirect the elevated open. Short (8.3) or substituted path spellings fail closed.
    """
    reject_links(path)
    descriptor = os.open(path, flags | _FLAGS, 0o644)
    try:
        if _normal(_final_path(descriptor)) != _normal(str(path)):
            raise UnsafePath(f'Путь подменён после проверки: {path}')
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsafePath(f'Ожидался обычный файл без жёстких ссылок: {path}')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view):]


def _read_verified(path: Path, limit: int) -> bytes:
    descriptor = _open_verified(path, os.O_RDONLY)
    try:
        chunks, size = [], 0
        while size <= limit:
            block = os.read(descriptor, limit + 1 - size)
            if not block:
                break
            chunks.append(block)
            size += len(block)
    finally:
        os.close(descriptor)
    if size > limit:
        raise ValueError(f'Слишком большой файл: {path.name}')
    return b''.join(chunks)


def _append_journal(native: Path, record: dict) -> None:
    descriptor = _open_verified(native / 'journal.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        _write_all(descriptor, (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8'))
    finally:
        os.close(descriptor)


def _located(request: Path) -> bool:
    return request.parent.name == 'requests' and request.suffix == '.json'


def _result_path(request: Path) -> Path:
    return request.with_name(request.stem + '.result.json')


def _reserve_result(request: Path) -> int:
    """Create `<uuid>.result.json` exclusively before anything else: a request runs once."""
    if not _located(request):
        raise ValueError('Запрос должен лежать в папке requests')
    uuid.UUID(request.stem)
    try:
        return _open_verified(_result_path(request), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        raise ValueError('Этот запрос уже выполнялся') from None


def _ready(game: Path, running, guard) -> None:
    active = running(game)
    if active:
        raise RuntimeError(f'Игра или лаунчер запущены ({process_names(active)}); запись в игру отменена')
    guard(game)


def _digests(descriptor: int) -> tuple[str, str]:
    full, short = hashlib.sha256(), hashlib.sha1()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while block := os.read(descriptor, BLOCK):
        full.update(block)
        short.update(block)
    return full.hexdigest(), short.hexdigest().upper()


def _copy(source: int, target: int) -> None:
    os.lseek(source, 0, os.SEEK_SET)
    while block := os.read(source, BLOCK):
        _write_all(target, block)
    os.fsync(target)


def _open_staging(native: Path, op: dict) -> int:
    given = Path(op['staging'])
    if not given.is_absolute() or given.suffix.casefold() != '.pak':
        raise ValueError('Пакет должен лежать в папке staging')
    expected = inside(native / 'staging', given.name)
    if _normal(str(given)) != _normal(str(expected)):
        raise ValueError('Пакет должен лежать в папке staging')
    descriptor = _open_verified(expected, os.O_RDONLY)
    try:
        opened, named = os.fstat(descriptor), os.lstat(expected)
        if stat.S_ISLNK(named.st_mode) or (opened.st_ino, opened.st_dev) != (named.st_ino, named.st_dev):
            raise UnsafePath('Пакет в staging подменён')
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _package(native: Path, name: str = 'package.json') -> dict | None:
    try:
        raw = _read_verified(native / name, PACKAGE_LIMIT)
    except FileNotFoundError:
        return None
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'Некорректный {name}')
    return data


def _removable(native: Path) -> tuple[list, tuple[dict | None, dict | None]]:
    """Hashes a remove request may name: package.json's or installed.json's; None only without both."""
    records = (_package(native), _package(native, 'installed.json'))
    allowed = [record.get('pak_sha256') for record in records if record is not None]
    return allowed or [None], records


def _check_ours(target: Path, pak_hash: str | None) -> None:
    if pak_hash is not None and target.exists():
        if not target.is_file() or sha256(target) != pak_hash:
            raise ValueError('Файл с именем пакета перевода не наш; удаление отменено')


def _sha1(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha1').hexdigest().upper()


def _install(staging: int, target: Path, op: dict, land) -> None:
    temporary = target.with_name(f'{target.name}.{uuid.uuid4().hex}.tmp')
    output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS)
    try:
        try:
            _copy(staging, output)
        finally:
            os.close(output)
        check = os.open(temporary, os.O_RDONLY | _FLAGS)
        try:
            copied = _digests(check)
        finally:
            os.close(check)
        if copied != (op['sha256'], op['sha1']):
            raise ValueError('Скопированный пакет не совпал по SHA-256/SHA-1; прежний пакет сохранён')
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    land({'op': 'install_package', 'write': 'pak', 'sha256': op['sha256']})
    if sha256(target) != op['sha256']:
        raise RuntimeError('Установленный пакет не совпал по SHA-256')


def _apply(mount: Path, target: Path, current: bytes, op: dict, land) -> bytes:
    if not target.is_file() or _sha1(target) != ENTRY.fullmatch(op['entry']).group(1):
        raise ValueError('Установленный пакет не совпадает со строкой Mount')
    desired = with_entry(current, op['entry'])
    record = {'op': 'apply_mount', 'write': 'mount', 'mount_sha256': hashlib.sha256(desired).hexdigest()}
    if desired == current:
        land(dict(record, write=None))
        return current
    replace_atomic(mount, current, desired, landed=lambda: land(record))
    return desired


def _remove(mount: Path, target: Path, current: bytes, op: dict, records: tuple, land) -> bytes:
    pak_hash = op['pak_sha256']
    _check_ours(target, pak_hash)
    desired = without_entry(current)
    if desired != current:
        record = {'op': 'remove', 'write': 'mount', 'mount_sha256': hashlib.sha256(desired).hexdigest()}
        replace_atomic(mount, current, desired, landed=lambda: land(record))
        current = desired
    if pak_hash is None:
        kept = 'kept: no package.json' if records == (None, None) else 'kept: no hash in package.json'
    elif not target.exists():
        kept = 'kept: missing'
    else:
        _check_ours(target, pak_hash)
        target.unlink()
        land({'op': 'remove', 'write': 'pak', 'pak': 'removed'})
        return current
    land({'op': 'remove', 'write': None, 'pak': kept})
    return current


def _run(request: Path, done: list[dict], discover_roots, running, guard) -> None:
    native = request.parent.parent

    def land(record: dict) -> None:
        """Called as soon as a write reaches the disk (or an op finishes without one)."""
        done.append(record)
        _append_journal(native, {'event': 'native_helper_op', 'request': request.stem, **record, 'at': _now()})

    _append_journal(native, {'event': 'native_helper_start', 'request': request.stem, 'at': _now()})
    data = _parse(_read_verified(request, REQUEST_LIMIT))
    given = Path(data['game'])
    reject_links(given)
    game = given.resolve()
    if game not in {Path(root).resolve() for root in discover_roots()}:
        raise ValueError('Корень игры из запроса не совпадает с найденной установкой')
    operations = data['operations']
    names = [op['op'] for op in operations]
    mount = inside(game, MOUNT)
    target = inside(game, PAK_RELATIVE)
    if not target.parent.is_dir():
        raise ValueError('Нет папки пакетов Lang_en/Base')

    # Every check runs before the first write; a failure here leaves the game untouched.
    _ready(game, running, guard)
    current = read_bounded(mount)
    digest = hashlib.sha256(current).hexdigest()
    if any(op.get('expected_sha256', digest) != digest for op in operations):
        raise ValueError('Mount изменился после проверки')
    if 'remove' not in names and fingerprint(current) not in accepted_fingerprints(native):
        raise ValueError('Версия игры не совпадает с проверенной')
    staging, records = None, (None, None)
    try:
        for op in operations:
            if op['op'] == 'install_package':
                staging = _open_staging(native, op)
                if _digests(staging) != (op['sha256'], op['sha1']):
                    raise ValueError('Пакет в staging не совпадает с планом')
            elif op['op'] == 'remove':
                allowed, records = _removable(native)
                if op['pak_sha256'] not in allowed:
                    raise ValueError('Удалять можно только пакет из package.json или installed.json')
                _check_ours(target, op['pak_sha256'])

        for op in operations:
            _ready(game, running, guard)
            if read_bounded(mount) != current:
                raise ValueError('Mount изменился во время выполнения')
            if op['op'] == 'install_package':
                _install(staging, target, op, land)
            elif op['op'] == 'apply_mount':
                current = _apply(mount, target, current, op, land)
            else:
                current = _remove(mount, target, current, op, records, land)
    finally:
        if staging is not None:
            os.close(staging)


def _finish(request: Path, reserved: int | None, done: list[dict], error: str | None) -> dict:
    result = {'schema_version': 1, 'ok': error is None, 'error': error, 'operations': done}
    if reserved is not None:
        try:
            _write_all(reserved, _encode(result))
            os.fsync(reserved)
        finally:
            os.close(reserved)
    if _located(request):
        _append_journal(request.parent.parent, {'event': 'native_helper', 'request': request.stem,
                                                'ok': result['ok'], 'error': error, 'operations': done,
                                                'at': _now()})
    return result


def execute(request: Path, *, discover_roots=_discovered_roots, running=game_processes,
            guard=update_guard) -> dict:
    request = Path(os.path.abspath(request))
    done: list[dict] = []
    reserved = None
    try:
        reserved = _reserve_result(request)
        _run(request, done, discover_roots, running, guard)
    except BaseException as error:
        _finish(request, reserved, done, f'{type(error).__name__}: {error}')
        raise
    return _finish(request, reserved, done, None)


def helper_main(argv=None) -> int | None:
    """`--native-helper PATH` mode, handled before any Qt object exists; None otherwise."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(arg == '--native-helper' or arg.startswith('--native-helper=') for arg in argv):
        return None
    parser = argparse.ArgumentParser(prog='WuWaRussian --native-helper')
    parser.add_argument('--native-helper', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = execute(args.native_helper, discover_roots=_discovered_roots, running=game_processes,
                         guard=update_guard)
    except Exception:
        return 2
    return 0 if result['ok'] else 2


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_ulong), ('fMask', ctypes.c_ulong), ('hwnd', ctypes.c_void_p),
                ('lpVerb', ctypes.c_wchar_p), ('lpFile', ctypes.c_wchar_p),
                ('lpParameters', ctypes.c_wchar_p), ('lpDirectory', ctypes.c_wchar_p),
                ('nShow', ctypes.c_int), ('hInstApp', ctypes.c_void_p), ('lpIDList', ctypes.c_void_p),
                ('lpClass', ctypes.c_wchar_p), ('hkeyClass', ctypes.c_void_p), ('dwHotKey', ctypes.c_ulong),
                ('hIconOrMonitor', ctypes.c_void_p), ('hProcess', ctypes.c_void_p)]


def _api():
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return shell32, kernel32


def run_elevated(request: Path) -> int:
    """Start the helper through UAC and wait; 1223 (ERROR_CANCELLED) when the user declines."""
    frozen = getattr(sys, 'frozen', False)
    args = ['--native-helper', str(Path(request).absolute())]
    if not frozen:
        args = ['-m', 'wuwaru.gui', *args]
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = 'runas'
    info.lpFile = sys.executable
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory = None if frozen else str(Path(__file__).resolve().parent.parent)
    info.nShow = SW_HIDE
    shell32, kernel32 = _api()
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return ctypes.get_last_error() or 1
    handle = info.hProcess
    if not handle:
        raise OSError('ShellExecuteExW не вернул процесс помощника')
    try:
        if kernel32.WaitForSingleObject(handle, INFINITE) == WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value
    finally:
        kernel32.CloseHandle(handle)


def launch_steam() -> None:
    os.startfile(f'steam://rungameid/{STEAM_APP_ID}')
