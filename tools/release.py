"""Sign and publish a launcher release that installed launchers pick up as an update.

    python -m tools.release init-key                 # once per release machine
    python tools/build_installer.py                  # -> dist/WuWaRussian-Setup-<ver>.exe
    python -m tools.release sign                     # -> dist/update.json (Ed25519)
    python -m tools.release publish --notes notes.md # GitHub release v<ver> with setup, .sha256, update.json

The private key stays in %USERPROFILE%\\.wuwaru-release (never in a repository). The GitHub token
comes from Git Credential Manager (`git credential fill`) and is never printed or written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wuwaru import __version__, updater  # noqa: E402

KEY_DIR = Path(os.environ.get('WUWARU_RELEASE_KEY_DIR') or Path.home() / '.wuwaru-release')
KEY_FILE = KEY_DIR / 'signing-key.pem'
DIST = ROOT / 'dist'


def setup_path(version: str = __version__) -> Path:
    return DIST / f'{updater.APP}-Setup-{version}.exe'


def load_key(path: Path = KEY_FILE) -> Ed25519PrivateKey:
    if not path.is_file():
        raise SystemExit(f'Нет ключа подписи {path}. Сначала: python -m tools.release init-key')
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f'{path}: не Ed25519-ключ.')
    return key


def init_key(path: Path = KEY_FILE) -> str:
    if path.exists():
        raise SystemExit(f'{path} уже есть; старый ключ не перезаписываю.')
    if ROOT in path.resolve().parents:
        raise SystemExit('Ключ не должен лежать внутри репозитория.')
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return updater.public_key_text(key.public_key())


def sign(setup: Path, version: str = __version__, key: Ed25519PrivateKey | None = None) -> Path:
    if not setup.is_file():
        raise SystemExit(f'Нет {setup}. Сначала: python tools/build_installer.py')
    digest = hashlib.sha256()
    with open(setup, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    key = key or load_key()
    fields = {'version': version, 'file': setup.name, 'size': setup.stat().st_size, 'sha256': digest.hexdigest()}
    statement = updater.sign_statement(fields, key)
    if updater.public_key_text(key.public_key()) != updater.PUBLIC_KEY:
        raise SystemExit('Ключ подписи не совпадает с PUBLIC_KEY в wuwaru/updater.py: лаунчеры такое не примут.')
    out = setup.parent / updater.STATEMENT_NAME
    out.write_bytes(statement)
    return out


# -- GitHub ----------------------------------------------------------------------------------------

def github_token() -> str:
    answer = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                            capture_output=True, text=True, check=True).stdout
    for line in answer.splitlines():
        if line.startswith('password='):
            return line[len('password='):]
    raise SystemExit('Git Credential Manager не дал токен GitHub.')


def api(method: str, url: str, token: str, body: bytes | None = None, content_type='application/json'):
    request = urllib.request.Request(url, data=body, method=method, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'WuWaRussian-Release',
        **({'Content-Type': content_type} if body is not None else {})})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read() or b'{}')


def publish(version: str, notes: str, repo: str = updater.UPDATE_REPO, token: str | None = None) -> str:
    setup = setup_path(version)
    files = [setup, setup.with_name(setup.name + '.sha256'), DIST / updater.STATEMENT_NAME]
    for path in files:
        if not path.is_file():
            raise SystemExit(f'Нет {path}.')
    statement = json.loads(files[2].read_text(encoding='utf-8'))
    if statement.get('version') != version or statement.get('file') != setup.name:
        raise SystemExit('update.json подписан для другой версии: python -m tools.release sign')
    release = updater.Release(version, setup.name, '', '', '', '')
    signed = updater.verify_statement(files[2].read_bytes(), release)
    if signed.size != setup.stat().st_size:
        raise SystemExit('update.json не совпадает с файлом установщика.')
    token = token or github_token()
    created = api('POST', f'https://api.github.com/repos/{repo}/releases', token, json.dumps({
        'tag_name': f'v{version}', 'target_commitish': 'main', 'name': f'WuWa Russian {version}',
        'body': notes, 'draft': False, 'prerelease': False}).encode('utf-8'))
    upload = created['upload_url'].split('{', 1)[0]
    for path in files:
        print(f'  загружаю {path.name} ({path.stat().st_size // 2**20} МиБ)…', flush=True)
        api('POST', f'{upload}?name={urllib.parse.quote(path.name)}', token, path.read_bytes(),
            'application/octet-stream')
    return created['html_url']


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Подпись и публикация релиза лаунчера')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init-key', help='создать ключ подписи (один раз)')
    sign_cmd = sub.add_parser('sign', help='подписать dist/WuWaRussian-Setup-<версия>.exe')
    sign_cmd.add_argument('--setup', type=Path)
    pub = sub.add_parser('publish', help='создать релиз на GitHub и загрузить файлы')
    pub.add_argument('--notes', type=Path, help='Markdown с описанием изменений')
    pub.add_argument('--repo', default=updater.UPDATE_REPO)
    args = parser.parse_args(argv)
    if args.command == 'init-key':
        public = init_key()
        print(f'Ключ: {KEY_FILE}\nPUBLIC_KEY для wuwaru/updater.py: {public}')
    elif args.command == 'sign':
        print(f'OK {sign(args.setup or setup_path())}')
    else:
        notes = args.notes.read_text(encoding='utf-8') if args.notes else f'WuWa Russian {__version__}'
        print(f'OK {publish(__version__, notes, args.repo)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
