"""Take a server delta into a project on the PC: verified files in, manual edits kept, backup first.

Moved from tools/server_update.py: the launcher applies deltas itself and the frozen app has no tools/ package.
`apply_update` runs with the app's database closed; any check that fails before the backup leaves the project untouched.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import shutil

from .public_corpus import import_corpus, read_lock
from .translation_db import TranslationDB
from .validation import validate

TEXT_REPOSITORY = 'Arikatsu/WutheringWaves_Data'
SOURCE_NAME = 'Arikatsu'
DATABASE = 'translations.sqlite'


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def apply_delta(core: TranslationDB, path: Path) -> dict:
    counts = dict(applied=0, manual=0, source_differs=0, already_translated=0, missing=0, invalid=0)
    keys = ('resource', 'table_name', 'id_type', 'id_value')
    with gzip.open(path, 'rt', encoding='utf-8') as stream, core.db:
        core.db.execute('BEGIN IMMEDIATE')
        for number, line in enumerate(stream):
            if number >= 2_000_000 or len(line) > 4 * 1024 * 1024:
                raise ValueError('Дельта больше допустимого')
            row = json.loads(line)
            if not isinstance(row, dict) or any(not isinstance(row.get(key), str) for key in (*keys, 'source_hash', 'target')):
                raise ValueError('Некорректная строка дельты')
            record = core.db.execute('SELECT * FROM records WHERE resource=? AND table_name=? AND id_type=? AND id_value=? '
                                     'AND active=1', tuple(row[key] for key in keys)).fetchone()
            if record is None:
                counts['missing'] += 1
            elif record['manual']:
                counts['manual'] += 1
            elif record['source_hash'] != row['source_hash']:
                counts['source_differs'] += 1
            elif (record['target'] is not None and record['translated_hash'] == record['source_hash'] and
                  record['status'] not in ('NEEDS_REVIEW', 'INVALID')):
                counts['already_translated'] += 1
            elif row.get('status') != 'MACHINE_TRANSLATED' or validate(record['source'], row['target']):
                counts['invalid'] += 1
            else:
                core.db.execute("UPDATE records SET target=?,translated_hash=source_hash,status='MACHINE_TRANSLATED',provider=? "
                                'WHERE seq=?', (row['target'], str(row.get('provider') or 'server-update')[:200], record['seq']))
                core.audit(record['seq'], 'SERVER_UPDATE', record)
                counts['applied'] += 1
    return counts


def apply_update(root: Path, workspace: Path, update: Path, *, fetcher=None) -> dict:
    """On the PC, app closed. Any check that fails before the backup leaves the project untouched."""
    from .portable import PathManager, create_project_backup

    update, workspace = Path(update), Path(workspace).absolute()
    summary = json.loads((update / 'summary.json').read_text(encoding='utf-8'))
    for name, key in (('delta.jsonl.gz', 'delta_sha256'), ('lock.json', 'lock_sha256')):
        if sha256(update / name) != summary[key]:
            raise ValueError('Контрольная сумма не совпала: ' + name)
    source = read_lock(update / 'lock.json', SOURCE_NAME)
    if source['repository'] != 'https://github.com/' + TEXT_REPOSITORY:
        raise ValueError('Обновление не из закреплённого источника')
    if not (workspace / DATABASE).is_file():
        raise FileNotFoundError('В папке нет базы проекта: ' + str(workspace))
    if fetcher is None:
        from .public_fetch import fetch as fetcher
    paths = PathManager(root)
    paths.initialize()
    lock = paths.sources / f'{SOURCE_NAME}-{source["revision"]}.json'
    shutil.copyfile(update / 'lock.json', lock)
    source_root = paths.sources / f'{TEXT_REPOSITORY.split("/")[1]}-{source["revision"]}'
    fetcher(lock, SOURCE_NAME, source_root)
    saved = create_project_backup(paths, workspace)
    core = TranslationDB(workspace)
    try:
        result = import_corpus(core, lock, source_root, SOURCE_NAME)
        delta = apply_delta(core, update / 'delta.jsonl.gz')
    finally:
        core.close()
    paths.save_public_source(SOURCE_NAME, lock, source_root, workspace)
    return {'status': 'APPLIED', 'revision': source['revision'], 'data_version': source['data_version'],
            'import': {key: result[key] for key in ('new', 'changed', 'unchanged', 'removed')}, 'delta': delta,
            'backup': str(saved['path'])}
