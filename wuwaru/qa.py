"""Streaming QA and atomic offline reports; no translation/network operations."""
from __future__ import annotations

import csv
import json
import os
import tempfile
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from threading import Event

from .glossary import Glossary
from .manifest import check_cancel
from .paths import inside, reject_links, relative_parts
from .translation_db import TranslationDB, STATUSES, CATEGORIES, digest, offline_path
from .validation import Issue, TranslationValidator


def csv_cell(value):
    # Excel must not interpret an untrusted game ID/term as a formula.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value


def output_path(core: TranslationDB, name: str) -> Path:
    parts = relative_parts(name)
    if len(parts) < 2 or parts[0] not in ('output', 'staging'):
        raise ValueError('Результат M3 должен находиться в output/ или staging/ рабочей папки')
    return offline_path(inside(core.workspace, name))


class QualityAssurance:
    def __init__(self, core: TranslationDB):
        self.core = core
        self.glossary = Glossary(core, version=Glossary(core).version)
        self.validator = TranslationValidator()
        self._resource = lru_cache(maxsize=32)(self._read_resource)

    def _read_resource(self, name):
        row = self.core.db.execute('SELECT inspection FROM resources WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def issues(self, row) -> list[Issue]:
        result = []
        try:
            if not all(isinstance(row[k], str) for k in ('source', 'source_hash', 'context', 'resource', 'table_name', 'id_value')):
                return [Issue('ERROR', 'RECORD_TYPE')]
            if row['source_hash'] != digest(row['source']):
                result.append(Issue('ERROR', 'SOURCE_HASH'))
            if row['status'] not in STATUSES or row['manual'] not in (0, 1):
                result.append(Issue('ERROR', 'RECORD_STATUS'))
            if not isinstance(row['category'], str) or row['category'] not in CATEGORIES:
                result.append(Issue('ERROR', 'CATEGORY'))
            resource = self._resource(row['resource'])
            if resource is None or row['table_name'] not in {t['name'] for t in resource['tables']}:
                result.append(Issue('ERROR', 'CONTEXT_RESOURCE'))
            original = self.core.db.execute('SELECT after_json FROM history WHERE seq=? AND event=\'IMPORT\' '
                                            'ORDER BY revision LIMIT 1', (row['seq'],)).fetchone()
            identity = ('resource', 'table_name', 'id_type', 'id_value')
            original_data = json.loads(original[0]) if original else None
            if original_data is None or any(original_data[k] != row[k] for k in identity):
                result.append(Issue('ERROR', 'ID'))
            try:
                valid_id = row['id_type'] == 'text' or (row['id_type'] == 'integer' and
                    str(int(row['id_value'])) == row['id_value'] and -(2**63) <= int(row['id_value']) < 2**63)
            except ValueError:
                valid_id = False
            if not valid_id:
                result.append(Issue('ERROR', 'ID_TYPE'))
            if not row['context'].strip() or len(row['context']) > 4096:
                result.append(Issue('ERROR', 'CONTEXT'))
            if self.core.db.execute('SELECT 1 FROM context_links l LEFT JOIN records r ON r.seq=l.related '
                                    'WHERE l.seq=? AND (r.seq IS NULL OR r.active<>1 OR l.seq=l.related) LIMIT 1',
                                    (row['seq'],)).fetchone():
                result.append(Issue('ERROR', 'CONTEXT_REFERENCE'))
            proper = False
            exact = self.core.db.execute("SELECT russian,category FROM glossary WHERE term=? AND verified=1 AND revision<=? "
                                          "AND context IN ('',?) ORDER BY (context=?) DESC,revision DESC LIMIT 1",
                                          (row['source'], self.glossary.version, row['context'], row['context'])).fetchone()
            if exact and exact['category'] in ('proper_name', 'character', 'location') and exact['russian'] == row['target']:
                proper = True
            result.extend(self.validator.check(row['source'], row['target'], proper_name=proper))
            if row['target'] is not None and row['translated_hash'] != row['source_hash']:
                result.append(Issue('ERROR', 'STALE'))
            if isinstance(row['target'], str):
                for term, russian in self.glossary.hints(row['source'], row['context']).items():
                    if russian.casefold() not in row['target'].casefold():
                        result.append(Issue('WARNING', 'GLOSSARY', term + ' → ' + russian))
                # Exact repeated source is a concept candidate, not a license to replace.
                variants = [self.core.db.execute('SELECT target FROM records WHERE source_hash=? AND active=1 '
                                                'AND target IS NOT NULL ORDER BY target ' + order + ' LIMIT 1',
                                                (row['source_hash'],)).fetchone() for order in ('ASC', 'DESC')]
                if any(v and v[0] != row['target'] for v in variants):
                    result.append(Issue('WARNING', 'INCONSISTENT', 'Разные переводы одинакового оригинала; проверь контекст'))
            if row['target'] is None or (isinstance(row['target'], str) and not row['target'].strip()):
                result = [issue for issue in result if issue.code not in ('MISSING', 'EMPTY')]
                result.append(Issue('INFO', 'UNTRANSLATED', 'Русский перевод ещё не заполнен'))
        except (ValueError, KeyError, TypeError, UnicodeError):
            result.append(Issue('ERROR', 'CORRUPT_RECORD'))
        return result

    def scan(self, *, cancelled: Event | None = None, progress=None):
        check_cancel(cancelled)
        if self.core.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Нарушена целостность рабочей SQLite')
        cursor, count = 0, 0
        while True:
            # Keyset cursor. No fetchall of text batches: one row may be up to 4 MiB.
            found = False
            for row in self.core.db.execute('SELECT * FROM records WHERE active=1 AND seq>? ORDER BY seq LIMIT 200', (cursor,)):
                check_cancel(cancelled)
                found = True
                cursor = row['seq']
                count += 1
                yield row, self.issues(row)
            if progress:
                progress(count)
            if not found:
                break

    def write(self, folder: Path, *, cancelled: Event | None = None, progress=None,
              allow_machine: bool = False) -> dict:
        summary = dict(schema_version=1, total=0, translated=0, untranslated_rows=0,
                       approved=0, unchecked=0,
                       ERROR=0, WARNING=0, INFO=0, error_rows=0, blocked_rows=0,
                       glossary_violations=0, categories={}, glossary_version=self.glossary.version,
                       evidence='offline_sources; linguistic_quality_not_measured',
                       source_types=dict(self.core.db.execute(
                           'SELECT source_type,count(*) FROM records WHERE active=1 GROUP BY source_type')),
                       installation_allowed=False)
        allowed = {'REVIEWED', 'APPROVED'} | ({'MACHINE_TRANSLATED'} if allow_machine else set())
        with (folder / 'issues.csv').open('x', encoding='utf-8-sig', newline='') as csv_file, \
                (folder / 'issues.json').open('x', encoding='utf-8') as json_file:
            writer = csv.writer(csv_file)
            writer.writerow(('seq', 'resource', 'table', 'id_type', 'id', 'severity', 'code', 'detail'))
            json_file.write('[')
            first = True
            for row, issues in self.scan(cancelled=cancelled, progress=progress):
                translated = isinstance(row['target'], str) and bool(row['target'].strip())
                untranslated = row['target'] is None or (isinstance(row['target'], str) and not row['target'].strip())
                approved = row['status'] == 'APPROVED'
                unchecked = row['status'] not in ('REVIEWED', 'APPROVED')
                error = any(i.severity == 'ERROR' for i in issues)
                blocked = error or row['status'] not in allowed
                for key, increment in (('total', 1), ('translated', translated),
                                       ('untranslated_rows', untranslated), ('approved', approved),
                                       ('unchecked', unchecked), ('error_rows', error), ('blocked_rows', blocked)):
                    summary[key] += increment
                category_name = row['category'] if row['category'] in CATEGORIES else 'CORRUPT_CATEGORY'
                category = summary['categories'].setdefault(category_name, dict(total=0, translated=0, approved=0, error_rows=0))
                for key, increment in (('total', 1), ('translated', translated), ('approved', approved), ('error_rows', error)):
                    category[key] += increment
                for issue in issues:
                    summary[issue.severity] += 1
                    summary['glossary_violations'] += issue.code == 'GLOSSARY'
                    writer.writerow(map(csv_cell, (row['seq'], row['resource'], row['table_name'], row['id_type'], row['id_value'],
                                                  issue.severity, issue.code, issue.detail)))
                    if not first:
                        json_file.write(',\n')
                    first = False
                    json.dump(dict(seq=row['seq'], resource=row['resource'] if isinstance(row['resource'], str) else None,
                                   id_type=row['id_type'] if isinstance(row['id_type'], str) else None,
                                   id=row['id_value'] if isinstance(row['id_value'], str) else None,
                                   **asdict(issue)), json_file, ensure_ascii=True)
            json_file.write(']\n')
        for category in summary['categories'].values():
            category['coverage_percent'] = round(100 * category['translated'] / category['total'], 2)
        with (folder / 'qa.json').open('x', encoding='utf-8') as stream:
            json.dump(summary, stream, ensure_ascii=False, indent=2)
        with (folder / 'coverage.csv').open('x', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(('category', 'total', 'translated', 'approved', 'error_rows', 'coverage_percent'))
            for name, category in summary['categories'].items():
                writer.writerow(map(csv_cell, (name, *category.values())))
        return summary

    def report(self, name: str, *, cancelled: Event | None = None, progress=None) -> dict:
        check_cancel(cancelled)
        destination = output_path(self.core, name)
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.qa-', dir=destination.parent) as temporary:
            folder = Path(temporary) / 'report'
            folder.mkdir()
            with self.core.db:
                self.core.db.execute('BEGIN')
                summary = self.write(folder, cancelled=cancelled, progress=progress)
            check_cancel(cancelled)
            reject_links(destination)
            if os.name != 'nt':
                raise OSError('Exclusive directory publication поддержана только на Windows')
            os.rename(folder, destination)
        return summary
