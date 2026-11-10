"""Small SQLite index for large diagnostic ResourceCatalog JSONL exports."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Self

from .manifest import check_cancel
from .paths import reject_links


class ResourceCatalogIndex:
    """Build and query a disk-backed resource index without retaining it in RAM."""

    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        reject_links(self.path)
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        uri = self.path.as_uri() + '?mode=ro&immutable=1'
        self.db = sqlite3.connect(uri, uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA query_only=ON')
        complete = self.db.execute(
            "SELECT value FROM catalog_meta WHERE key='complete'").fetchone()
        if complete is None or complete['value'] != '1':
            self.db.close()
            raise ValueError('SQLite ResourceCatalog не завершён')

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.db.close()

    @staticmethod
    def _upper_prefix(prefix: str) -> str:
        points = list(map(ord, prefix))
        for index in range(len(points) - 1, -1, -1):
            if points[index] < 0x10FFFF:
                return ''.join(map(chr, points[:index] + [points[index] + 1]))
        return prefix + '\U0010ffff'

    def search(self, *, exact_path: str | None = None,
               path_prefix: str | None = None,
               extensions: set[str] | None = None,
               archive: str | None = None, include_pruned: bool = True,
               limit: int = 100) -> tuple[dict, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError('Лимит поиска должен быть от 1 до 10000')
        if exact_path is not None and path_prefix is not None:
            raise ValueError('Укажи exact_path или path_prefix, но не оба')
        clauses, values = [], []
        if exact_path is not None:
            clauses.append('path_key = ?')
            values.append(exact_path.replace('\\', '/').casefold())
        elif path_prefix is not None:
            prefix = path_prefix.replace('\\', '/').casefold()
            clauses.extend(('path_key >= ?', 'path_key < ?'))
            values.extend((prefix, self._upper_prefix(prefix)))
        if extensions is not None and not extensions:
            return ()
        if extensions is not None:
            normalized = sorted({ext.casefold() if ext.startswith('.') else '.' + ext.casefold()
                                 for ext in extensions})
            clauses.append('extension IN (' + ','.join('?' for _ in normalized) + ')')
            values.extend(normalized)
        if archive is not None:
            clauses.append('archive = ?')
            values.append(archive.replace('\\', '/'))
        if not include_pruned:
            clauses.append('is_pruned = 0')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        rows = self.db.execute(
            'SELECT type,archive,kind,priority_hint,overlap_candidate,overlaps_with,'
            'overlap_resolution,path,offset,header_size,size,stored_size,sha1_stored,'
            'index_sha256,compression,resolution FROM resources' + where +
            ' LIMIT ?', (*values, limit))
        return tuple(self._record(row) for row in rows)

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        record = {'type': row['type'], 'archive': row['archive'], 'kind': row['kind'],
                  'priority_hint': row['priority_hint'],
                  'overlap_candidate': bool(row['overlap_candidate']),
                  'overlaps_with': row['overlaps_with'], 'path': row['path']}
        if row['type'] == 'entry':
            record.update(offset=row['offset'], header_size=row['header_size'], size=row['size'],
                          stored_size=row['stored_size'], sha1_stored=row['sha1_stored'],
                          index_sha256=row['index_sha256'], compression=row['compression'],
                          overlap_resolution=row['overlap_resolution'])
        else:
            record['resolution'] = row['resolution']
        return record

    @classmethod
    def build(cls, manifest: Path, destination: Path, *, cancelled=None) -> Path:
        """Import a complete JSONL export atomically; source remains untouched."""
        source, target = Path(manifest).absolute(), Path(destination).absolute()
        reject_links(source)
        reject_links(target)
        if not source.is_file():
            raise FileNotFoundError(source)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        db = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.sqlite.tmp',
                                             delete=False) as handle:
                temporary = Path(handle.name)
            temporary.unlink()
            db = sqlite3.connect(temporary)
            db.executescript('''
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=FULL;
                CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE packages (archive TEXT PRIMARY KEY, record_json TEXT NOT NULL);
                CREATE TABLE resources (
                    id INTEGER PRIMARY KEY,
                    archive TEXT NOT NULL REFERENCES packages(archive),
                    path_key TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    priority_hint INTEGER NOT NULL DEFAULT 0,
                    is_pruned INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    overlap_candidate INTEGER NOT NULL,
                    overlaps_with TEXT,
                    overlap_resolution TEXT,
                    path TEXT NOT NULL,
                    offset INTEGER,
                    header_size INTEGER,
                    size INTEGER,
                    stored_size INTEGER,
                    sha1_stored TEXT,
                    index_sha256 TEXT,
                    compression TEXT,
                    resolution TEXT
                );
            ''')
            package_count = entry_count = pruned_count = 0
            complete = None
            with source.open('r', encoding='utf-8') as stream:
                for line_no, line in enumerate(stream, 1):
                    check_cancel(cancelled)
                    if not line.strip():
                        continue
                    if complete is not None:
                        raise ValueError('Данные JSONL после complete-записи')
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f'Некорректный JSONL в строке {line_no}') from exc
                    kind = record.get('type')
                    if kind == 'catalog':
                        if line_no != 1 or record.get('schema_version') != 1:
                            raise ValueError('Неизвестная схема JSONL ResourceCatalog')
                    elif kind == 'package':
                        archive = record.get('archive')
                        normalized_archive = archive.replace('\\', '/') if isinstance(archive, str) else ''
                        if (not normalized_archive or normalized_archive.startswith('/')
                                or ':' in normalized_archive
                                or any(part in ('', '.', '..') for part in normalized_archive.split('/'))):
                            raise ValueError(f'Некорректный package в строке {line_no}')
                        try:
                            db.execute('INSERT INTO packages VALUES (?, ?)',
                                       (archive, json.dumps(record, ensure_ascii=False,
                                                            separators=(',', ':'))))
                        except sqlite3.IntegrityError as exc:
                            raise ValueError('Повторный package в JSONL') from exc
                        package_count += 1
                    elif kind in ('entry', 'pruned_entry'):
                        archive, path = record.get('archive'), record.get('path')
                        normalized_archive = archive.replace('\\', '/') if isinstance(archive, str) else ''
                        normalized_path = path.replace('\\', '/') if isinstance(path, str) else ''
                        if (not isinstance(archive, str) or not isinstance(path, str)
                                or not normalized_path or normalized_path.startswith('/')
                                or ':' in normalized_path
                                or any(part in ('', '.', '..') for part in normalized_path.split('/'))
                                or normalized_archive != archive
                                or db.execute('SELECT 1 FROM packages WHERE archive=?',
                                              (archive,)).fetchone() is None):
                            raise ValueError(f'Запись без package в строке {line_no}')
                        normalized = path.replace('\\', '/').casefold()
                        extension = Path(normalized).suffix.casefold()
                        db.execute('INSERT INTO resources '
                                   '(archive,path_key,extension,priority_hint,is_pruned,type,kind,'
                                   'overlap_candidate,overlaps_with,overlap_resolution,path,offset,'
                                   'header_size,size,stored_size,sha1_stored,index_sha256,compression,'
                                   'resolution) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (archive, normalized, extension,
                                    int(record.get('priority_hint', 0)), int(kind == 'pruned_entry'),
                                    kind, record.get('kind', ''),
                                    int(bool(record.get('overlap_candidate'))),
                                    record.get('overlaps_with'), record.get('overlap_resolution'),
                                    path, record.get('offset'), record.get('header_size'),
                                    record.get('size'), record.get('stored_size'),
                                    record.get('sha1_stored'), record.get('index_sha256'),
                                    record.get('compression'), record.get('resolution')))
                        if kind == 'entry':
                            entry_count += 1
                        else:
                            pruned_count += 1
                    elif kind == 'complete':
                        if complete is not None:
                            raise ValueError('Повторная complete-запись JSONL')
                        complete = record
                    else:
                        raise ValueError(f'Неизвестная запись JSONL в строке {line_no}')
            if (complete is None or complete.get('package_count') != package_count
                    or complete.get('available_entries') != entry_count):
                raise ValueError('JSONL ResourceCatalog не имеет согласованной complete-записи')
            check_cancel(cancelled)
            db.executescript('''
                CREATE INDEX resources_path_idx ON resources(path_key);
                CREATE INDEX resources_extension_idx ON resources(extension);
                CREATE INDEX resources_archive_idx ON resources(archive);
                CREATE INDEX resources_priority_idx ON resources(priority_hint, archive);
            ''')
            metadata = {'schema_version': 1, 'package_count': package_count,
                        'entry_count': entry_count, 'pruned_count': pruned_count}
            db.executemany('INSERT INTO catalog_meta VALUES (?,?)',
                           [('complete', '1'), ('metadata', json.dumps(metadata))])
            db.commit()
            check_cancel(cancelled)
            db.close()
            db = None
            with temporary.open('r+b') as output:
                os.fsync(output.fileno())
            os.rename(temporary, target)
            return target
        finally:
            if db is not None:
                db.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
