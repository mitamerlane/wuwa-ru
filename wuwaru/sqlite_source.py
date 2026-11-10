"""Strict, bounded, read-only inspection of standalone localization SQLite files."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from collections.abc import Iterable, Mapping

from .core_contracts import DatabaseValidation, SourceRecord
from .manifest import check_cancel, fingerprint
from .paths import reject_links

MAX_DATABASE = 256 * 1024 * 1024
MAX_ROWS = 2_000_000


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def inspect_database(path: Path, *, independent: bool = False,
                     max_rows: int = MAX_ROWS,
                     cancelled: threading.Event | None = None) -> dict:
    """Two query strategies compare the same canonical typed-ID/content digest.

    The second pass adds a reverse ID/text preflight on a fresh connection. Both use
    the same canonical digest query and SQLite engine, not independent SQLite parsers.
    No schema SQL is executed and no views/triggers/virtual tables are accepted.
    """
    check_cancel(cancelled)
    path = Path(path).absolute()
    reject_links(path)
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = path.with_name(path.name + suffix)
        reject_links(sidecar)
        if sidecar.exists():
            raise ValueError('Нужна самостоятельная SQLite без WAL/journal')
    if not 16 <= path.stat().st_size <= MAX_DATABASE:
        raise ValueError('Недопустимый размер SQLite')
    before = fingerprint(path, True, cancelled)
    tables = []
    steps = 0

    def progress():
        nonlocal steps
        check_cancel(cancelled)
        steps += 1
        return int(steps > 200_000)  # bounded VM work, including integrity_check

    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4 * 1024 * 1024)
            db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 256)
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA cache_size=-4096')
            db.set_progress_handler(progress, 1000)
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite integrity_check не пройден')
            schema = db.execute(
                "SELECT type, name, sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' "
                'ORDER BY name').fetchall()
            if len(schema) > 256:
                raise ValueError('Слишком много объектов SQLite')
            total = 0
            for kind, name, sql in schema:
                if kind == 'index':
                    continue
                if kind != 'table' or not sql or 'CREATE VIRTUAL' in sql.upper():
                    raise ValueError('Неподдерживаемый объект схемы SQLite')
                columns = db.execute(f'PRAGMA table_info({quote(name)})').fetchall()
                names = [c[1] for c in columns]
                if not {'Id', 'Content'}.issubset(names):
                    raise ValueError(f'Нет Id/Content: {name}')
                count = db.execute(f'SELECT count(*) FROM {quote(name)}').fetchone()[0]
                total += count
                if total > max_rows:
                    raise ValueError('Превышен лимит записей SQLite')
                if independent:
                    reverse_count = 0
                    for identifier, content in db.execute(
                            f'SELECT Id,Content FROM {quote(name)} ORDER BY Id DESC,Content DESC'):
                        check_cancel(cancelled)
                        if type(identifier) not in (int, str) or not isinstance(content, str):
                            raise ValueError('Недопустимые типы при обратной проверке')
                        reverse_count += 1
                    if reverse_count != count:
                        raise ValueError('Количество записей при обратной проверке изменилось')
                # SQLite performs the sort on disk if necessary; never retain all IDs.
                duplicate = db.execute(f'SELECT 1 FROM {quote(name)} GROUP BY typeof(Id), Id '
                                       'HAVING count(*) > 1 LIMIT 1').fetchone()
                if duplicate:
                    raise ValueError('Повторяющийся типизированный ID')
                digest = hashlib.sha256()
                seen = 0
                order = ' ORDER BY typeof(Id), CAST(Id AS TEXT) COLLATE BINARY'
                for identifier, content in db.execute(
                        f'SELECT Id, Content FROM {quote(name)}' + order):
                    check_cancel(cancelled)
                    if type(identifier) not in (int, str) or not isinstance(content, str):
                        raise ValueError('ID должен быть int/text, Content — text')
                    if isinstance(identifier, str) and not identifier:
                        raise ValueError('Пустой ID')
                    key = ('integer' if type(identifier) is int else 'text', identifier)
                    value = hashlib.sha256(content.encode('utf-8')).hexdigest()
                    digest.update(json.dumps([*key, value], ensure_ascii=False,
                                             separators=(',', ':')).encode('utf-8') + b'\n')
                    seen += 1
                if seen != count:
                    raise ValueError('Количество записей SQLite изменилось')
                tables.append({'name': name, 'schema_sql': sql, 'columns': columns,
                               'rows': count, 'records_sha256': digest.hexdigest()})
            if not tables or total == 0:
                raise ValueError('Пустая локализационная SQLite')
    except sqlite3.Error as exc:
        check_cancel(cancelled)
        raise ValueError(f'Некорректная или слишком сложная SQLite: {exc}') from exc
    if fingerprint(path, True, cancelled) != before:
        raise RuntimeError('SQLite изменилась во время проверки')
    return {'sha256': before['sha256'], 'size': before['size'], 'tables': tables,
            'rows': total, 'integrity': 'ok'}


class ReadOnlySQLiteSourceAdapter:
    """Typed adapter over the existing strict SQLite inspection contract."""

    def inspect_schema(self, path: Path) -> Mapping[str, tuple[str, ...]]:
        inspection = inspect_database(path)
        return {table['name']: tuple(column[1] for column in table['columns'])
                for table in inspection['tables']}

    def validate_database(self, path: Path) -> DatabaseValidation:
        inspection = inspect_database(path)
        schema = [(table['name'], table['schema_sql'],
                   [column[1] for column in table['columns']])
                  for table in inspection['tables']]
        schema_sha256 = hashlib.sha256(json.dumps(
            schema, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        return DatabaseValidation(inspection['sha256'], inspection['rows'], schema_sha256)

    def import_records(self, path: Path) -> Iterable[SourceRecord]:
        inspection = inspect_database(path)
        path = Path(path).absolute()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA cache_size=-4096')
            for table in inspection['tables']:
                for identifier, content in db.execute(
                        f'SELECT Id, Content FROM {quote(table["name"])}'):
                    yield SourceRecord(table['name'], identifier, content)

    def compare_source_records(self, left: Path, right: Path) -> bool:
        def signature(path: Path):
            inspection = inspect_database(path)
            return tuple((table['name'], table['rows'], table['records_sha256'])
                         for table in inspection['tables'])
        return signature(left) == signature(right)
