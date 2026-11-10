"""Independent whole-file json.load verification, separate from streaming adapter.

Research/acceptance command, not the production importer: it intentionally uses RAM
for one full JSON and its typed ID set, then compares every non-null source field.
No game resources are read and no translations are changed.
"""
import argparse
import collections
import hashlib
import json
import sqlite3
import sys
from itertools import zip_longest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wuwaru.paths import inside
from wuwaru.public_corpus import read_lock
from wuwaru.translation_db import offline_path


def verify(lock, source_name, source_root, workspace):
    source = read_lock(lock, source_name)
    source_root, workspace = offline_path(source_root), offline_path(workspace)
    db = sqlite3.connect(inside(workspace, 'translations.sqlite').as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    counts = collections.Counter()
    schemas = collections.Counter()
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity failed')
        for file in source['files']:
            raw = inside(source_root, file['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != file['sha256']:
                raise ValueError('Source digest failed')
            data = json.loads(raw.decode('utf-8-sig'))
            rows = data if isinstance(data, list) else [dict(Id=k, Content=v) for k, v in data.items()]
            ids = set()
            for row in rows:
                schemas[','.join(sorted(row))] += 1
                key = (type(row['Id']).__name__, str(row['Id']))
                if key in ids:
                    raise ValueError('Duplicate typed ID')
                ids.add(key)
                counts['raw_records'] += 1
                counts['null_content'] += row['Content'] is None
            resource = 'public/' + source['repository'].removeprefix('https://github.com/') + '/' + file['path']
            original = (r for r in rows if r['Content'] is not None)
            imported = db.execute('SELECT id_type,id_value,source,source_hash,source_metadata,source_type '
                                  'FROM records WHERE resource=? AND active=1 ORDER BY seq', (resource,))
            for row, actual in zip_longest(original, imported):
                if row is None or actual is None:
                    raise ValueError('Missing/extra imported row')
                metadata = {k: v for k, v in row.items() if k not in ('Id', 'Content')}
                expected = ('integer' if type(row['Id']) is int else 'text', str(row['Id']), row['Content'],
                            hashlib.sha256(row['Content'].encode('utf-8')).hexdigest())
                if actual[:4] != expected or json.loads(actual[4]) != metadata or actual[5] != 'PUBLIC_TEXTMAP':
                    raise ValueError('Content/ID/provenance mismatch')
                counts['verified_imported'] += 1
            counts['files'] += 1
        counts['duplicate_ids_within_file'] = 0
        result = dict(schema_version=1, source_name=source_name, revision=source['revision'],
                      method='independent json.load vs every imported ID/text/hash/metadata',
                      **counts, schemas=dict(schemas), game_mapping_proven=False)
        total = db.execute("SELECT count(*) FROM records WHERE active=1 AND source_type='PUBLIC_TEXTMAP'").fetchone()[0]
        if total != counts['verified_imported']:
            raise ValueError('Workspace contains another public corpus')
        return result
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path('research/public-text-sources.json'))
    parser.add_argument('--source-name', required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.lock, args.source_name, args.source_root, args.workspace), indent=2))
