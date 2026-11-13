"""Low-level game-file primitives shared by the application and the research tools.

Moved from tools/mountlang_probe.py and tools/native_pak_experiment.py without
behavior changes; the tools re-export these names.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from wuwaru.discovery import NESTED_GAME, STEAM_APP_ID, inspect_installation, parse_vdf
from wuwaru.native_package import PINNED_TABLE, sha256, table_record
from wuwaru.paths import UnsafePath, inside, reject_links

RESOURCES = 'Client/Saved/Resources'
RESOURCE = RESOURCES + '/3.7.0'  # the 3.7 research tools and fixtures; the application asks resource(game)
MOUNT_FILE = 'Mount/MountLang_en.txt'
MOUNT = RESOURCE + '/' + MOUNT_FILE
LIMIT = 4096
VERSION_FOLDER = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,3}')


def resource(game: Path) -> str:
    """'Client/Saved/Resources/<version>' of this game: the one version folder with the language Mount.

    The folder is named after the game version (3.7.0, 3.8.0…). None is FileNotFoundError; several are not
    guessed between."""
    base = inside(game, RESOURCES)
    reject_links(base)
    try:
        names = sorted(item.name for item in base.iterdir() if VERSION_FOLDER.fullmatch(item.name)
                       and (item / MOUNT_FILE).is_file())
    except (FileNotFoundError, NotADirectoryError):
        names = []
    if not names:
        raise FileNotFoundError('Нет MountLang_en.txt')
    if len(names) > 1:
        raise ValueError('Несколько папок ресурсов игры, какая действует — неизвестно: ' + ', '.join(names))
    return RESOURCES + '/' + names[0]


def mount_path(game: Path) -> Path:
    return inside(game, resource(game) + '/' + MOUNT_FILE)


def pak_path(game: Path) -> Path:
    return inside(game, resource(game) + '/Lang_en/Base/' + PAK_NAME)


def resources_label(game: Path | None) -> str:
    """Version folder name for a badge, '—' when the game or its folder is not known."""
    try:
        return resource(inspect_installation(Path(game)).root).rsplit('/', 1)[-1]
    except (OSError, ValueError, TypeError):
        return '—'


def install_top(game: Path) -> Path:
    """The folder Steam installed: the game itself, or the launcher folder above the nested game."""
    game = Path(game)
    if game.name.casefold() == NESTED_GAME.casefold() and (game.parent / 'launcher.exe').is_file():
        return game.parent
    return game


def read_bounded(path: Path, limit: int = LIMIT) -> bytes:
    reject_links(path)
    before = path.stat()
    if before.st_nlink != 1:
        raise ValueError('Hardlinked files are not supported')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    after = path.stat()
    if len(raw) > limit or (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Oversized or changing metadata')
    return raw


def consumers(game: Path) -> list[dict]:
    """Fail closed if Windows cannot provide the process inventory; no command lines."""
    quoted_root = str(game).replace("'", "''") + '\\'
    code = ("$ErrorActionPreference='Stop'; $all=@(Get-CimInstance Win32_Process); "
            "if (!$all.Count) { throw 'Process inventory unavailable' }; "
            "$matches=@($all | Where-Object { $_.Name -match "
            "'(?i)Client-Win64-Shipping|Wuther|launcher|Kuro|KRInstall' -or "
            f"($_.ExecutablePath -and $_.ExecutablePath.StartsWith('{quoted_root}',"
            "[StringComparison]::OrdinalIgnoreCase)) } | "
            "Select-Object ProcessId,Name,ExecutablePath); "
            "ConvertTo-Json -InputObject $matches -Compress")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', code],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20,
                            # No console window: it steals focus, and the Play page re-checks on
                            # every activation, so a visible window loops the check forever.
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('Process inventory failed; no game write permitted: ' + result.stderr)
    return json.loads(result.stdout.lstrip('\ufeff'))


# launcher_main/launcher_updater/KRInstall are the Kuro launcher of the 3.8 layout; it runs elevated, without a path.
GAME_PROCESS = re.compile(r'(?i)Client-Win64-Shipping|Wuther|Kuro|^launcher_(main|updater)\.exe$|^KRInstall')


def game_processes(game: Path) -> list[dict]:
    """`consumers` narrowed to this game: a Wuthering Waves / Kuro name or an EXE under the root.

    `consumers` also matches any *launcher* (Epic, Battle.net\u2026), which says nothing about this
    game; the tools keep using it unchanged. Failures propagate, so callers still fail closed."""
    root = os.path.normcase(os.path.abspath(install_top(game))).rstrip('\\/') + os.sep  # the Kuro launcher too
    found = []
    for item in consumers(game):
        name, path = str(item.get('Name') or ''), str(item.get('ExecutablePath') or '')
        if GAME_PROCESS.search(name) or (path and os.path.normcase(os.path.abspath(path)).startswith(root)):
            found.append(item)
    return found


def process_names(active: list[dict]) -> str:
    """Process names only, never paths, for the window's reasons and the helper's error."""
    return ', '.join(sorted({str(item.get('Name') or 'unknown') for item in active})) or 'unknown'


def replace_contents(source: Path, expected: bytes, desired: bytes) -> None:
    """One existing file only. A disk/power failure still requires backup recovery."""
    reject_links(source)
    if source.stat().st_nlink != 1:
        raise ValueError('Hardlinked Mount is not supported')
    with source.open('r+b') as stream:
        if stream.read(LIMIT + 1) != expected:
            raise ValueError('Mount changed before write')
        stream.seek(0)
        stream.write(desired)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    if read_bounded(source) != desired:
        raise RuntimeError('Write verification failed; use the preserved backup')


def replace_atomic(path: Path, expected: bytes, desired: bytes, *, landed=None) -> None:
    """Temp file next to the target, fsync, re-check, os.replace, read back.

    `landed` is called right after os.replace, before verification, so a caller can record
    a write that reached the disk even if the verification then fails.
    """
    reject_links(path)
    temporary = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0))
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        if read_bounded(path) != expected:
            raise ValueError('Mount changed before write')
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if landed is not None:
        landed()
    if read_bounded(path) != desired:
        raise RuntimeError('Write verification failed; use the preserved backup')


def update_guard(game: Path) -> None:
    """Steam state whitelist only; never export or hash the personal appmanifest."""
    game = install_top(game)
    if game.parent.name.casefold() != 'common' or game.parent.parent.name.casefold() != 'steamapps':
        raise ValueError('A/B updater check requires the prepared Steam installation')
    apps = game.parent.parent
    for relative in ('downloading/' + STEAM_APP_ID, 'temp/' + STEAM_APP_ID):
        path = inside(apps, relative)
        if path.exists():
            raise ValueError('Active or pending Steam update: ' + relative)
    path = inside(apps, 'appmanifest_' + STEAM_APP_ID + '.acf')
    app = parse_vdf(read_bounded(path).decode('utf-8-sig')).get('appstate', {})
    if app.get('appid') != STEAM_APP_ID or app.get('stateflags') != '4':
        raise ValueError('Steam update state is not fully installed')
    for total, done in (('bytestodownload', 'bytesdownloaded'), ('bytestostage', 'bytesstaged')):
        if int(app.get(total, '-1')) < 0 or app.get(total) != app.get(done):
            raise ValueError('Steam update progress incomplete or unavailable')


PAK_NAME = 'pakchunk10-WuWaRussian.pak'
MOUNT_STEM = 'Lang_en/Base/pakchunk10-WuWaRussian'
VERIFIED_FINGERPRINT = (
    'Lang_en/Base/pakchunk10-WindowsNoEditor,4,89C6309745252902C297007F3EB3D2C50807AF3A,'
    'EE7F1D1A2027E4367E45797CE56A3286202D778D,,',
    'Lang_en/3.7.6/pakchunk10-WindowsNoEditor_P,10,17D0D8925FCC399BE97960D161D3767F4FBA957E,'
    'ACD316833F62174D5CDC437C0A3E4252C5E7740F,,')
PAK_RELATIVE = RESOURCE + '/Lang_en/Base/' + PAK_NAME
REBUILD_MISSING = 'Пакет нужно собрать заново'


class Phase(StrEnum):
    READY = 'READY'
    NEEDS_INSTALL = 'NEEDS_INSTALL'
    NEEDS_REBUILD = 'NEEDS_REBUILD'
    GAME_RUNNING = 'GAME_RUNNING'
    STEAM_UPDATING = 'STEAM_UPDATING'
    VERSION_CHANGED = 'VERSION_CHANGED'
    NO_GAME = 'NO_GAME'
    NO_INPUTS = 'NO_INPUTS'


@dataclass
class LaunchState:
    phase: Phase
    reasons: list[str] = field(default_factory=list)
    game: Path | None = None
    build_id: str | None = None
    fingerprint: tuple[str, ...] | None = None
    ours_in_mount: bool = False
    installed_sha256: str | None = None
    package: dict | None = None
    mount_sha256: str | None = None
    installed_record: dict | None = None
    installed_bytes: int | None = None
    mount_applied: bool = False  # READY and Mount already holds exactly our line for the installed PAK
    table: dict | None = None  # provenance of the English table in Data/Native (None: table.json unreadable)
    resources: str | None = None  # version folder under Client/Saved/Resources the state was read from


def _lines(mount: bytes) -> tuple[list[str], int, int]:
    """Decoded lines with endings, plus [start, end) of the ::Mount:: section body."""
    lines = mount.decode('utf-8').splitlines(keepends=True)
    start = next((i + 1 for i, line in enumerate(lines) if line.strip() == '::Mount::'), None)
    if start is None:
        raise ValueError('Mount file has no ::Mount:: section')
    end = next((i for i in range(start, len(lines)) if lines[i].strip() == '::Del::'), len(lines))
    return lines, start, end


def _is_ours(line: str) -> bool:
    return line.strip().split(',', 1)[0] == MOUNT_STEM


def _body(line: str) -> str:
    return line.rstrip('\r\n')


def fingerprint(mount: bytes) -> tuple[str, ...]:
    lines, start, end = _lines(mount)
    return tuple(_body(line) for line in lines[start:end] if not _is_ours(line))


def with_entry(mount: bytes, entry: str) -> bytes:
    """Our line exactly once, as the last line of ::Mount::, before ::Del::."""
    mount = without_entry(mount)
    lines, _, end = _lines(mount)
    eol = '\r\n' if b'\r\n' in mount else '\n'
    head, tail = lines[:end], lines[end:]
    if head and not head[-1].endswith(('\n', '\r')):
        head[-1] += eol
    return ''.join(head + [entry + (eol if tail else '')] + tail).encode('utf-8')


def without_entry(mount: bytes) -> bytes:
    lines, start, end = _lines(mount)
    kept = lines[:start] + [line for line in lines[start:end] if not _is_ours(line)] + lines[end:]
    if end == len(lines) and len(kept) < len(lines) and not lines[-1].endswith(('\n', '\r')) and kept:
        # Our line was the unterminated last line: the new last line loses the ending we added.
        kept[-1] = kept[-1].rstrip('\r\n')
    return ''.join(kept).encode('utf-8')


def mount_entry(package: dict) -> str:
    return f"{MOUNT_STEM},900,{package['pak_sha1']},,,"


def has_entry(mount: bytes) -> bool:
    """Our stem in the ::Mount:: section only; a line under ::Del:: is not mounted."""
    lines, start, end = _lines(mount)
    return any(_is_ours(line) for line in lines[start:end])


def drop_staging(native: Path, plan: list[dict]) -> None:
    """After a verified install: the helper copies the package and never deletes in Data/Native,
    so the app removes the staging copy itself. Failures leave a file the next build sweeps."""
    staging = Path(native) / 'staging'
    for op in plan:
        if op['op'] != 'install_package':
            continue
        path = Path(op['staging'])
        try:
            if path.parent == staging and path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            pass


def _accepted(native: Path) -> list[tuple[tuple[str, ...], str]]:
    """(game fingerprint, table SHA-256) pairs. Our PAK replaces the whole table, so a version is accepted
    together with the table it was accepted with; entries from before tables could change name the pinned one."""
    pairs = [(VERIFIED_FINGERPRINT, PINNED_TABLE['sha256'])]
    path = native / 'accepted.json'
    if path.is_file():
        data = json.loads(read_bounded(path, 1024 * 1024).decode('utf-8'))
        for item in data.get('accepted', []):
            pairs.append((tuple(item['fingerprint']), item.get('table_sha256') or PINNED_TABLE['sha256']))
    return pairs


def accepted_fingerprints(native: Path, table_sha256: str | None = None) -> set[tuple[str, ...]]:
    """Game versions accepted for the table in `native` (or for `table_sha256`)."""
    if table_sha256 is None:
        table_sha256 = table_record(native)['sha256']
    return {fp for fp, table in _accepted(native) if table == table_sha256}


def table_reused(native: Path, fp: tuple[str, ...]) -> bool:
    """The current table already belongs to another game version: on `fp` it would hide that version's
    new official strings behind the old ones."""
    table = table_record(native)['sha256']
    return any(table == used and tuple(other) != tuple(fp) for other, used in _accepted(native))


def accept_fingerprint(native: Path, fp: tuple[str, ...]) -> None:
    native.mkdir(parents=True, exist_ok=True)
    path = native / 'accepted.json'
    table = table_record(native)
    data = {'schema_version': 1, 'accepted': []}
    if path.is_file():
        data = json.loads(read_bounded(path, 1024 * 1024).decode('utf-8'))
    at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if not any(tuple(item['fingerprint']) == tuple(fp) and
               (item.get('table_sha256') or PINNED_TABLE['sha256']) == table['sha256'] for item in data['accepted']):
        data['accepted'].append({'fingerprint': list(fp), 'table_sha256': table['sha256'],
                                 'table_revision': table['revision'], 'accepted_at': at})
        descriptor, temporary = tempfile.mkstemp(dir=native, prefix='accepted.', suffix='.tmp')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    with (native / 'journal.jsonl').open('a', encoding='utf-8') as journal:
        journal.write(json.dumps({'event': 'accept_fingerprint', 'fingerprint': list(fp),
                                  'table_sha256': table['sha256'], 'at': at},
                                 ensure_ascii=False) + '\n')


def _package(native: Path, name: str = 'package.json') -> dict | None:
    path = native / name
    if not path.is_file():
        return None
    return json.loads(read_bounded(path, 1024 * 1024).decode('utf-8'))


def record_installed(native: Path, op: dict) -> dict:
    """Non-elevated, after the helper installed `op`: which package now sits in the game.

    package.json moves on with every build; installed.json keeps naming the PAK in the game so
    «Удалить перевод» works between a rebuild and its installation."""
    record = {'schema_version': 1, 'key': Path(op['staging']).stem, 'pak_sha256': op['sha256'],
              'pak_sha1': op['sha1']}
    native.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=native, prefix='installed.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, native / 'installed.json')
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return record


def inspect(game: Path | None, native: Path, key: str | None, *, running=game_processes,
            guard=update_guard, input_error: str | None = None,
            build_id: str | None = None) -> LaunchState:
    state = LaunchState(Phase.NO_GAME, game=game, build_id=build_id)
    if game is None:
        state.reasons.append('Игра не найдена')
        return state
    try:
        folder = resource(game)
        mount = read_bounded(inside(game, folder + '/' + MOUNT_FILE))
    except FileNotFoundError:
        state.reasons.append('Нет MountLang_en.txt')
        return state
    except UnsafePath:
        raise
    except ValueError as error:  # several version folders: not guessed between
        state.reasons.append(str(error))
        return state
    state.resources = folder.rsplit('/', 1)[-1]
    state.mount_sha256 = hashlib.sha256(mount).hexdigest()
    try:
        state.fingerprint = fingerprint(mount)
        state.ours_in_mount = has_entry(mount)
    except ValueError as error:
        state.reasons.append(str(error))
    state.package = _package(native)
    state.installed_record = _package(native, 'installed.json')
    pak = inside(game, folder + '/Lang_en/Base/' + PAK_NAME)
    if pak.is_file():
        reject_links(pak)
        state.installed_sha256 = sha256(pak)
        state.installed_bytes = pak.stat().st_size
    try:
        active = running(game)
    except Exception as error:  # fail closed: unknown process state counts as running
        active = [{'Name': 'unknown'}]
        state.reasons.append(str(error))
    if active:
        state.phase = Phase.GAME_RUNNING
        state.reasons.append('Запущено: ' + process_names(active))
        state.reasons.append('Закрой игру и лаунчер')
        return state
    try:
        guard(game)
    except (ValueError, OSError) as error:
        state.phase = Phase.STEAM_UPDATING
        state.reasons.append(str(error))
        return state
    if key is None:
        state.phase = Phase.NO_INPUTS
        state.reasons.append(input_error or 'Нет входных данных перевода')
        return state
    try:
        state.table = table_record(native)
    except (OSError, ValueError) as error:
        state.phase = Phase.NO_INPUTS
        state.reasons.append(str(error))
        return state
    if state.fingerprint is None or state.fingerprint not in accepted_fingerprints(native, state.table['sha256']):
        state.phase = Phase.VERSION_CHANGED
        if state.fingerprint is not None and any(state.fingerprint == fp for fp, _ in _accepted(native)):
            state.reasons.append('Таблица строк не проверена с этой версией игры')
        else:
            state.reasons.append('Версия игры не совпадает с проверенной')
        return state
    package = state.package
    if package is None or package.get('key') != key:
        state.phase = Phase.NEEDS_REBUILD
        state.reasons.append('Пакет перевода нужно собрать заново')
        return state
    if state.installed_sha256 != package.get('pak_sha256'):
        if not (native / 'staging' / f"{package.get('key')}.pak").is_file():
            # Staging goes away after an install, the game copy with «Удалить перевод»:
            # nothing left to install from, so the same key is built again first.
            state.phase = Phase.NEEDS_REBUILD
            state.reasons.append(REBUILD_MISSING)
            return state
        state.phase = Phase.NEEDS_INSTALL
        state.reasons.append('Пакет нужно установить в игру')
        return state
    state.phase = Phase.READY
    try:
        state.mount_applied = with_entry(mount, mount_entry(package)) == mount
    except (KeyError, ValueError):
        state.mount_applied = False
    return state


def _inspected_mount(state: LaunchState) -> str:
    if state.mount_sha256 is None:
        raise ValueError('Mount не был прочитан при проверке')
    return state.mount_sha256


def plan_play(state: LaunchState, native: Path) -> list[dict]:
    if state.phase not in (Phase.READY, Phase.NEEDS_INSTALL) or state.game is None or state.package is None:
        raise ValueError(f'Запуск с переводом недоступен в состоянии {state.phase}')
    package = state.package
    plan: list[dict] = []
    if state.phase is Phase.NEEDS_INSTALL:
        plan.append({'op': 'install_package',
                     'staging': str(native / 'staging' / f"{package['key']}.pak"),
                     'sha256': package['pak_sha256'], 'sha1': package['pak_sha1']})
    plan.append({'op': 'apply_mount', 'expected_sha256': _inspected_mount(state),
                 'entry': mount_entry(package)})
    return plan


def plan_remove(state: LaunchState) -> list[dict]:
    if state.phase in (Phase.NO_GAME, Phase.GAME_RUNNING, Phase.STEAM_UPDATING) or state.game is None:
        raise ValueError(f'Удаление перевода недоступно в состоянии {state.phase}')
    # Only our own records name the PAK to delete, never the bare file in the game: installed.json
    # when it matches that file, else package.json, else installed.json (PAK already gone).
    record, package = state.installed_record, state.package
    if record is not None and record.get('pak_sha256') == state.installed_sha256:
        pak = record.get('pak_sha256')
    elif package is not None:
        pak = package.get('pak_sha256')
    else:
        pak = (record or {}).get('pak_sha256')
    return [{'op': 'remove', 'pak_sha256': pak, 'expected_sha256': _inspected_mount(state)}]
