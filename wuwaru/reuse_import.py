"""Offline candidate import. Exact identity is evidence of alignment, never approval."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from .paths import relative_parts
from .public_text import invalid_constant, unique_object
from .translation_db import TranslationDB, digest, offline_path
from .validation import validate

# This bounded adapter serves small public dictionaries, not the gated full export.
MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_MATCHES = 2_000_000


def _text(value, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 65536:
        raise ValueError('Нужна непустая UTF-8 строка до 64 KiB')


def _load(path: Path):
    path = offline_path(path)
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Корпус превышает 16 MiB')
    corpus = json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique_object,
                        parse_constant=invalid_constant)
    if not isinstance(corpus, dict) or type(corpus.get('schema_version')) is not int or corpus['schema_version'] != 1:
        raise ValueError('Неизвестная схема reuse корпуса')
    provenance = corpus.get('provenance')
    if not isinstance(provenance, dict) or provenance.get('rights_confirmed') is not True:
        raise ValueError('Права на переиспользование не подтверждены')
    for field in ('url', 'revision', 'license', 'license_url'):
        _text(provenance.get(field))
    entries = corpus.get('entries')
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ValueError('Нужен список до 100000 пар')
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Нужен объект пары')
        _text(entry.get('source'), optional=True)
        _text(entry.get('target'))
        key = entry.get('key')
        if key is None:
            _text(entry.get('source'))
            continue
        if not isinstance(key, dict) or set(key) not in ({'id'}, {'resource', 'table', 'id'}):
            raise ValueError('Ключ: id либо resource/table/id')
        identifier = key['id']
        if type(identifier) is int:
            if not -(2**63) <= identifier < 2**63:
                raise ValueError('ID вне int64')
        elif isinstance(identifier, str):
            _text(identifier)
        else:
            raise ValueError('ID должен быть int64 или строкой')
        if 'resource' in key:
            _text(key['resource'])
            _text(key['table'])
            relative_parts(key['resource'])
    return hashlib.sha256(data).hexdigest(), provenance, entries


def import_reuse(core: TranslationDB, path: Path, *, apply: bool = False) -> dict:
    """Default preview; apply preserves every existing target and records provenance.

    A complete resource/table/typed ID AND byte-exact EN is EXACT_ID_SOURCE.
    Text-only dictionaries yield EXACT_SOURCE_ONLY candidates requiring context review.
    ID-only, changed EN, invalid targets and conflicts never populate target.
    rights_confirmed records the operator's evidenced decision, not a license oracle.
    """
    corpus_hash, provenance, entries = _load(path)
    candidates = []
    by_record = defaultdict(list)
    result = dict(schema_version=1, corpus_sha256=corpus_hash, entries=len(entries),
                  eligible_rows=0, applied_rows=0, ambiguous_rows=0,
                  manual_protected_rows=0, existing_protected_rows=0,
                  installation_allowed=False, network_sent=False)
    with core.db:
        # One consistent view; also prevents an editor writing between check and apply.
        core.db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        for ordinal, entry in enumerate(entries):
            key, source = entry.get('key'), entry.get('source')
            if key is None:
                rows = core.db.execute('SELECT * FROM records WHERE active=1 AND source_hash=? AND source=?',
                                       (digest(source), source)).fetchall()
            else:
                kind = 'integer' if type(key['id']) is int else 'text'
                sql = 'SELECT * FROM records WHERE active=1 AND id_type=? AND id_value=?'
                args = [kind, str(key['id'])]
                if 'resource' in key:
                    sql += ' AND resource=? AND table_name=?'
                    args += [key['resource'], key['table']]
                rows = core.db.execute(sql, args).fetchall()
            for row in rows or [None]:
                if row is None:
                    decision = 'UNMATCHED'
                elif source is None:
                    decision = 'ID_ONLY'
                elif row['source'] != source:
                    decision = 'SOURCE_CHANGED'
                elif key is not None and 'resource' not in key:
                    decision = 'UNSCOPED_ID'
                elif validate(source, entry['target']):
                    decision = 'INVALID'
                else:
                    decision = 'EXACT_ID_SOURCE' if key else 'EXACT_SOURCE_ONLY'
                candidate = dict(ordinal=ordinal, seq=row['seq'] if row else 0,
                                 decision=decision, alignment=decision, entry=entry)
                candidates.append(candidate)
                if len(candidates) > MAX_MATCHES:
                    raise ValueError('Слишком много совпадений корпуса')
                if decision.startswith('EXACT_'):
                    by_record[row['seq']].append(candidate)
        resources = set()
        for seq, matches in by_record.items():
            before = core.record(seq)
            targets = {m['entry']['target'] for m in matches}
            if before['manual']:
                result['manual_protected_rows'] += 1
                decision = 'MANUAL_PROTECTED'
            elif len(targets) != 1:
                result['ambiguous_rows'] += 1
                decision = 'AMBIGUOUS'
            elif before['target'] is not None:
                result['existing_protected_rows'] += 1
                decision = 'EXISTING_PROTECTED'
            else:
                result['eligible_rows'] += 1
                decision = 'APPLIED' if apply else 'ELIGIBLE'
                if apply:
                    core.db.execute("UPDATE records SET target=?,translated_hash=source_hash,"
                                    "provider=?,status='NEEDS_REVIEW' WHERE seq=?",
                                    (next(iter(targets)), 'reuse:' + corpus_hash, seq))
                    core.audit(seq, 'REUSE', before)
                    result['applied_rows'] += 1
                    resources.add(before['resource'])
            for match in matches:
                match['decision'] = decision
        if apply:
            core.db.execute('INSERT OR IGNORE INTO reuse_imports VALUES (?,?)',
                            (corpus_hash, json.dumps(provenance, ensure_ascii=False)))
            core.db.execute('DELETE FROM reuse_candidates WHERE corpus_hash=?', (corpus_hash,))
            core.db.executemany('INSERT INTO reuse_candidates VALUES (?,?,?,?,?)',
                ((corpus_hash, c['ordinal'], c['seq'], c['decision'],
                  json.dumps({**c['entry'], 'alignment': c['alignment']}, ensure_ascii=False)) for c in candidates))
            for resource in resources:
                core.db.execute('UPDATE resources SET generation=generation+1 WHERE name=?', (resource,))
        result['alignments'] = dict(Counter(c['alignment'] for c in candidates))
        result['decisions'] = dict(Counter(c['decision'] for c in candidates))
        result['matched_rows'] = len({c['seq'] for c in candidates if c['seq']})
    return result
