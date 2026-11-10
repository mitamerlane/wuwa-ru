"""Self-update of the installed launcher from GitHub Releases. No Qt here.

Flow: `latest_release` reads the newest public release of `UPDATE_REPO`; `download` fetches its
signed `update.json`, checks the Ed25519 signature against the pinned `PUBLIC_KEY`, then streams
the setup EXE and accepts it only with the signed size and SHA-256; `start_installer` hands over to
that setup (`--update DIR --wait-pid PID`), which waits for the launcher to exit, installs over the
same folder (user data stays, see installer/core.py) and starts the launcher again.

The private signing key never leaves the release machine (tools/release.py). The SHA-256 file next
to the setup is for people; the updater trusts only the signed statement.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import __version__

UPDATE_REPO = 'mitamerlane/wuwa-ru'
# Raw Ed25519 public key (base64). Its private half: %USERPROFILE%\.wuwaru-release\signing-key.pem.
PUBLIC_KEY = 'JJYMtCJ5L7RiSPoJzHZ1P525P3ACH4oPybEUtWHx0bg='
API_URL = f'https://api.github.com/repos/{UPDATE_REPO}/releases/latest'
DOWNLOAD_PREFIX = f'https://github.com/{UPDATE_REPO}/releases/download/'
STATEMENT_NAME = 'update.json'
APP = 'WuWaRussian'
# Release assets redirect from github.com to GitHub's storage hosts.
ALLOWED_HOSTS = frozenset({'api.github.com', 'github.com', 'objects.githubusercontent.com',
                           'release-assets.githubusercontent.com'})
API_LIMIT = 1024 * 1024
STATEMENT_LIMIT = 64 * 1024
SETUP_LIMIT = 1024 ** 3
TIMEOUT = 20
CHUNK = 256 * 1024
VERSION = re.compile(r'v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})')
SHA256 = re.compile(r'[0-9a-f]{64}')

Opener = Callable[..., object]


class UpdateError(Exception):
    """Any reason not to update; the message is shown to the player as is."""


class Cancelled(UpdateError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    setup_name: str
    setup_url: str
    statement_url: str
    notes: str
    page_url: str


@dataclass(frozen=True)
class Statement:
    version: str
    file: str
    size: int
    sha256: str


def parse_version(text: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(str(text).strip())
    if not match:
        raise UpdateError(f'Непонятная версия релиза: {text!r}.')
    return tuple(int(part) for part in match.groups())


def is_newer(release: Release, current: str = __version__) -> bool:
    return parse_version(release.version) > parse_version(current)


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.port:
        raise UpdateError('Обновление ведёт на неожиданный адрес, отказано.')


def _open(url: str, opener: Opener):
    _check_url(url)
    request = urllib.request.Request(url, headers={'User-Agent': f'{APP}-Updater/{__version__}',
                                                   'Accept': 'application/vnd.github+json'})
    try:
        response = opener(request, timeout=TIMEOUT)
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001 - offline, DNS, HTTP 4xx/5xx: all mean "not now"
        raise UpdateError(f'Не удалось связаться с GitHub: {exc}') from exc
    _check_url(response.geturl())
    return response


def _read(url: str, opener: Opener, limit: int) -> bytes:
    with _open(url, opener) as response:
        try:
            data = response.read(limit + 1)
        except Exception as exc:  # noqa: BLE001
            raise UpdateError(f'Обрыв связи с GitHub: {exc}') from exc
    if len(data) > limit:
        raise UpdateError('Ответ GitHub слишком большой, отказано.')
    return data


def latest_release(opener: Opener = urllib.request.urlopen) -> Release:
    try:
        data = json.loads(_read(API_URL, opener, API_LIMIT))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError('GitHub вернул непонятный ответ.') from exc
    if not isinstance(data, dict):
        raise UpdateError('GitHub вернул непонятный ответ.')
    version = '.'.join(map(str, parse_version(data.get('tag_name', ''))))
    setup_name = f'{APP}-Setup-{version}.exe'
    assets = {asset.get('name'): asset.get('browser_download_url')
              for asset in data.get('assets') or [] if isinstance(asset, dict)}
    urls = {}
    for name in (setup_name, STATEMENT_NAME):
        url = assets.get(name)
        if not isinstance(url, str) or not url.startswith(DOWNLOAD_PREFIX):
            raise UpdateError(f'В релизе {version} нет подписанного файла {name}.')
        _check_url(url)
        urls[name] = url
    notes = data.get('body') if isinstance(data.get('body'), str) else ''
    page = data.get('html_url') if isinstance(data.get('html_url'), str) else ''
    return Release(version, setup_name, urls[setup_name], urls[STATEMENT_NAME], notes[:4000], page)


# -- signed statement -----------------------------------------------------------------------------

def public_key_text(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode('ascii')


def _payload(fields: dict) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def sign_statement(fields: dict, key: Ed25519PrivateKey) -> bytes:
    """Release side: {version, file, size, sha256} -> update.json bytes."""
    body = {'schema_version': 1, 'app': APP, 'version': fields['version'], 'file': fields['file'],
            'size': int(fields['size']), 'sha256': fields['sha256']}
    body['signature'] = base64.b64encode(key.sign(_payload(body))).decode('ascii')
    return json.dumps(body, ensure_ascii=False, indent=2).encode('utf-8')


def verify_statement(data: bytes, release: Release, public_key: str | None = None) -> Statement:
    public_key = PUBLIC_KEY if public_key is None else public_key
    if not public_key:
        raise UpdateError('В этой сборке нет ключа проверки обновлений; обнови вручную.')
    try:
        doc = json.loads(data)
        signature = base64.b64decode(doc.pop('signature'), validate=True)
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(signature, _payload(doc))
    except (InvalidSignature, ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        raise UpdateError('Подпись обновления не сходится, ничего не установлено.') from exc
    size, sha = doc.get('size'), doc.get('sha256')
    if (doc.get('schema_version') != 1 or doc.get('app') != APP or doc.get('version') != release.version
            or doc.get('file') != release.setup_name):
        raise UpdateError('Подписанный файл не относится к этому релизу, ничего не установлено.')
    if not isinstance(size, int) or not 0 < size <= SETUP_LIMIT or not isinstance(sha, str) \
            or not SHA256.fullmatch(sha):
        raise UpdateError('Подписанный файл обновления повреждён.')
    return Statement(release.version, release.setup_name, size, sha)


# -- download and hand-over -----------------------------------------------------------------------

def download(release: Release, folder: Path, opener: Opener = urllib.request.urlopen,
             public_key: str | None = None, progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] = lambda: False) -> Path:
    """Verified setup EXE in `folder`, or UpdateError with nothing left behind."""
    statement = verify_statement(_read(release.statement_url, opener, STATEMENT_LIMIT), release, public_key)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / statement.file
    part = dest.with_name(dest.name + '.part')
    for old in folder.glob(f'{APP}-Setup-*.exe*'):  # setups of earlier updates are not needed any more
        if old.is_file() and not old.is_symlink():
            old.unlink(missing_ok=True)
    digest, done = hashlib.sha256(), 0
    try:
        with _open(release.setup_url, opener) as response, open(part, 'wb') as out:
            while True:
                if cancelled():
                    raise Cancelled('Обновление отменено.')
                try:
                    chunk = response.read(CHUNK)
                except Exception as exc:  # noqa: BLE001
                    raise UpdateError(f'Обрыв связи при скачивании: {exc}') from exc
                if not chunk:
                    break
                done += len(chunk)
                if done > statement.size:
                    raise UpdateError('Файл обновления больше заявленного, отказано.')
                digest.update(chunk)
                out.write(chunk)
                if progress:
                    progress(done, statement.size)
        if done != statement.size or digest.hexdigest() != statement.sha256:
            raise UpdateError('Файл обновления не совпал с подписью, ничего не установлено.')
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest


def install_root() -> Path | None:
    """Folder of an installed, frozen launcher (has the installer's manifest); None otherwise."""
    if not getattr(sys, 'frozen', False):
        return None
    root = Path(sys.executable).resolve().parent
    return root if (root / '.install-manifest.json').is_file() else None


def start_installer(setup: Path, target: Path, pid: int | None = None, popen=subprocess.Popen) -> None:
    flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    popen([str(setup), '--update', str(target), '--wait-pid', str(pid or os.getpid())],
          cwd=str(Path(setup).parent), close_fds=True, creationflags=flags)
