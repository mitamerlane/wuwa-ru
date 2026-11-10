"""Transactional M3 operations over the M2 store, without a Qt dependency."""
from __future__ import annotations

import json
from threading import Event

from .manifest import check_cancel
from .translation_db import TranslationDB, STATUSES, CATEGORIES, digest
from .validation import blocking_errors

PAGE_BYTES = 8 * 1024 * 1024


def row_bytes(row) -> int:
    return sum(len(v.encode('utf-8', errors='surrogatepass')) for v in dict(row).values() if isinstance(v, str))


class Conflict(ValueError):
    pass


class Editor:
    def __init__(self, core: TranslationDB):
        self.core = core
        core.db.create_function('casefold', 1, lambda s: s.casefold() if isinstance(s, str) else '')

    def version(self, seq: int) -> str:
        # Covers imports/context changes and edits from older M2 clients too.
        revision = self.core.db.execute('SELECT coalesce(max(revision),0) FROM history WHERE seq=?', (seq,)).fetchone()[0]
        return digest(json.dumps([dict(self.core.record(seq)), revision], sort_keys=True, ensure_ascii=False))

    def checked(self, seq: int, expected: str):
        row = self.core.record(seq)
        if self.version(seq) != expected:
            raise Conflict('Запись изменилась. Обнови строку и сравни правки.')
        if not row['active']:
            raise ValueError('Запись удалена из текущего источника')
        return row

    def page(self, *, query: str = '', category: str = '', status: str = '', resource: str = '',
             after: int = 0, limit: int = 200, severity: str = '', term: str = '',
             cancelled: Event | None = None, source_type: str = '', merge_filter: str = '') -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 200 or after < 0:
            raise ValueError('Страница должна содержать 1..200 строк')
        if severity not in ('', 'ERROR', 'WARNING', 'INFO'):
            raise ValueError('Неизвестный уровень QA')
        sql, params = ['active=1', 'seq>?'], [after]
        if merge_filter not in ('', 'CONFLICT', 'CANDIDATES'):
            raise ValueError('Неизвестный фильтр импорта')
        if merge_filter:
            sql.append("EXISTS (SELECT 1 FROM merge_candidates c JOIN merge_imports i ON i.id=c.import_id "
                       "WHERE c.seq=records.seq AND i.state='APPLIED'" +
                       (" AND c.conflict=1" if merge_filter == 'CONFLICT' else '') + ')')
            if merge_filter == 'CONFLICT':
                sql.append("NOT (manual=1 AND status IN ('REVIEWED','APPROVED') AND translated_hash=source_hash)")
        if source_type not in ('', 'SQLITE_STAGING', 'REAL_LOCAL_RESOURCE',
                               'PUBLIC_TEXTMAP', 'SYNTHETIC_FIXTURE'):
            raise ValueError('Неизвестный тип источника')
        for field, value in (('category', category), ('status', status), ('resource', resource), ('source_type', source_type)):
            if value:
                sql.append(f'{field}=?')
                params.append(value)
        if query:
            sql.append('(instr(casefold(id_value),?) OR instr(casefold(source),?) OR instr(casefold(target),?))')
            params.extend([query.casefold()] * 3)
        if term:
            # Literal occurrences, not global replacements; punctuation is significant.
            sql.append('instr(casefold(source),?)')
            params.append(term.casefold())
        from .qa import QualityAssurance
        qa = QualityAssurance(self.core)
        result = []
        size = 0
        suffix = ' ORDER BY seq'
        if not severity:
            suffix += ' LIMIT ?'
            params.append(limit)
        for row in self.core.db.execute('SELECT * FROM records WHERE ' + ' AND '.join(sql) + suffix, params):
            check_cancel(cancelled)
            if severity and not any(i.severity == severity for i in qa.issues(row)):
                continue
            length = row_bytes(row)
            if result and size + length > PAGE_BYTES:
                break
            size += length
            item = dict(row)
            item['merge_status'] = self.merge_status(row['seq'])
            item['version'] = self.version(row['seq'])
            result.append(item)
            if len(result) == limit:
                break
        return result

    def merge_status(self, seq: int) -> str:
        count, conflicts = self.core.db.execute("SELECT count(*),coalesce(sum(c.conflict),0) FROM merge_candidates c "
            "JOIN merge_imports i ON i.id=c.import_id WHERE c.seq=? AND i.state='APPLIED'", (seq,)).fetchone()
        row = self.core.record(seq)
        if conflicts and row['manual'] and row['status'] in ('REVIEWED', 'APPROVED') and row['translated_hash'] == row['source_hash']:
            return 'RESOLVED'
        return 'CONFLICT' if conflicts else ('CANDIDATES' if count else '')

    def issues(self, seq: int):
        from .qa import QualityAssurance
        return QualityAssurance(self.core).issues(self.core.record(seq))

    def save(self, seq: int, target: str, expected: str, *, status: str = 'REVIEWED', context: str | None = None) -> str:
        if status not in ('REVIEWED', 'APPROVED', 'NEEDS_REVIEW', 'INVALID'):
            raise ValueError('Недопустимый статус ручного перевода')
        with self.core.db:
            self.core.db.execute('BEGIN IMMEDIATE')
            row = self.checked(seq, expected)
            context = row['context'] if context is None else context
            if not isinstance(context, str) or not context.strip() or len(context) > 4096:
                raise ValueError('Нужен непустой контекст до 4096 символов')
            context.encode('utf-8')
            errors = blocking_errors(row['source'], target)
            if errors:
                raise ValueError('Сохранение заблокировано: ' + ', '.join(errors))
            self.core.db.execute("UPDATE records SET target=?,translated_hash=source_hash,manual=1,provider='manual',status=?,context=? WHERE seq=?",
                                 (target, status, context, seq))
            if context != row['context']:
                self.core.db.execute('UPDATE resources SET generation=generation+1 WHERE name=?', (row['resource'],))
            self.core.audit(seq, 'MANUAL', row)
        return self.version(seq)

    def history(self, seq: int, *, before: int = 2**63-1, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 200:
            raise ValueError('Лимит истории 1..200')
        result, size = [], 0
        for row in self.core.db.execute(
                'SELECT * FROM history WHERE seq=? AND revision<? ORDER BY revision DESC LIMIT ?', (seq, before, limit)):
            length = row_bytes(row)
            if result and size + length > PAGE_BYTES:
                break
            result.append(dict(row))
            size += length
        return result

    def restore(self, seq: int, revision: int, expected: str) -> None:
        with self.core.db:
            self.core.db.execute('BEGIN IMMEDIATE')
            row = self.checked(seq, expected)
            entry = self.core.db.execute('SELECT after_json FROM history WHERE seq=? AND revision=?', (seq, revision)).fetchone()
            if not entry:
                raise ValueError('Неизвестная ревизия')
            old = json.loads(entry[0])
            target = old['target']
            errors = blocking_errors(row['source'], target)
            if not isinstance(target, str) or 'UNICODE' in errors or 'TOO_LARGE' in errors:
                raise ValueError('Эта ревизия не содержит допустимого перевода')
            stale = (old['source_hash'] != row['source_hash'] or old['context'] != row['context'] or
                     old['translated_hash'] != row['source_hash'])
            status = 'INVALID' if errors else ('NEEDS_REVIEW' if stale else 'REVIEWED')
            self.core.db.execute("UPDATE records SET target=?,translated_hash=?,manual=1,provider='manual',status=? WHERE seq=?",
                                 (target, old['translated_hash'], status, seq))
            self.core.audit(seq, 'RESTORE', row)

    def statuses(self, versions: dict[int, str], status: str, *, cancelled: Event | None = None) -> None:
        if status not in STATUSES or status in ('MACHINE_TRANSLATED', 'UNTRANSLATED') or len(versions) > 1000:
            raise ValueError('Недопустимый статус или размер пакета (максимум 1000)')
        with self.core.db:
            self.core.db.execute('BEGIN IMMEDIATE')
            for seq, expected in versions.items():
                check_cancel(cancelled)
                row = self.checked(seq, expected)
                if status in ('REVIEWED', 'APPROVED') and (blocking_errors(row['source'], row['target']) or
                                                         row['source_hash'] != row['translated_hash']):
                    raise ValueError('Нужно исправить и сохранить актуальный перевод перед утверждением')
                self.core.db.execute('UPDATE records SET status=?,manual=1 WHERE seq=?', (status, seq))
                self.core.audit(seq, 'STATUS', row)

    def categorize(self, seq: int, category: str, expected: str) -> None:
        if category not in CATEGORIES:
            raise ValueError('Неизвестная категория')
        with self.core.db:
            self.core.db.execute('BEGIN IMMEDIATE')
            row = self.checked(seq, expected)
            self.core.db.execute('UPDATE records SET category=? WHERE seq=?', (category, seq))
            self.core.audit(seq, 'CATEGORY', row)

    def link_context(self, seq: int, related: int, expected: str) -> None:
        with self.core.db:
            self.core.db.execute('BEGIN IMMEDIATE')
            row = self.checked(seq, expected)
            if not self.core.record(related)['active'] or related == seq:
                raise ValueError('Нужна другая активная запись контекста')
            self.core.db.execute('INSERT OR IGNORE INTO context_links VALUES (?,?)', (seq, related))
            self.core.audit(seq, 'CONTEXT_LINK', row)

    def variants(self, term: str, *, after: str = '', limit: int = 100) -> list[dict]:
        if not term or not 1 <= limit <= 200:
            raise ValueError('Нужен термин и лимит 1..200')
        result, size = [], 0
        for row in self.core.db.execute(
            'SELECT target,count(*) AS uses FROM records WHERE active=1 AND instr(casefold(source),?) '
                'AND target>? GROUP BY target ORDER BY target LIMIT ?', (term.casefold(), after, limit)):
            length = row_bytes(row)
            if result and size + length > PAGE_BYTES:
                break
            result.append(dict(row))
            size += length
        return result
