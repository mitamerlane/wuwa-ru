"""Take a newer public English table for the Play package: one file of the pinned public source, by Git blob.

Moved from tools/native_table.py so the launcher can fetch it itself (the frozen app has no tools/ package).
See tools/native_table.py for the command line."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import urllib.parse
import urllib.request

from . import native_package as package
from .paths import reject_links

API = 'https://api.github.com/repos/' + package.TABLE_REPOSITORY
RAW = 'https://raw.githubusercontent.com/' + package.TABLE_REPOSITORY + '/'
MAX_JSON = 16 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{40}')
NAME = re.compile(r'[\w.-]{1,100}')
COMMITS = 30
GAME_VERSION = re.compile(r'\d{1,3}\.\d{1,3}\.\d{1,3}')
DATA_VERSION = re.compile(r'GameVer (\d{1,3}\.\d{1,3}\.\d{1,3}) \| ResVer \d{1,3}\.\d{1,3}\.\d{1,3} ')


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


def resolve(opener, revision: str | None = None, repository: str = package.TABLE_REPOSITORY) -> tuple[str, dict]:
    """Full revision and its commit; without `revision`, the head of the default branch."""
    api = 'https://api.github.com/repos/' + repository
    if revision is None:
        branch = _json(opener, api)['default_branch']
        if not isinstance(branch, str) or not NAME.fullmatch(branch) or '..' in branch:
            raise ValueError('Недопустимое имя ветки GitHub')
        ref = urllib.parse.quote(branch)
    elif SHA.fullmatch(revision):
        ref = revision
    else:
        raise ValueError('Ревизия должна быть полным SHA-1 из 40 знаков')
    commit = _json(opener, f'{api}/commits/{ref}')
    sha = commit.get('sha')
    if not isinstance(sha, str) or not SHA.fullmatch(sha) or (revision is not None and sha != revision):
        raise ValueError('GitHub вернул другую ревизию')
    return sha, commit['commit']


def locate(opener, tree: str, repository: str = package.TABLE_REPOSITORY, path: str = package.TABLE_PATH) -> dict:
    """Tree entry of the table: walk Text/ConfigDB/en one level at a time, names and hashes checked."""
    api = 'https://api.github.com/repos/' + repository
    *folders, name = path.split('/')
    for part in folders + [name]:
        if not isinstance(tree, str) or not re.fullmatch(r'[0-9A-Za-z]{1,64}', tree):
            raise ValueError('Недопустимое дерево GitHub')
        listing = _json(opener, f'{api}/git/trees/{tree}')
        if listing.get('truncated'):
            raise ValueError('GitHub обрезал список файлов')
        kind = 'blob' if part == name else 'tree'
        found = [entry for entry in listing['tree'] if entry.get('path') == part and entry.get('type') == kind]
        if len(found) != 1:
            raise ValueError(f'В ревизии нет {path}')
        if kind == 'tree':
            tree = found[0]['sha']
    entry = found[0]
    if not isinstance(entry.get('sha'), str) or not SHA.fullmatch(entry['sha']) or \
            type(entry.get('size')) is not int or not 0 < entry['size'] <= package.TABLE_MAX:
        raise ValueError('Недопустимая запись дерева GitHub')
    return entry


def download(opener, revision: str, entry: dict, target: Path, repository: str = package.TABLE_REPOSITORY,
             path: str = package.TABLE_PATH) -> str:
    """Stream the raw file; kept only if its size and Git blob are the tree's. Returns SHA-256."""
    blob, content, size = hashlib.sha1(b'blob %d\0' % entry['size']), hashlib.sha256(), 0
    url = f'https://raw.githubusercontent.com/{repository}/{revision}/' + urllib.parse.quote(path, safe='/')
    with opener(url) as response, \
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


def read_rows(source: Path) -> list[tuple[str, str, int]]:
    """MultiText.json of the text source: a list of {Id, Content, RedirectDbIndex}, nothing else, no repeated Id."""
    try:
        with source.open('rb') as stream:
            items = json.loads(stream.read().decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('MultiText.json не читается как UTF-8 JSON') from error
    if not isinstance(items, list) or not items:
        raise ValueError('MultiText.json должен быть непустым списком строк')
    rows, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'Id', 'Content', 'RedirectDbIndex'}:
            raise ValueError('Строка MultiText.json не из полей Id, Content, RedirectDbIndex')
        key, content, redirect = item['Id'], item['Content'], item['RedirectDbIndex']
        if not isinstance(key, str) or not key or not isinstance(content, str) or type(redirect) is not int:
            raise ValueError('Недопустимые типы в строке MultiText.json')
        if key in seen:
            raise ValueError('Повтор Id в MultiText.json')
        seen.add(key)
        rows.append((key, content, redirect))
    return rows


def write_table(rows: list[tuple[str, str, int]], target: Path) -> None:
    """The table as the public one is laid out: same DDL, 4096-byte pages, rows in source order."""
    database = sqlite3.connect(target)
    try:
        database.execute('PRAGMA page_size=4096')
        database.execute(package.TABLE_DDL)
        database.executemany('INSERT INTO MultiText VALUES (?,?,?)', rows)
        database.commit()
        stored = database.execute('SELECT Id, Content, RedirectDbIndex FROM MultiText ORDER BY rowid').fetchall()
    finally:
        database.close()
    if stored != rows:
        raise ValueError('Собранная таблица не совпала с исходными строками')


def build(native: Path, revision: str, *, opener=_open) -> dict:
    """Build the English table from the public text source at a full revision and install it.

    Used while the table source has nothing for a new game version. The JSON is taken by Git blob like the
    table is; every row is checked and read back. Whether it matches the installed client stays UNKNOWN."""
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError('Ревизия должна быть полным SHA-1 из 40 знаков')
    native = Path(native)
    reject_links(native)
    native.mkdir(parents=True, exist_ok=True)
    repository, path = package.TEXT_REPOSITORY, package.TEXT_PATH
    revision, commit = resolve(opener, revision, repository)
    entry = locate(opener, commit['tree']['sha'], repository, path)
    message = commit.get('message')
    date = (commit.get('committer') or {}).get('date')
    with tempfile.TemporaryDirectory(dir=native, prefix='.fetch-') as temporary:
        source, staged = Path(temporary) / 'MultiText.json', Path(temporary) / package.DATABASE
        download(opener, revision, entry, source, repository, path)
        write_table(read_rows(source), staged)
        blob, digest, size = package._digests(staged)
        record = dict(repository=repository, path=path, revision=revision, blob=blob, size=size, sha256=digest,
                      data_version=(message.splitlines() or [None])[0][:128] if isinstance(message, str) else None,
                      data_date=date[:64] if isinstance(date, str) else None, source_blob=entry['sha'])
        return package.install_table(staged, record, native)


def find_revision(opener, game_version: str) -> str:
    """Newest revision of the text source whose commit names this game version.

    The source titles its commits by the data they hold ('GameVer 3.8.0 | ResVer 3.8.9 | …') and GitHub lists
    them newest first; `game_version` is the name of the game's resources folder. A version the last COMMITS
    commits of the file do not name is refused, never replaced by a neighbouring one."""
    if not isinstance(game_version, str) or not GAME_VERSION.fullmatch(game_version):
        raise ValueError('Версия игры неизвестна')
    listing = _json(opener, f'https://api.github.com/repos/{package.TEXT_REPOSITORY}/commits?path='
                    + urllib.parse.quote(package.TEXT_PATH, safe='/') + f'&per_page={COMMITS}')
    if not isinstance(listing, list) or len(listing) > COMMITS:
        raise ValueError('Недопустимый список ревизий GitHub')
    for item in listing:
        commit = item.get('commit') if isinstance(item, dict) else None
        message = commit.get('message') if isinstance(commit, dict) else None
        if not isinstance(message, str) or not isinstance(item.get('sha'), str) or not SHA.fullmatch(item['sha']):
            raise ValueError('Недопустимая запись списка ревизий GitHub')
        named = DATA_VERSION.match(message)
        if named and named.group(1) == game_version:
            return item['sha']
    raise ValueError(f'В публичном тексте игры пока нет версии {game_version}')


def update(native: Path, game_version: str, *, opener=_open) -> dict:
    """Set `native` up with the table for this game version: {'record', 'changed'}.

    The launcher's own way to a table after a game update. Nothing is downloaded when the table in place was
    already built from the revision found. Whether it matches the installed client stays UNKNOWN."""
    revision = find_revision(opener, game_version)
    current = package.table_record(Path(native))
    if current.get('repository') == package.TEXT_REPOSITORY and current.get('revision') == revision:
        return {'record': current, 'changed': False}
    return {'record': build(native, revision, opener=opener), 'changed': True}
