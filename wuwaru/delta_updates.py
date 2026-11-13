"""Translation delta updates for every installed launcher. No Qt here.

The server (tools/server_publish.py) pushes each finished delta into the public repository `DELTA_REPO`:
`deltas/<revision12>/{delta.jsonl.gz,lock.json,summary.json}` and a signed `latest.json` that names them with
their SHA-256. The launcher reads only `latest.json` and those files, over HTTPS from raw.githubusercontent.com,
with no token or key of any kind. Nothing here can reach the server.

Trust: `latest.json` carries an Ed25519 signature made on the server; the matching public key is pinned below.
A repository that was taken over cannot push text into players' packs without that key. A replayed older
manifest is refused through `sequence`. Every file is accepted only with the signed size and SHA-256.

`run_delta_update` is one launcher start: manifest -> (new revision) download, back up, apply through
`delta_apply.apply_update`, manual edits kept -> (text changed) wait for, then take, the newer English
table of Loping151/WWUIResourcesFull. It never builds or installs the package: «Играть» stays manual.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import __version__

DELTA_REPO = 'mitamerlane/wuwa-ru-updates'  # public, read-only for players; the owner creates it and adds the server's deploy key
BRANCH = 'main'
HOST = 'raw.githubusercontent.com'
RAW_PREFIX = f'https://{HOST}/{DELTA_REPO}/{BRANCH}/'
MANIFEST_NAME = 'latest.json'
# Raw Ed25519 public key (base64) of the server's delta signing key. Empty until the owner runs
# `python3 -m tools.server_publish keygen` on the server and pastes the printed key here.
DELTA_PUBLIC_KEY = ''
APP = 'WuWaRussianDelta'
FILES = ('delta.jsonl.gz', 'lock.json', 'summary.json')
FILE_LIMITS = {'delta.jsonl.gz': 128 * 1024 * 1024, 'lock.json': 16 * 1024 * 1024, 'summary.json': 1024 * 1024}
MANIFEST_LIMIT = 64 * 1024
TIMEOUT = 30
CHUNK = 256 * 1024
REVISION = re.compile(r'[0-9a-f]{40}')
SHA256 = re.compile(r'[0-9a-f]{64}')
STATE_FILE = 'delta-state.json'

# Statuses shown in the launcher. `label` is what the player reads; the strings are the contract with the UI.
UP_TO_DATE = 'UP_TO_DATE'            # «перевод актуален»
APPLIED = 'APPLIED'                  # new delta taken, package not rebuilt yet
WAITING_TABLE = 'WAITING_TABLE'      # «ждём таблицу»: text moved on, the English table has not
TABLE_RECEIVED = 'TABLE_RECEIVED'    # «таблица получена»
NOTHING_PUBLISHED = 'NOTHING_PUBLISHED'
NETWORK_ERROR = 'NETWORK_ERROR'
HASH_ERROR = 'HASH_ERROR'
ERROR = 'ERROR'
LABELS = {
    UP_TO_DATE: 'Перевод актуален',
    APPLIED: 'Обновление перевода получено — нажми «Играть», пак соберётся заново',
    WAITING_TABLE: 'Ждём английскую таблицу: новые строки попадут в пак, когда она выйдет',
    TABLE_RECEIVED: 'Таблица получена — нажми «Играть», пак соберётся на ней',
    NOTHING_PUBLISHED: 'Обновлений перевода пока нет',
    NETWORK_ERROR: 'Нет связи с сервером обновлений. Проверю при следующем запуске',
    HASH_ERROR: 'Файлы обновления не прошли проверку (подпись или SHA-256). Ничего не изменено',
    ERROR: 'Обновление перевода не применено',
}

Opener = Callable[..., object]


class DeltaError(Exception):
    """A reason not to take the update; `state` is one of the statuses above."""

    def __init__(self, state: str, message: str = ''):
        super().__init__(message or LABELS[state])
        self.state = state


# -- signed manifest ------------------------------------------------------------------------------

def _payload(fields: dict) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def sign_manifest(fields: dict, signer: Callable[[bytes], bytes]) -> bytes:
    """Server side: manifest fields -> latest.json bytes with the signature over all other fields.

    `signer` is bytes -> 64-byte Ed25519 signature (the server has no `cryptography`: tools/ed25519_pure.py).
    This module imports nothing beyond the standard library at load time so the server can use it."""
    body = dict(fields, schema_version=1, app=APP)
    body['signature'] = base64.b64encode(signer(_payload(body))).decode('ascii')
    return json.dumps(body, ensure_ascii=False, indent=1).encode('utf-8')


def verify_manifest(data: bytes, public_key: str | None = None) -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    public_key = DELTA_PUBLIC_KEY if public_key is None else public_key
    if not public_key:
        raise DeltaError(HASH_ERROR, 'В этой сборке нет ключа проверки обновлений перевода; обнови программу.')
    try:
        doc = json.loads(data)
        signature = base64.b64decode(doc.pop('signature'), validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(signature, _payload(doc))
    except (InvalidSignature, ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        raise DeltaError(HASH_ERROR, 'Подпись обновления перевода не сходится, ничего не изменено.') from exc
    revision, sequence, files = doc.get('revision'), doc.get('sequence'), doc.get('files')
    if (doc.get('schema_version') != 1 or doc.get('app') != APP or not isinstance(revision, str)
            or not REVISION.fullmatch(revision) or doc.get('folder') != f'deltas/{revision[:12]}'
            or type(sequence) is not int or sequence < 1 or not isinstance(files, dict)
            or set(files) != set(FILES)):
        raise DeltaError(HASH_ERROR, 'Манифест обновления повреждён, ничего не изменено.')
    for name, entry in files.items():
        if (not isinstance(entry, dict) or type(entry.get('size')) is not int
                or not 0 < entry['size'] <= FILE_LIMITS[name] or not isinstance(entry.get('sha256'), str)
                or not SHA256.fullmatch(entry['sha256'])):
            raise DeltaError(HASH_ERROR, f'Манифест обновления: неверная запись {name}.')
    return doc


# -- network --------------------------------------------------------------------------------------

def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname != HOST or parts.username or parts.port
            or not parts.path.startswith(f'/{DELTA_REPO}/{BRANCH}/') or '..' in parts.path):
        raise DeltaError(ERROR, 'Обновление ведёт на неожиданный адрес, отказано.')


def _open(url: str, opener: Opener):
    _check_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': f'WuWaRussian-Delta/{__version__}'})
    try:
        response = opener(request, timeout=TIMEOUT)
    except DeltaError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise DeltaError(NOTHING_PUBLISHED) from exc
        raise DeltaError(NETWORK_ERROR, f'Сервер обновлений ответил ошибкой {exc.code}.') from exc
    except Exception as exc:  # noqa: BLE001 - offline, DNS, timeouts: all mean "not now"
        raise DeltaError(NETWORK_ERROR) from exc
    _check_url(response.geturl())
    return response


def fetch_manifest(opener: Opener = urllib.request.urlopen, public_key: str | None = None) -> dict:
    with _open(RAW_PREFIX + MANIFEST_NAME, opener) as response:
        try:
            data = response.read(MANIFEST_LIMIT + 1)
        except Exception as exc:  # noqa: BLE001
            raise DeltaError(NETWORK_ERROR) from exc
    if len(data) > MANIFEST_LIMIT:
        raise DeltaError(HASH_ERROR, 'Манифест обновления слишком большой, отказано.')
    return verify_manifest(data, public_key)


def download_delta(manifest: dict, folder: Path, opener: Opener = urllib.request.urlopen,
                   cancelled: Callable[[], bool] = lambda: False) -> Path:
    """The three files of `manifest` in `folder/<revision12>`, each with its signed size and SHA-256, or nothing."""
    import hashlib
    folder = Path(folder)
    target = folder / manifest['revision'][:12]
    staged = folder / f'.{manifest["revision"][:12]}.part'
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    try:
        for name in FILES:
            entry, digest, size = manifest['files'][name], hashlib.sha256(), 0
            with _open(RAW_PREFIX + manifest['folder'] + '/' + name, opener) as response, \
                    open(staged / name, 'xb') as out:
                while True:
                    if cancelled():
                        raise DeltaError(ERROR, 'Обновление отменено.')
                    try:
                        block = response.read(CHUNK)
                    except Exception as exc:  # noqa: BLE001
                        raise DeltaError(NETWORK_ERROR) from exc
                    if not block:
                        break
                    size += len(block)
                    if size > entry['size']:
                        raise DeltaError(HASH_ERROR, f'{name} больше заявленного, отказано.')
                    digest.update(block)
                    out.write(block)
            if size != entry['size'] or digest.hexdigest() != entry['sha256']:
                raise DeltaError(HASH_ERROR, f'{name} не совпал с подписанным SHA-256, ничего не изменено.')
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staged, target)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return target


# -- state ----------------------------------------------------------------------------------------

def _state_path(paths) -> Path:
    return paths.data / 'Updates' / STATE_FILE


def load_state(paths) -> dict:
    try:
        state = json.loads(_state_path(paths).read_text(encoding='utf-8'))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(paths, state: dict) -> None:
    path = _state_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + '.tmp')
    staged.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(staged, path)


def project_revisions(workspace: Path) -> set[str]:
    """Revisions of the pinned text source the project database knows (read-only)."""
    from .delta_apply import TEXT_REPOSITORY
    repository = 'https://github.com/' + TEXT_REPOSITORY
    found = set()
    db = sqlite3.connect(f'{(Path(workspace) / "translations.sqlite").as_uri()}?mode=ro', uri=True)
    try:
        for (raw,) in db.execute('SELECT inspection FROM resources'):
            try:
                inspection = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(inspection, dict) and inspection.get('repository') == repository:
                found.add(inspection.get('revision'))
    finally:
        db.close()
    return found


def _result(state: str, **extra) -> dict:
    return dict(state=state, message=LABELS[state], **extra)


# -- one launcher start ---------------------------------------------------------------------------

def run_delta_update(paths, workspace: Path, *, opener: Opener = urllib.request.urlopen,
                     table_opener=None, fetcher=None, public_key: str | None = None,
                     cancelled: Callable[[], bool] = lambda: False, now=None) -> dict:
    """Never raises for expected failures: returns {state, message, ...} for the launcher to show.

    The project database must not be open elsewhere (the launcher runs this as its only job).
    """
    from . import delta_apply, table_fetch
    from . import native_package as package
    table_opener = table_opener or table_fetch._open
    workspace = Path(workspace)
    state = load_state(paths)
    notes: dict = {}
    try:
        manifest = fetch_manifest(opener, public_key)
        revision = manifest['revision']
        if manifest['sequence'] < state.get('sequence', 0):
            raise DeltaError(HASH_ERROR, 'Манифест старее уже применённого, отказано.')
        if revision != state.get('revision') and revision not in project_revisions(workspace):
            update = download_delta(manifest, paths.data / 'Updates', opener, cancelled)
            report = delta_apply.apply_update(paths.root, workspace, update, fetcher=fetcher)
            imported = report['import']
            notes.update(applied=report['delta'], backup=report['backup'], imported=imported)
            state.update(revision=revision, sequence=manifest['sequence'], data_version=manifest.get('data_version'),
                         applied_at=(now() if now else datetime.now(timezone.utc)).isoformat(timespec='seconds'),
                         table_pending=bool(imported['new'] or imported['changed']),
                         table_blob_at_apply=package.table_record(paths.native)['blob'])
            save_state(paths, state)
            shutil.rmtree(update, ignore_errors=True)
            notes['just_applied'] = True
        elif revision != state.get('revision'):  # the project already is at this text revision
            state.update(revision=revision, sequence=manifest['sequence'], data_version=manifest.get('data_version'))
            save_state(paths, state)
        notes.update(revision=revision, data_version=manifest.get('data_version'))
        if state.get('table_pending'):
            return _table_step(paths, state, notes, table_opener, table_fetch, cancelled)
        return _result(APPLIED if notes.get('just_applied') else UP_TO_DATE, **notes)
    except DeltaError as exc:
        return dict(state=exc.state, message=str(exc), **notes)
    except (OSError, ValueError, KeyError, sqlite3.Error, json.JSONDecodeError) as exc:
        return dict(state=ERROR, message=f'{LABELS[ERROR]}: {exc}'[:400], **notes)
    except Exception as exc:  # noqa: BLE001 - the launcher must start whatever the network or the data did
        return dict(state=ERROR, message=f'{LABELS[ERROR]}: {type(exc).__name__}: {exc}'[:400], **notes)


def _table_step(paths, state, notes, opener, table_fetch, cancelled) -> dict:
    """Text moved on: has Loping151 published a different table yet? If so take it (public file, by Git blob)."""
    from urllib.error import URLError
    from . import native_package as package
    try:
        revision, commit = table_fetch.resolve(opener)
        entry = table_fetch.locate(opener, commit['tree']['sha'])
    except (URLError, OSError, TimeoutError) as exc:
        return dict(state=NETWORK_ERROR, message='Нет связи с GitHub: таблицу проверю при следующем запуске.',
                    detail=str(exc)[:200], **notes)
    record = package.table_record(paths.native)
    installed = record['blob']
    # A table built from the text source (table_fetch.build) is never traded for the older public one.
    if record['repository'] != package.TABLE_REPOSITORY or state.get('table_blob_at_apply') not in (None, installed):  # taken meanwhile, e.g. by tools.native_table
        state.update(table_pending=False)
        save_state(paths, state)
        return _result(TABLE_RECEIVED, **notes)
    if entry['sha'] == installed:
        return _result(WAITING_TABLE, **notes)
    if cancelled():
        raise DeltaError(ERROR, 'Обновление отменено.')
    try:
        record = table_fetch.fetch(paths.native, revision, opener=opener)
    except (URLError, OSError, TimeoutError) as exc:
        return dict(state=NETWORK_ERROR, message='Таблица не скачалась: связь оборвалась. Повторю при следующем запуске.',
                    detail=str(exc)[:200], **notes)
    except ValueError as exc:  # size / Git blob mismatch: nothing was installed
        return dict(state=HASH_ERROR, message=f'Таблица не прошла проверку: {exc}', **notes)
    state.update(table_pending=False, table_received=record.get('sha256'))
    save_state(paths, state)
    return _result(TABLE_RECEIVED, table=package.table_label(record), **notes)
