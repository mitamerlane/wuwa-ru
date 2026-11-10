"""Offline localization store; stable typed keys, transactional import and audit trail."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from collections.abc import Iterable
from pathlib import Path
from threading import Event

from .manifest import check_cancel, fingerprint
from .paths import UnsafePath, inside, reject_links, relative_parts
from .sqlite_source import inspect_database, quote
from .core_contracts import ResourceSourceType
from .validation import blocking_errors

APPLICATION_ID = 0x57525532
STATUSES = ('UNTRANSLATED', 'MACHINE_TRANSLATED', 'NEEDS_REVIEW', 'REVIEWED', 'APPROVED', 'INVALID')
CATEGORIES = ('other', 'dialogue', 'quest', 'ui', 'character', 'location', 'ability', 'item')
MIGRATIONS = (
    (
        'CREATE TABLE resources (name TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, '
        'inspection TEXT NOT NULL, generation INTEGER NOT NULL)',
        "CREATE TABLE records (seq INTEGER PRIMARY KEY, resource TEXT NOT NULL, table_name TEXT NOT NULL, "
        "id_type TEXT NOT NULL CHECK(id_type IN ('integer','text')), id_value TEXT NOT NULL, "
        "context TEXT NOT NULL, source TEXT NOT NULL, source_hash TEXT NOT NULL, target TEXT, "
        "translated_hash TEXT, status TEXT NOT NULL DEFAULT 'UNTRANSLATED' CHECK(status IN "
        "('UNTRANSLATED','MACHINE_TRANSLATED','NEEDS_REVIEW','REVIEWED','APPROVED','INVALID')), "
        "manual INTEGER NOT NULL DEFAULT 0 CHECK(manual IN (0,1)), provider TEXT, "
        "active INTEGER NOT NULL DEFAULT 1, generation INTEGER NOT NULL, "
        "UNIQUE(resource,table_name,id_type,id_value))",
        'CREATE INDEX records_pending ON records(active,manual,status,seq)',
        'CREATE INDEX records_resource ON records(resource,active,seq)',
        'CREATE TABLE history (revision INTEGER PRIMARY KEY, seq INTEGER NOT NULL, event TEXT NOT NULL, '
        "before_json TEXT, after_json TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        'CREATE INDEX history_record ON history(seq,revision)',
    ),
    (
        'CREATE INDEX records_queue ON records(active,manual,seq)',
        'CREATE TABLE glossary (revision INTEGER PRIMARY KEY, term TEXT NOT NULL, context TEXT NOT NULL, '
        "russian TEXT NOT NULL, category TEXT NOT NULL, verified INTEGER NOT NULL, "
        "created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        'CREATE INDEX glossary_lookup ON glossary(term,context,revision DESC)',
        'CREATE TABLE cache (key TEXT PRIMARY KEY, target TEXT NOT NULL)',
        'CREATE TABLE runs (id TEXT PRIMARY KEY, signature TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, '
        "state TEXT NOT NULL DEFAULT 'RUNNING', processed INTEGER NOT NULL DEFAULT 0)",
        'CREATE TABLE usage (attempt INTEGER PRIMARY KEY, run_id TEXT NOT NULL, provider TEXT NOT NULL, '
        "estimated_cost REAL NOT NULL, state TEXT NOT NULL DEFAULT 'RESERVED', "
        "created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    ),
    (
        "ALTER TABLE records ADD COLUMN category TEXT NOT NULL DEFAULT 'other'",
        'CREATE INDEX records_category ON records(active,category,seq)',
        'CREATE INDEX records_status ON records(active,status,seq)',
        'CREATE INDEX records_id ON records(id_value,active,seq)',
        'CREATE INDEX records_concept ON records(source_hash,active,target)',
        'CREATE TABLE context_links (seq INTEGER NOT NULL, related INTEGER NOT NULL, '
        'PRIMARY KEY(seq,related))',
    ),
    (
        "ALTER TABLE records ADD COLUMN source_type TEXT NOT NULL DEFAULT 'SQLITE_STAGING'",
        "ALTER TABLE records ADD COLUMN source_metadata TEXT NOT NULL DEFAULT '{}'",
        'CREATE INDEX records_source_type ON records(active,source_type,seq)',
    ),
    (
        'CREATE TABLE source_snapshots (resource TEXT NOT NULL, generation INTEGER NOT NULL, '
        'inspection TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, '
        'PRIMARY KEY(resource,generation))',
        'INSERT INTO source_snapshots(resource,generation,inspection) SELECT name,generation,inspection FROM resources',
    ),
    (
        'CREATE TABLE reuse_imports (sha256 TEXT PRIMARY KEY, provenance_json TEXT NOT NULL)',
        'CREATE TABLE reuse_candidates (corpus_hash TEXT NOT NULL, ordinal INTEGER NOT NULL, '
        'seq INTEGER NOT NULL, decision TEXT NOT NULL, entry_json TEXT NOT NULL, '
        'PRIMARY KEY(corpus_hash,ordinal,seq))',
        'CREATE INDEX reuse_record ON reuse_candidates(seq,corpus_hash)',
    ),
    (
        'CREATE TABLE merge_imports (id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, '
        'config_json TEXT NOT NULL, preview_hash TEXT NOT NULL, '
        "state TEXT NOT NULL CHECK(state IN ('APPLIED','ROLLED_BACK')), "
        'created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)',
        'CREATE TABLE merge_candidates (import_id TEXT NOT NULL REFERENCES merge_imports(id), '
        'ordinal INTEGER NOT NULL, seq INTEGER NOT NULL, alignment TEXT NOT NULL, '
        'decision TEXT NOT NULL, conflict INTEGER NOT NULL DEFAULT 0, entry_json TEXT NOT NULL, before_json TEXT, after_json TEXT, '
        'revision INTEGER, PRIMARY KEY(import_id,ordinal,seq))',
        'CREATE INDEX merge_record ON merge_candidates(seq,import_id)',
    ),
    (
        'ALTER TABLE usage ADD COLUMN actual_usage_json TEXT',
    ),
    (
        'ALTER TABLE usage RENAME TO usage_old',
        'CREATE TABLE usage (attempt INTEGER PRIMARY KEY, run_id TEXT NOT NULL, provider TEXT NOT NULL, '
        "estimated_cost REAL, state TEXT NOT NULL DEFAULT 'RESERVED', "
        'created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, actual_usage_json TEXT)',
        'INSERT INTO usage SELECT * FROM usage_old',
        'DROP TABLE usage_old',
    ),
    (
        'CREATE TABLE scene_sources (path TEXT PRIMARY KEY, sha1 TEXT NOT NULL, archive TEXT NOT NULL)',
        "CREATE TABLE scene_lines (seq INTEGER PRIMARY KEY, ordinal INTEGER NOT NULL UNIQUE, scene INTEGER NOT NULL, "
        "kind TEXT NOT NULL CHECK(kind IN ('line','choice')), speaker TEXT)",
        "CREATE TABLE cast_members (name TEXT PRIMARY KEY, gender TEXT CHECK(gender IN ('F','M')), "
        "address TEXT CHECK(address IN ('ты','вы')), source TEXT, lines INTEGER NOT NULL DEFAULT 0, "
        'approved INTEGER NOT NULL DEFAULT 0 CHECK(approved IN (0,1)))',
    ),
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def offline_path(path: Path) -> Path:
    path = Path(path).absolute()
    reject_links(path)
    # Detect the established installation layout without launching or scanning it.
    for ancestor in (path, *path.parents):
        if (ancestor / 'Client/Content/Paks').is_dir() or (
                ancestor.name.casefold() == 'client' and (ancestor / 'Content/Paks').is_dir()):
            raise UnsafePath('Рабочая папка должна находиться вне игры')
    return path


def source_connection(path: Path) -> sqlite3.Connection:
    reject_links(path)
    db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro&immutable=1', uri=True)
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA temp_store=FILE')
    db.execute('PRAGMA cache_size=-4096')
    return db


class TranslationDB:
    def __init__(self, workspace: Path):
        self.workspace = offline_path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        path = inside(self.workspace, 'translations.sqlite')
        existed = path.exists()
        if existed and path.stat().st_nlink != 1:
            raise UnsafePath('Рабочая БД не должна быть hardlink')
        for suffix in ('-wal', '-shm', '-journal'):
            reject_links(path.with_name(path.name + suffix))
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        try:
            if existed and self.db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
                raise ValueError('Это не база Translation Core')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version > len(MIGRATIONS):
                raise ValueError('База создана более новой версией приложения')
            self.db.execute('PRAGMA trusted_schema=OFF')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA cache_size=-8192')
            self.db.execute('PRAGMA temp_store=FILE')
            for index in range(version, len(MIGRATIONS)):
                with self.db:
                    self.db.execute('BEGIN IMMEDIATE')
                    for statement in MIGRATIONS[index]:
                        self.db.execute(statement)
                    self.db.execute(f'PRAGMA application_id={APPLICATION_ID}')
                    self.db.execute(f'PRAGMA user_version={index + 1}')
            self.db.execute('PRAGMA journal_mode=WAL')
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def record(self, seq: int) -> sqlite3.Row:
        row = self.db.execute('SELECT * FROM records WHERE seq=?', (seq,)).fetchone()
        if row is None:
            raise ValueError('Неизвестный ID записи')
        return row

    def audit(self, seq: int, event: str, before: sqlite3.Row | None) -> None:
        self.db.execute('INSERT INTO history(seq,event,before_json,after_json) VALUES (?,?,?,?)',
                        (seq, event, json.dumps(dict(before), ensure_ascii=False) if before else None,
                         json.dumps(dict(self.record(seq)), ensure_ascii=False)))

    def import_source(self, resource: str, source: Path, *,
                      cancelled: Event | None = None,
                      source_type: ResourceSourceType = ResourceSourceType.SQLITE_STAGING,
                      source_metadata: dict[str, str] | None = None
                      ) -> dict[str, int]:
        """Import a standalone SQLite; optional origin metadata is explicit and typed.

        One resource is the snapshot boundary. A failure rolls back the whole import;
        removed resources require explicit removal through a later inventory adapter.
        """
        resource = '/'.join(relative_parts(resource))
        if not resource.endswith('.db') or len(resource) > 1024:
            raise ValueError('Ресурс должен иметь относительное имя .db')
        for (existing,) in self.db.execute('SELECT name FROM resources'):
            if existing.casefold() == resource.casefold() and existing != resource:
                raise ValueError('Коллизия регистра имён ресурсов')
        source = offline_path(source)
        if source.resolve().is_relative_to(self.workspace.resolve()):
            raise UnsafePath('Исходный staging должен быть вне рабочей папки')
        if not isinstance(source_type, ResourceSourceType):
            raise TypeError('source_type должен быть ResourceSourceType')
        if source_type == ResourceSourceType.PUBLIC_TEXTMAP:
            raise ValueError('PUBLIC_TEXTMAP импортируется только отдельным JSON-адаптером')
        metadata = dict(source_metadata or {})
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items()):
            raise TypeError('source_metadata должен быть строковым словарём')
        if source_type == ResourceSourceType.REAL_LOCAL_RESOURCE and not {
                'archive', 'entry_path', 'archive_index_sha256'}.issubset(metadata):
            raise ValueError('REAL_LOCAL_RESOURCE требует проверенное происхождение PAK entry')
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'))
        if len(metadata_json.encode('utf-8')) > 4096:
            raise ValueError('Слишком большие source_metadata')
        inspection = inspect_database(source, cancelled=cancelled)
        if source_type != ResourceSourceType.SQLITE_STAGING:
            inspection['source_type'] = source_type.value
        if metadata:
            inspection['source_metadata'] = metadata
        def rows():
            with closing(source_connection(source)) as src:
                for table in inspection['tables']:
                    name = table['name']
                    for identifier, content in src.execute(f'SELECT Id, Content FROM {quote(name)}'):
                        yield name, identifier, content, 'other', metadata_json
        return self.import_snapshot(resource, source, inspection, rows(), cancelled=cancelled)

    def import_snapshot(self, resource: str, source: Path, inspection: dict,
                        rows: Iterable[tuple[str, int | str, str | None, str, str]], *,
                        cancelled: Event | None = None) -> dict[str, int]:
        """Shared transactional store path for independently validated source adapters.

        Each file is one snapshot; adapters supply a bounded validated iterator.
        Public JSON never acquires SQLite provenance or native export eligibility.
        """
        resource = '/'.join(relative_parts(resource))
        if len(resource) > 1024:
            raise ValueError('Слишком длинное имя ресурса')
        for (existing,) in self.db.execute('SELECT name FROM resources'):
            if existing.casefold() == resource.casefold() and existing != resource:
                raise ValueError('Коллизия регистра имён ресурсов')
        try:
            source_type = ResourceSourceType(inspection.get(
                'source_type', ResourceSourceType.SQLITE_STAGING.value)).value
        except ValueError as exc:
            raise ValueError('Неизвестный тип источника Translation Core') from exc
        old = self.db.execute('SELECT * FROM resources WHERE name=?', (resource,)).fetchone()
        stored_source = str(source)
        # Shared portable corpora live next to Projects, outside each workspace.
        if (source_type == ResourceSourceType.PUBLIC_TEXTMAP.value and
                self.workspace.parent.name == 'Projects' and
                source.is_relative_to(self.workspace.parent.parent / 'Sources')):
            stored_source = os.path.relpath(source, self.workspace)
        if (old is not None and old['sha256'] == inspection['sha256'] and
                json.loads(old['inspection']) == json.loads(json.dumps(inspection))):
            with self.db:
                self.db.execute('UPDATE resources SET path=? WHERE name=?', (stored_source, resource))
            return dict(new=0, changed=0, unchanged=self.db.execute('SELECT count(*) FROM records WHERE resource=? AND active=1', (resource,)).fetchone()[0], removed=0)
        generation = old['generation'] + 1 if old else 1
        counts = dict(new=0, changed=0, unchanged=0, removed=0)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('CREATE TEMP TABLE IF NOT EXISTS import_ids '
                            '(kind TEXT, id TEXT, table_name TEXT, PRIMARY KEY(kind,id,table_name))')
            self.db.execute('DELETE FROM import_ids')
            for name, identifier, content, category, metadata in rows:
                check_cancel(cancelled)
                kind = 'integer' if type(identifier) is int else 'text'
                try:
                    self.db.execute('INSERT INTO import_ids VALUES (?,?,?)', (kind, str(identifier), name))
                except sqlite3.IntegrityError:
                    raise ValueError('Повторяющийся типизированный ID в источнике') from None
                if content is None:
                    continue
                key = (resource, name, kind, str(identifier))
                before = self.db.execute('SELECT * FROM records WHERE resource=? AND table_name=? '
                                         'AND id_type=? AND id_value=?', key).fetchone()
                source_hash = digest(content)
                if before is not None and before['source_type'] != source_type:
                    raise ValueError('Нельзя менять тип источника существующего ресурса')
                if before is None:
                    seq = self.db.execute(
                        'INSERT INTO records(resource,table_name,id_type,id_value,context,source,'
                        'source_hash,generation,category,source_type,source_metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (*key, resource + '/' + name, content, source_hash, generation, category,
                         source_type, metadata)).lastrowid
                    self.audit(seq, 'IMPORT', None)
                    counts['new'] += 1
                else:
                    changed = (before['source_hash'] != source_hash or not before['active'] or
                               before['source_metadata'] != metadata)
                    self.db.execute('UPDATE records SET source=?, source_hash=?, generation=?, active=1, '
                                    "source_metadata=?, status=CASE WHEN ? THEN 'NEEDS_REVIEW' ELSE status END WHERE seq=?",
                                    (content, source_hash, generation, metadata, changed, before['seq']))
                    if changed:
                        self.audit(before['seq'], 'SOURCE_CHANGED', before)
                    counts['changed' if changed else 'unchanged'] += 1
            # Keyset loop avoids retaining removed records or mutating a live cursor.
            cursor = 0
            while True:
                removed = self.db.execute('SELECT * FROM records WHERE resource=? AND active=1 '
                                          'AND generation<>? AND seq>? ORDER BY seq LIMIT 500',
                                          (resource, generation, cursor)).fetchall()
                if not removed:
                    break
                for before in removed:
                    check_cancel(cancelled)
                    self.db.execute('UPDATE records SET active=0 WHERE seq=?', (before['seq'],))
                    self.audit(before['seq'], 'REMOVED', before)
                    counts['removed'] += 1
                cursor = removed[-1]['seq']
            if fingerprint(source, True, cancelled)['sha256'] != inspection['sha256']:
                raise RuntimeError('Исходный файл изменился во время импорта')
            self.db.execute('INSERT INTO resources(name,path,sha256,inspection,generation) VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET '
                            'path=excluded.path,sha256=excluded.sha256,inspection=excluded.inspection,'
                            'generation=excluded.generation',
                            (resource, stored_source, inspection['sha256'],
                             json.dumps(inspection, ensure_ascii=False), generation))
            self.db.execute('INSERT INTO source_snapshots(resource,generation,inspection) VALUES (?,?,?)',
                            (resource, generation, json.dumps(inspection, ensure_ascii=False)))
        return counts

    def set_context(self, seq: int, context: str) -> None:
        """Refine table context with speaker/quest evidence; preserve protected edits."""
        if not isinstance(context, str) or not context or len(context) > 4096:
            raise ValueError('Нужен непустой контекст не длиннее 4096 символов')
        try:
            context.encode('utf-8')
        except UnicodeError:
            raise ValueError('Некорректный Unicode контекста') from None
        before = self.record(seq)
        if before['context'] == context:
            return
        with self.db:
            self.db.execute("UPDATE records SET context=?,status='NEEDS_REVIEW' WHERE seq=?", (context, seq))
            self.db.execute('UPDATE resources SET generation=generation+1 WHERE name=?', (before['resource'],))
            self.audit(seq, 'CONTEXT_CHANGED', before)

    def set_manual(self, seq: int, target: str, *, approved: bool = False) -> None:
        before = self.record(seq)
        errors = blocking_errors(before['source'], target)
        if not isinstance(target, str):
            raise ValueError('Ручной перевод должен быть строкой')
        try:
            target.encode('utf-8')
        except UnicodeError:
            raise ValueError('Ручной перевод содержит недопустимый Unicode') from None
        if errors and approved:
            raise ValueError('Нельзя утвердить перевод: ' + ','.join(errors))
        with self.db:
            self.db.execute('UPDATE records SET target=?, translated_hash=source_hash, manual=1, '
                            'provider=\'manual\', status=? WHERE seq=?',
                            (target, 'INVALID' if errors else ('APPROVED' if approved else 'REVIEWED'), seq))
            self.audit(seq, 'MANUAL', before)
