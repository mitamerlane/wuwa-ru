"""Bounded offline EN→RU merge with explicit schemas and append-only import journals."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from .manifest import check_cancel, fingerprint
from .paths import relative_parts, reject_links
from .public_text import iter_textmap, unique_object, invalid_constant
from .reuse_import import _text
from .sqlite_source import quote
from .translation_db import TranslationDB, digest, offline_path, source_connection
from .validation import validate

MAX_FILE = 256 * 1024 * 1024
MAX_ROWS = 2_000_000
MAX_MATCHES = 2_000_000
FIELDS = {'id', 'source', 'target', 'context', 'version', 'resource', 'table', 'author'}


def packed(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, RecursionError) as exc:
        raise ValueError('Метаданные должны быть ограниченным JSON без бинарных полей') from exc


def read_config(path: Path) -> dict:
    path = offline_path(path)
    with path.open('rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('Схема превышает 64 KiB')
    return json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique_object,
                      parse_constant=invalid_constant)


def _config(config):
    # Round-trip also detaches caller-owned dictionaries during a long import.
    config = json.loads(packed(config))
    if not isinstance(config, dict):
        raise ValueError('Схема должна быть объектом')
    if config.get('id_type', 'native') not in ('native', 'integer', 'text'):
        raise ValueError('id_type: native, integer или text')
    if (type(config.get('schema_version')) is not int or config['schema_version'] != 1
            or config.get('format') not in ('json', 'csv', 'sqlite')):
        raise ValueError('Неизвестная схема импорта')
    fields, defaults, provenance = config.get('fields'), config.get('defaults', {}), config.get('provenance')
    if (not isinstance(fields, dict) or 'target' not in fields or set(fields) - FIELDS
            or not isinstance(defaults, dict) or set(defaults) - FIELDS):
        raise ValueError('Нужны явные fields и defaults')
    for path in fields.values():
        if not isinstance(path, (str, list)) or not path:
            raise ValueError('Поле — имя либо список вложенных имён')
        for part in ([path] if isinstance(path, str) else path):
            _text(part)
    if not isinstance(provenance, dict) or provenance.get('rights_confirmed') is not True:
        raise ValueError('Права на предполагаемое использование не подтверждены')
    for key in ('name', 'url', 'version', 'author', 'rights_evidence', 'usage'):
        _text(provenance.get(key))
    return config


def _rows(path, config, cancelled):
    fmt = config['format']
    if fmt == 'json':
        yield from iter_textmap(path, cancelled=cancelled, textmap=False)
    elif fmt == 'csv':
        delimiter = config.get('delimiter', ',')
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise ValueError('CSV delimiter должен быть одним символом')
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream, delimiter=delimiter, strict=True)
            names = reader.fieldnames
            if not names or len(names) != len(set(names)):
                raise ValueError('Пустой или повторяющийся заголовок CSV')
            try:
                for row in reader:
                    if None in row or None in row.values():
                        raise ValueError('Число полей CSV не совпадает с заголовком')
                    yield row
            except csv.Error as exc:
                raise ValueError('Некорректный или слишком большой CSV field') from exc
    else:
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = path.with_name(path.name + suffix)
            reject_links(sidecar)
            if sidecar.exists():
                raise ValueError('Нужен самостоятельный SQLite без sidecar')
        table = config.get('table')
        _text(table)
        with closing(source_connection(path)) as src:
            calls = 0
            def progress():
                nonlocal calls
                check_cancel(cancelled)
                calls += 1
                return int(calls > 200_000)
            src.set_progress_handler(progress, 1000)
            src.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4 * 1024 * 1024)
            objects = src.execute('SELECT type,name,sql FROM sqlite_schema LIMIT 257').fetchall()
            if len(objects) > 256 or any(t not in ('table', 'index') or
                    (sql and 'virtual' in sql.casefold()) for t, _, sql in objects):
                raise ValueError('Views/triggers/virtual tables/сложная схема запрещены')
            if table not in {name for t, name, _ in objects if t == 'table'}:
                raise ValueError('Нет выбранной таблицы SQLite')
            if src.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Повреждённый SQLite')
            cursor = src.execute('SELECT * FROM ' + quote(table))
            names = [c[0] for c in cursor.description]
            for row in cursor:
                yield dict(zip(names, row))


def _entry(raw, config):
    entry = dict(config.get('defaults', {}))
    for field, path in config['fields'].items():
        value = raw
        for name in ([path] if isinstance(path, str) else path):
            value = value.get(name) if isinstance(value, dict) else None
        if value is not None and value != '':
            entry[field] = value
    for field in FIELDS - {'id'}:
        _text(entry.get(field), optional=field != 'target')
    identifier = entry.get('id')
    if identifier is not None:
        kind = config.get('id_type', 'native')
        if kind == 'integer' and isinstance(identifier, str) and re.fullmatch(r'-?(0|[1-9][0-9]*)', identifier):
            identifier = entry['id'] = int(identifier)
        if (kind == 'integer' and type(identifier) is not int) or (kind == 'text' and not isinstance(identifier, str)):
            raise ValueError('Тип ID не соответствует явному id_type')
        if type(identifier) is int:
            if not -(2**63) <= identifier < 2**63:
                raise ValueError('ID вне int64')
        elif isinstance(identifier, str):
            _text(identifier)
        else:
            raise ValueError('ID должен быть int64 или строкой')
    if 'resource' in entry:
        relative_parts(entry['resource'])
    if not entry.get('source') and identifier is None:
        raise ValueError('Нужен EN или ID; ID без EN сохраняется только кандидатом')
    entry['author'] = entry.get('author', config['provenance']['author'])
    # Raw fields preserve unknown export metadata; never execute or display as rich text.
    entry['raw'] = raw
    if len(packed(entry).encode('utf-8')) > 4 * 1024 * 1024:
        raise ValueError('Запись превышает 4 MiB')
    return entry


def _matches(core, entry):
    where, args = ['active=1'], []
    if 'id' in entry:
        where += ['id_type=?', 'id_value=?']
        args += ['integer' if type(entry['id']) is int else 'text', str(entry['id'])]
    elif entry.get('source'):
        where += ['source_hash=?', 'source=?']
        args += [digest(entry['source']), entry['source']]
    for key, column in (('resource', 'resource'), ('table', 'table_name'), ('context', 'context')):
        if key in entry:
            where.append(column + '=?')
            args.append(entry[key])
    return core.db.execute('SELECT * FROM records WHERE ' + ' AND '.join(where) + ' ORDER BY seq', args)


def _alignment(row, entry, versions, count):
    if row is None:
        return 'UNMATCHED', 'NO_MATCH'
    if not entry.get('source'):
        return 'AMBIGUOUS', 'MISSING_EN'
    if entry['source'] != row['source']:
        return 'UNMATCHED', 'SOURCE_CHANGED'
    version = versions[row['resource']]
    if not entry.get('version') or not version:
        return 'UNMATCHED', 'VERSION_UNKNOWN'
    if entry['version'] != version:
        return 'UNMATCHED', 'VERSION_CHANGED'
    if count != 1 or ('id' not in entry and 'context' not in entry):
        return 'AMBIGUOUS', 'IDENTITY_AMBIGUOUS'
    return ('EXACT' if 'id' in entry else 'CONTEXT_MATCH'), 'ELIGIBLE'


def merge_import(core: TranslationDB, path: Path, config: dict, *, apply=False,
                 expected: str | None = None, cancelled=None) -> dict:
    """Preview is read-only for the core DB; apply requires its unchanged preview hash.

    Files are normalized into a bounded disk spool; no full corpus in Python RAM.
    A version is original-EN snapshot metadata, distinct from translation provenance.version.
    """
    check_cancel(cancelled)
    config, path = _config(config), offline_path(path)
    if not 0 < path.stat().st_size <= MAX_FILE:
        raise ValueError('Размер источника должен быть 1..256 MiB')
    before_file = fingerprint(path, True, cancelled)
    with tempfile.TemporaryDirectory(prefix='merge-', dir=core.workspace) as temporary:
        with closing(sqlite3.connect(Path(temporary) / 'spool.sqlite')) as spool:
            spool.execute('PRAGMA cache_size=-4096')
            spool.execute('PRAGMA temp_store=FILE')
            spool.execute('CREATE TABLE entries(ordinal INTEGER PRIMARY KEY, entry TEXT NOT NULL)')
            spool.execute('CREATE TABLE plan(ordinal INTEGER,seq INTEGER,alignment TEXT,decision TEXT, '
                          'target TEXT,before_json TEXT,conflict INTEGER DEFAULT 0,PRIMARY KEY(ordinal,seq))')
            spool.execute('CREATE INDEX plan_seq ON plan(seq,decision,target)')
            spool.execute('CREATE TABLE variants(target TEXT PRIMARY KEY)')
            size = 0
            for ordinal, raw in enumerate(_rows(path, config, cancelled)):
                check_cancel(cancelled)
                if ordinal >= MAX_ROWS:
                    raise ValueError('Превышен лимит 2 млн записей')
                entry = packed(_entry(raw, config))
                size += len(entry.encode('utf-8'))
                if size > 3 * MAX_FILE:
                    raise ValueError('Превышен лимит нормализованного корпуса')
                spool.execute('INSERT INTO entries VALUES (?,?)', (ordinal, entry))
            spool.commit()
            if fingerprint(path, True, cancelled) != before_file:
                raise ValueError('Источник изменился во время чтения')
            with core.db:
                core.db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
                report = _plan(core, spool, config, before_file['sha256'], cancelled)
                if apply:
                    if not expected or expected != report['preview_hash']:
                        raise ValueError('Предпросмотр устарел: нужен новый preview и его preview_hash')
                    _apply(core, spool, report, config, cancelled)
                report['coverage'] = coverage(core)
                return report


def _plan(core, spool, config, sha256, cancelled):
    resources = [dict(r) for r in core.db.execute('SELECT name,inspection,generation FROM resources ORDER BY name')]
    versions = {}
    for r in resources:
        inspection = json.loads(r['inspection'])
        versions[r['name']] = inspection.get('data_version') or inspection.get('resource_version')
    report = dict(schema_version=1, source_sha256=sha256, entries=0, applied_rows=0,
                  eligible_rows=0, conflict_rows=0, manual_protected_rows=0,
                  installation_allowed=False, network_sent=False)
    token = hashlib.sha256(packed([config, sha256, resources,
        core.db.execute('SELECT coalesce(max(revision),0) FROM history').fetchone()[0],
        [tuple(r) for r in core.db.execute('SELECT id,state FROM merge_imports ORDER BY id')]]).encode('utf-8'))
    matches = 0
    for ordinal, serialized in spool.execute('SELECT * FROM entries ORDER BY ordinal'):
        check_cancel(cancelled)
        entry = json.loads(serialized)
        report['entries'] += 1
        # Count without fetching huge matching sets into memory.
        count = 0
        for _ in _matches(core, entry):
            check_cancel(cancelled)
            count += 1
            if matches + count > MAX_MATCHES:
                raise ValueError('Превышен лимит 2 млн совпадений')
        if matches + max(1, count) > MAX_MATCHES:
            raise ValueError('Превышен лимит 2 млн совпадений')
        rows = _matches(core, entry) if count else [None]
        for row in rows:
            check_cancel(cancelled)
            alignment, decision = _alignment(row, entry, versions, count)
            if decision == 'ELIGIBLE' and validate(entry['source'], entry['target']):
                decision = 'INVALID'
            seq, before = (row['seq'], packed(dict(row))) if row else (0, None)
            spool.execute('INSERT INTO plan(ordinal,seq,alignment,decision,target,before_json) VALUES (?,?,?,?,?,?)',
                          (ordinal, seq, alignment, decision, entry['target'], before))
            token.update(packed([ordinal, seq, alignment, decision, before]).encode('utf-8'))
            matches += 1
    for (seq,) in spool.execute("SELECT DISTINCT seq FROM plan WHERE decision='ELIGIBLE' ORDER BY seq"):
        check_cancel(cancelled)
        row = core.record(seq)
        spool.execute('DELETE FROM variants')
        spool.execute("INSERT OR IGNORE INTO variants SELECT target FROM plan WHERE seq=? AND decision='ELIGIBLE'", (seq,))
        # Persisted unresolved variants participate even when no target was selected.
        for (serialized,) in core.db.execute(
                "SELECT c.entry_json FROM merge_candidates c JOIN merge_imports i ON i.id=c.import_id "
                "WHERE c.seq=? AND i.state='APPLIED' AND c.alignment IN ('EXACT','CONTEXT_MATCH') "
                "AND c.decision<>'INVALID'", (seq,)):
            previous = json.loads(serialized)
            if (previous.get('source') == row['source'] and previous.get('version') == versions[row['resource']]
                    and previous.get('matched_context') == row['context']):
                spool.execute('INSERT OR IGNORE INTO variants VALUES (?)', (previous['target'],))
        if row['target'] is not None:
            spool.execute('INSERT OR IGNORE INTO variants VALUES (?)', (row['target'],))
        conflict = spool.execute('SELECT count(*) FROM variants').fetchone()[0] > 1
        report['conflict_rows'] += int(conflict)
        if row['manual']:
            decision = 'MANUAL_PROTECTED'
            report['manual_protected_rows'] += 1
        elif conflict:
            decision = 'CONFLICT'
        elif row['target'] is not None:
            decision = 'EXISTING_PROTECTED'
        else:
            decision = 'ELIGIBLE'
            report['eligible_rows'] += 1
        spool.execute("UPDATE plan SET decision=?,conflict=? WHERE seq=? AND decision='ELIGIBLE'", (decision, int(conflict), seq))
        token.update(packed([seq, decision]).encode('utf-8'))
        for (target,) in spool.execute('SELECT target FROM variants ORDER BY target'):
            token.update(packed(target).encode('utf-8'))
    report['alignments'] = dict(spool.execute('SELECT alignment,count(*) FROM plan GROUP BY alignment'))
    report['decisions'] = dict(spool.execute('SELECT decision,count(*) FROM plan GROUP BY decision'))
    report['sample'] = [dict(ordinal=o, seq=s, alignment=a, decision=d, target=t)
        for o, s, a, d, t in spool.execute('SELECT ordinal,seq,alignment,decision,target FROM plan ORDER BY ordinal,seq LIMIT 100')]
    report['sample_truncated'] = matches > 100
    report['preview_hash'] = token.hexdigest()
    return report


def _apply(core, spool, report, config, cancelled):
    import_id = uuid.uuid4().hex
    core.db.execute('INSERT INTO merge_imports(id,sha256,config_json,preview_hash,state) VALUES (?,?,?,?,?)',
                    (import_id, report['source_sha256'], packed(config), report['preview_hash'], 'APPLIED'))
    for (seq,) in spool.execute("SELECT DISTINCT seq FROM plan WHERE decision='ELIGIBLE' ORDER BY seq"):
        check_cancel(cancelled)
        before = core.record(seq)
        target = spool.execute("SELECT target FROM plan WHERE seq=? AND decision='ELIGIBLE' LIMIT 1", (seq,)).fetchone()[0]
        core.db.execute("UPDATE records SET target=?,translated_hash=source_hash,provider=?,status='NEEDS_REVIEW' WHERE seq=?",
                        (target, 'reuse:merge:' + import_id, seq))
        core.audit(seq, 'MERGE:' + import_id, before)
        report['applied_rows'] += 1
    # Each ordinal is kept, including duplicates, ambiguous, invalid and unmatched rows.
    for ordinal, seq, alignment, decision, conflict, before, entry in spool.execute(
            'SELECT p.ordinal,seq,alignment,decision,conflict,before_json,entry FROM plan p JOIN entries e USING(ordinal) ORDER BY p.ordinal,seq'):
        check_cancel(cancelled)
        data = json.loads(entry)
        data['matched_context'] = json.loads(before)['context'] if before else None
        entry = packed(data)
        after, revision = None, None
        if decision == 'ELIGIBLE':
            decision, after = 'APPLIED', packed(dict(core.record(seq)))
            revision = core.db.execute('SELECT max(revision) FROM history WHERE seq=?', (seq,)).fetchone()[0]
        else:
            before = None
        core.db.execute('INSERT INTO merge_candidates VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (import_id, ordinal, seq, alignment, decision, conflict, entry, before, after, revision))
    core.db.execute('UPDATE resources SET generation=generation+1 WHERE name IN '
                    '(SELECT DISTINCT r.resource FROM records r JOIN merge_candidates c ON c.seq=r.seq '
                    "WHERE c.import_id=? AND c.decision='APPLIED')", (import_id,))
    report['import_id'] = import_id
    report['decisions'] = dict(core.db.execute('SELECT decision,count(*) FROM merge_candidates WHERE import_id=? GROUP BY decision', (import_id,)))


def rollback_import(core: TranslationDB, import_id: str, *, cancelled=None) -> dict:
    """Full atomic reversal or refusal if any affected row has subsequent history.

    Journals remain for audit; newer edits and manual approvals are never overwritten.
    """
    with core.db:
        core.db.execute('BEGIN IMMEDIATE')
        state = core.db.execute('SELECT state FROM merge_imports WHERE id=?', (import_id,)).fetchone()
        if state is None:
            raise ValueError('Неизвестная сессия импорта')
        if state[0] == 'ROLLED_BACK':
            return dict(import_id=import_id, restored_rows=0, state=state[0])
        query = "SELECT seq,min(before_json),min(after_json),min(revision) FROM merge_candidates WHERE import_id=? AND decision='APPLIED' GROUP BY seq ORDER BY seq"
        count = 0
        for seq, before, after, revision in core.db.execute(query, (import_id,)):
            check_cancel(cancelled)
            current = core.record(seq)
            latest = core.db.execute('SELECT max(revision) FROM history WHERE seq=?', (seq,)).fetchone()[0]
            if packed(dict(current)) != after or latest != revision:
                raise ValueError('После импорта запись изменена; полный откат отменён, правки сохранены')
            old = json.loads(before)
            columns = ('target', 'translated_hash', 'provider', 'status', 'manual')
            core.db.execute('UPDATE records SET ' + ','.join(c + '=?' for c in columns) + ' WHERE seq=?',
                            [old[c] for c in columns] + [seq])
            core.audit(seq, 'MERGE_ROLLBACK:' + import_id, current)
            count += 1
        core.db.execute('UPDATE resources SET generation=generation+1 WHERE name IN '
                        '(SELECT DISTINCT r.resource FROM records r JOIN merge_candidates c ON c.seq=r.seq '
                        "WHERE c.import_id=? AND c.decision='APPLIED')", (import_id,))
        core.db.execute("UPDATE merge_imports SET state='ROLLED_BACK' WHERE id=?", (import_id,))
        return dict(import_id=import_id, restored_rows=count, state='ROLLED_BACK')


def candidates(core: TranslationDB, *, seq: int | None = None, import_id: str = '',
               after: int = 0, limit: int = 100, include_rolled_back=False) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100 or after < 0:
        raise ValueError('Страница кандидатов: 1..100')
    where, args = ['c.rowid>?'], [after]
    if seq is not None:
        where.append('c.seq=?')
        args.append(seq)
    if import_id:
        where.append('c.import_id=?')
        args.append(import_id)
    if not include_rolled_back:
        where.append("i.state='APPLIED'")
    result, size = [], 0
    for row in core.db.execute('SELECT c.rowid cursor,c.*,i.state,i.config_json,r.status current_status,r.target current_target '
            'FROM merge_candidates c JOIN merge_imports i ON i.id=c.import_id LEFT JOIN records r ON r.seq=c.seq WHERE '
            + ' AND '.join(where) + ' ORDER BY c.rowid LIMIT ?', args + [limit]):
        item = dict(row)
        item['provenance'] = json.loads(item.pop('config_json'))['provenance']
        item['entry'] = json.loads(item.pop('entry_json'))
        size += len(packed(item).encode('utf-8'))
        if result and size > 8 * 1024 * 1024:
            break
        result.append(item)
    return result


def coverage(core: TranslationDB) -> dict:
    from .engine import PENDING
    # Nonempty current EN only; stale translations are never counted as coverage.
    current = "target IS NOT NULL AND length(trim(target))>0 AND coalesce(translated_hash,'')=source_hash AND status<>'INVALID'"
    active = "active=1 AND length(trim(source))>0"
    total, unique = core.db.execute(f'SELECT count(*),count(DISTINCT source) FROM records WHERE {active}').fetchone()
    filled, reviewed = core.db.execute(f"SELECT count(*),coalesce(sum(status IN ('REVIEWED','APPROVED')),0) FROM records WHERE {active} AND {current}").fetchone()
    remaining, remaining_unique = core.db.execute(f'SELECT count(*),count(DISTINCT source) FROM records WHERE {active} AND NOT ({current})').fetchone()
    queue, queue_unique = core.db.execute(f'SELECT count(*),count(DISTINCT source) FROM records WHERE {active} AND {PENDING}').fetchone()
    return dict(schema_version=1, total_rows=total, unique_en=unique, filled_rows=filled,
                reviewed_rows=reviewed, remaining_rows=remaining, remaining_unique_en=remaining_unique,
                luna_queue_rows=queue, luna_queue_unique_en=queue_unique,
                filled_percent=round(100 * filled / total, 4) if total else 0,
                reviewed_percent=round(100 * reviewed / total, 4) if total else 0,
                native_coverage='UNKNOWN', installation_allowed=False, network_sent=False)
