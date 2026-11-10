"""Copy staging databases, change Content only, independently compare and publish."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from itertools import zip_longest
from pathlib import Path
from threading import Event

from .manifest import check_cancel, fingerprint
from .paths import inside, reject_links
from .sqlite_source import inspect_database, quote
from .translation_db import TranslationDB, digest, offline_path, source_connection
from .validation import blocking_errors


def schema(db: sqlite3.Connection) -> list:
    return db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name').fetchall()


def verify_copy(source: Path, target: Path, cancelled: Event | None) -> int:
    """Fresh connections, full row comparison including typed IDs and non-Content data."""
    count = 0
    with closing(source_connection(source)) as src, closing(source_connection(target)) as dst:
        if schema(src) != schema(dst):
            raise ValueError('Экспорт изменил исходную схему')
        if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Экспорт повреждён')
        for (name,) in src.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
            columns = [c[1] for c in src.execute(f'PRAGMA table_info({quote(name)})')]
            if name.startswith('sqlite_'):
                # Internal SQLite tables must be unchanged too.
                sql = f'SELECT * FROM {quote(name)} ORDER BY 1'
                for original, translated in zip_longest(src.execute(sql), dst.execute(sql)):
                    check_cancel(cancelled)
                    if original != translated:
                        raise ValueError('Изменены внутренние данные SQLite')
                continue
            content_index = columns.index('Content')
            sql = f'SELECT * FROM {quote(name)} ORDER BY typeof(Id),CAST(Id AS TEXT) COLLATE BINARY'
            for original, translated in zip_longest(src.execute(sql), dst.execute(sql)):
                check_cancel(cancelled)
                if original is None or translated is None:
                    raise ValueError('Изменено количество записей')
                for index, (a, b) in enumerate(zip(original, translated)):
                    if index != content_index and (type(a) is not type(b) or a != b):
                        raise ValueError('Изменён ID или нетекстовое поле')
                errors = blocking_errors(original[content_index], translated[content_index])
                if errors:
                    raise ValueError('Некорректная строка экспорта: ' + ','.join(errors))
                count += 1
    return count


def export_databases(core: TranslationDB, name: str, *, allow_machine: bool = False,
                     cancelled: Event | None = None) -> dict:
    """Export only into a new directory inside the explicitly offline workspace."""
    check_cancel(cancelled)
    destination = offline_path(inside(core.workspace, name))
    if destination.exists():
        raise FileExistsError(destination)
    resources = core.db.execute('SELECT * FROM resources ORDER BY name').fetchall()
    if not resources:
        raise ValueError('Нет исходных SQLite')
    if core.db.execute("SELECT 1 FROM records WHERE source_type='PUBLIC_TEXTMAP' LIMIT 1").fetchone() or any(
            json.loads(r['inspection']).get('source_type') == 'PUBLIC_TEXTMAP' for r in resources):
        raise ValueError('PUBLIC_TEXTMAP не имеет доказанного соответствия игровой SQLite; нативный экспорт запрещён')
    destination.parent.mkdir(parents=True, exist_ok=True)
    allowed = {'APPROVED', 'REVIEWED'} | ({'MACHINE_TRANSLATED'} if allow_machine else set())
    report = dict(schema_version=1, mode='BUILD_OFFLINE', compatibility='UNKNOWN',
                  installation_allowed=False, allow_machine=allow_machine, rows=0, databases=[])
    with tempfile.TemporaryDirectory(prefix='.export-', dir=destination.parent) as temporary:
        folder = Path(temporary) / 'ru'
        folder.mkdir()
        with core.db:
            core.db.execute('BEGIN')  # a consistent localization-store snapshot
            from .qa import QualityAssurance
            quality = QualityAssurance(core).write(folder, cancelled=cancelled, allow_machine=allow_machine)
            if quality['blocked_rows']:
                raise ValueError('QA: есть критические ошибки или не проверенные переводы')
            report['qa'] = quality
            for resource in resources:
                check_cancel(cancelled)
                source = Path(resource['path'])
                inspection = inspect_database(source, cancelled=cancelled)
                if inspection['sha256'] != resource['sha256']:
                    raise ValueError('Staging изменился после импорта')
                target = inside(folder, resource['name'])
                target.parent.mkdir(parents=True, exist_ok=True)
                with closing(source_connection(source)) as src, closing(sqlite3.connect(target)) as dst:
                    src.backup(dst, pages=256, progress=lambda *_: check_cancel(cancelled))
                    dst.execute('PRAGMA trusted_schema=OFF')
                    dst.execute('PRAGMA cache_size=-8192')
                    with dst:
                        for table in inspection['tables']:
                            table_name = table['name']
                            # Original Id may have no index. Temporary expression index avoids O(N²).
                            index_name = '__wuwaru_' + uuid.uuid4().hex
                            dst.execute(f'CREATE INDEX {quote(index_name)} ON {quote(table_name)}(typeof(Id),Id)')
                            for identifier, content in src.execute(f'SELECT Id,Content FROM {quote(table_name)}'):
                                check_cancel(cancelled)
                                kind = 'integer' if type(identifier) is int else 'text'
                                row = core.db.execute('SELECT * FROM records WHERE resource=? AND table_name=? '
                                                       'AND id_type=? AND id_value=? AND active=1',
                                                       (resource['name'], table_name, kind, str(identifier))).fetchone()
                                if (row is None or row['source_hash'] != digest(content) or
                                        row['translated_hash'] != row['source_hash'] or row['status'] not in allowed):
                                    raise ValueError('Есть пропущенные, устаревшие или не проверенные переводы')
                                errors = blocking_errors(content, row['target'])
                                if errors:
                                    raise ValueError('Экспорт заблокирован: ' + ','.join(errors))
                                result = dst.execute(f'UPDATE {quote(table_name)} SET Content=? WHERE typeof(Id)=? AND Id=?',
                                                     (row['target'], kind, identifier))
                                if result.rowcount != 1:
                                    raise ValueError('Неоднозначный ID при экспорте')
                            dst.execute(f'DROP INDEX {quote(index_name)}')
                actual_rows = verify_copy(source, target, cancelled)
                active_rows = core.db.execute('SELECT count(*) FROM records WHERE resource=? AND active=1',
                                              (resource['name'],)).fetchone()[0]
                if actual_rows != inspection['rows'] or actual_rows != active_rows:
                    raise ValueError('Количество записей store/staging/export не совпало')
                if fingerprint(source, True, cancelled)['sha256'] != resource['sha256']:
                    raise ValueError('Staging изменился во время экспорта')
                with target.open('r+b') as stream:
                    os.fsync(stream.fileno())
                report['rows'] += actual_rows
                report['databases'].append(dict(name=resource['name'], source_sha256=resource['sha256'],
                                                 sha256=fingerprint(target, True, cancelled)['sha256'],
                                                 rows=actual_rows))
        with (folder / 'export-manifest.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        check_cancel(cancelled)
        reject_links(destination)
        if os.name != 'nt':
            raise OSError('Exclusive directory publication поддержана только на Windows')
        os.rename(folder, destination)
    return report
