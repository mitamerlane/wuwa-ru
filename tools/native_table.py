"""Take a newer public English table for the Play package: one file of the pinned public source, by Git blob.

`fetch` resolves a revision of Loping151/WWUIResourcesFull (the default branch head, or a full SHA-1 given),
walks its tree to Text/ConfigDB/en/lang_multi_text.db and downloads only that file. It is installed only if size
and Git blob match the tree; native_package.install_table then checks it again and writes table.json.
`show` prints the table Data/Native is set up with. Run with the app closed. Never reads or writes the game,
never uses keys; this is public data, not a game file. A newer table needs the game version accepted again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wuwaru import native_package as package  # noqa: E402
from wuwaru.paths import reject_links  # noqa: E402

API = 'https://api.github.com/repos/' + package.TABLE_REPOSITORY
RAW = 'https://raw.githubusercontent.com/' + package.TABLE_REPOSITORY + '/'
MAX_JSON = 16 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{40}')
NAME = re.compile(r'[\w.-]{1,100}')


def _open(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                   'User-Agent': 'wuwaru-table-fetch'})
    return urllib.request.urlopen(request, timeout=90)


def _json(opener, url):
    with opener(url) as response:
        data = response.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError('Слишком большой ответ GitHub')
    return json.loads(data)


def resolve(opener, revision: str | None = None) -> tuple[str, dict]:
    """Full revision and its commit; without `revision`, the head of the default branch."""
    if revision is None:
        branch = _json(opener, API)['default_branch']
        if not isinstance(branch, str) or not NAME.fullmatch(branch) or '..' in branch:
            raise ValueError('Недопустимое имя ветки GitHub')
        ref = urllib.parse.quote(branch)
    elif SHA.fullmatch(revision):
        ref = revision
    else:
        raise ValueError('Ревизия должна быть полным SHA-1 из 40 знаков')
    commit = _json(opener, f'{API}/commits/{ref}')
    sha = commit.get('sha')
    if not isinstance(sha, str) or not SHA.fullmatch(sha) or (revision is not None and sha != revision):
        raise ValueError('GitHub вернул другую ревизию')
    return sha, commit['commit']


def locate(opener, tree: str) -> dict:
    """Tree entry of the table: walk Text/ConfigDB/en one level at a time, names and hashes checked."""
    *folders, name = package.TABLE_PATH.split('/')
    for part in folders + [name]:
        if not isinstance(tree, str) or not re.fullmatch(r'[0-9A-Za-z]{1,64}', tree):
            raise ValueError('Недопустимое дерево GitHub')
        listing = _json(opener, f'{API}/git/trees/{tree}')
        if listing.get('truncated'):
            raise ValueError('GitHub обрезал список файлов')
        kind = 'blob' if part == name else 'tree'
        found = [entry for entry in listing['tree'] if entry.get('path') == part and entry.get('type') == kind]
        if len(found) != 1:
            raise ValueError(f'В ревизии нет {package.TABLE_PATH}')
        if kind == 'tree':
            tree = found[0]['sha']
    entry = found[0]
    if not isinstance(entry.get('sha'), str) or not SHA.fullmatch(entry['sha']) or \
            type(entry.get('size')) is not int or not 0 < entry['size'] <= package.TABLE_MAX:
        raise ValueError('Недопустимая запись дерева GitHub')
    return entry


def download(opener, revision: str, entry: dict, target: Path) -> str:
    """Stream the raw file; kept only if its size and Git blob are the tree's. Returns SHA-256."""
    blob, content, size = hashlib.sha1(b'blob %d\0' % entry['size']), hashlib.sha256(), 0
    with opener(RAW + revision + '/' + urllib.parse.quote(package.TABLE_PATH, safe='/')) as response, \
            target.open('xb') as output:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > entry['size']:
                raise ValueError('Загрузка больше размера из дерева')
            blob.update(block)
            content.update(block)
            output.write(block)
    if size != entry['size']:
        raise ValueError('Не совпал размер загрузки')
    if blob.hexdigest() != entry['sha']:
        raise ValueError('Загрузка не совпала с Git blob')
    return content.hexdigest()


def fetch(native: Path, revision: str | None = None, *, opener=_open) -> dict:
    native = Path(native)
    reject_links(native)
    native.mkdir(parents=True, exist_ok=True)
    revision, commit = resolve(opener, revision)
    entry = locate(opener, commit['tree']['sha'])
    message = commit.get('message')
    date = (commit.get('committer') or {}).get('date')
    with tempfile.TemporaryDirectory(dir=native, prefix='.fetch-') as temporary:
        staged = Path(temporary) / package.DATABASE
        digest = download(opener, revision, entry, staged)
        record = dict(repository=package.TABLE_REPOSITORY, path=package.TABLE_PATH, revision=revision,
                      blob=entry['sha'], size=entry['size'], sha256=digest,
                      data_version=(message.splitlines() or [None])[0][:128] if isinstance(message, str) else None,
                      data_date=date[:64] if isinstance(date, str) else None)
        return package.install_table(staged, record, native)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('fetch', 'show'))
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument('--root', help='portable folder WuWaRussian (uses Data/Native)')
    where.add_argument('--native', help='Data/Native folder')
    parser.add_argument('--revision', help='full SHA-1 of the source revision; default: the default branch head')
    args = parser.parse_args(argv)
    native = Path(args.native) if args.native else Path(args.root) / 'Data' / 'Native'
    if args.command == 'fetch':
        record = fetch(native, args.revision)
        print('Таблица принята:', package.table_label(record))
        print('Коммит:', record['data_version'] or 'UNKNOWN')
        print('SHA-256:', record['sha256'])
        print('«Играть» соберёт пакет заново. Версию игры с новой таблицей нужно принять заново.')
    else:
        record = package.table_record(native)
        print(package.table_label(record), '(закреплённый снимок)' if record == package.PINNED_TABLE else '')
        print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONUTF8', '1')
    raise SystemExit(main())
