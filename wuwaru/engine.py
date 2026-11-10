"""Bounded batches, durable checkpoints and fail-closed network spending."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from threading import Event

from .flow_context import WAVES, text_kind
from .glossary import Glossary
from .manifest import check_cancel
from .providers import (OpenAICompatibleProvider, ProviderError, SYSTEM, TranslationItem,
                        TranslationProvider, payload)
from .translation_db import TranslationDB, digest
from .validation import needs_translation, validate

SCENE_BATCH_CHARS = 6000  # Measured on the real FlowLists: only 6 of 20 672 scenes are longer, and they still go whole.
ESTIMATE_GAP = 1.3  # Stop a wave when real tokens per character run this far above the estimate.
SCENE_TAIL = 1 << 40  # Cursor values from here on are records outside any scene, by seq.
PENDING = ("active=1 AND manual=0 AND (target IS NULL OR translated_hash<>source_hash OR status IN ('INVALID','NEEDS_REVIEW')) "
           "AND NOT (coalesce(provider,'') LIKE 'reuse:%' AND target IS NOT NULL "
           "AND translated_hash=source_hash AND status<>'INVALID')")
FILLER_REPEATS = 8  # "Test Test …" made the model repeat itself past the timeout; a real "Ha ha ha ha!" is shorter.


def is_filler(source: str) -> bool:
    """A developer placeholder: one word repeated many times. It stays English and INVALID, for a human to look at."""
    words = source.split()
    return len(words) >= FILLER_REPEATS and len({word.casefold() for word in words}) == 1


@dataclass(frozen=True)
class Budget:
    allowed: bool = False
    max_requests: int = 0
    max_cost: float = 0
    input_price: float | None = None  # user supplied currency units per million tokens
    output_price: float | None = None

    bounded_pilot: bool = False
    token_cap: int | None = None  # Input plus output tokens the provider reports for one run ID.
    tokens_per_char: float | None = None
    tokens_per_item: float = 0  # Short strings cost mostly their JSON wrapping, not their characters.
    tokens_per_request: float = 0
    min_chars_for_gap: int = 20_000  # One short request says nothing about the estimate.

    def __post_init__(self):
        if self.token_cap is not None and (type(self.token_cap) is not int or self.token_cap < 1):
            raise ValueError('Некорректный предел токенов')
        if type(self.max_requests) is not int or self.max_requests < 0 or any(
                not math.isfinite(value) or value < 0 for value in
                (self.max_cost, self.input_price, self.output_price) if value is not None):
            raise ValueError('Некорректный бюджет')


class TranslationEngine:
    def __init__(self, core: TranslationDB, provider: TranslationProvider, *, attempts: int = 3,
                 interval: float = 1, sleep=time.sleep, clock=time.monotonic, selection=None, scenes: bool = False,
                 wave: str | None = None, changed_since: int | None = None):
        if not 1 <= attempts <= 10 or not math.isfinite(interval) or interval < 0:
            raise ValueError('Некорректные retry/rate limit')
        self.core, self.provider = core, provider
        self.scenes = scenes
        if wave is not None and (not scenes or wave not in WAVES):
            raise ValueError('Волна требует режима сцен и известного имени')
        self.wave = wave
        # Only records an import added or changed after this history revision: one game update, not old leftovers.
        if changed_since is not None and (type(changed_since) is not int or changed_since < 0):
            raise ValueError('Некорректная граница истории')
        self.changed_since = changed_since
        self.glossary = Glossary(core)
        self.attempts, self.interval = attempts, interval
        self.sleep, self.clock = sleep, clock
        self.last_request: float | None = None
        self.selection = tuple(sorted(set(selection))) if selection is not None else None
        if self.selection is not None and (not 1 <= len(self.selection) <= 200 or any(
                type(seq) is not int or seq < 1 for seq in self.selection)):
            raise ValueError('Пилот требует 1..200 точных seq')

    def signature(self) -> str:
        resources = [tuple(r) for r in self.core.db.execute(
            'SELECT name,sha256,generation FROM resources ORDER BY name')]
        values = [resources, self.glossary.version, self.provider.identity]
        if self.selection is not None:
            values.append(self.selection)
        if self.wave is not None:
            values.append(self.wave)
        if self.changed_since is not None:
            values.append(['changed_since', self.changed_since])
        if self.scenes:
            values.append([tuple(row) for row in self.core.db.execute('SELECT path,sha1 FROM scene_sources ORDER BY path')])
            values.append([tuple(row) for row in self.core.db.execute('SELECT name,gender,address FROM cast_members ORDER BY name')])
        return digest(json.dumps(values))

    def _changed(self, alias: str) -> tuple[str, tuple]:
        if self.changed_since is None:
            return '', ()
        return (f" AND {alias}seq IN (SELECT seq FROM history WHERE revision>? AND event IN ('IMPORT','SOURCE_CHANGED'))",
                (self.changed_since,))

    def _scene_rows(self, cursor: int, limit: int) -> list[dict]:
        """Scenes in play order through the ordinal index, then everything else by seq, both without a full sort."""
        self.core.db.create_function('worth_translating', 2, needs_translation, deterministic=True)
        self.core.db.create_function('text_kind', 1, text_kind, deterministic=True)
        kinds = WAVES[self.wave] if self.wave else None
        scope = '' if self.selection is None else ' AND r.seq IN (' + ','.join('?' for _ in self.selection) + ')'
        changed, since = self._changed('r.')
        scope += changed
        fields = ('r.seq,r.source,r.source_hash,r.context,r.id_value,r.category,s.scene,s.kind,s.speaker,c.gender,c.address')
        wanted = f'{PENDING} AND worth_translating(r.id_value,r.source){scope}'
        rows = []
        if kinds is not None and 'scene' not in kinds:
            cursor = max(cursor, SCENE_TAIL)
        if cursor < SCENE_TAIL:
            rows = self.core.db.execute(
                f'SELECT {fields},s.ordinal AS position FROM scene_lines s CROSS JOIN records r ON r.seq=s.seq '
                f'LEFT JOIN cast_members c ON c.name=s.speaker WHERE {wanted} AND s.ordinal>? ORDER BY s.ordinal LIMIT ?',
                (*(self.selection or ()), *since, cursor, limit)).fetchall()
            cursor = SCENE_TAIL
        if not rows:
            tail_kinds = () if kinds is None else tuple(kind for kind in kinds if kind != 'scene')
            tail = '' if kinds is None else 'AND text_kind(r.id_value) IN (' + ','.join('?' for _ in tail_kinds) + ')'
            rows = self.core.db.execute(
                f'SELECT {fields},r.seq+{SCENE_TAIL} AS position FROM records r LEFT JOIN scene_lines s ON s.seq=r.seq '
                f'LEFT JOIN cast_members c ON c.name=s.speaker WHERE {wanted} AND s.seq IS NULL AND r.seq>? {tail} ORDER BY r.seq LIMIT ?',
                (*(self.selection or ()), *since, cursor - SCENE_TAIL, *tail_kinds, limit)).fetchall()
        return [dict(row) for row in rows]

    def _scene_batches(self, batch_size: int, cursor: int):
        while True:
            rows = self._scene_rows(cursor, batch_size)
            if not rows:
                return
            used, boundary, end = 0, 0, len(rows)
            for index, row in enumerate(rows):
                length = len(row['source'].encode('utf-8')) + 256
                if length > 128 * 1024:
                    raise ValueError('Строка превышает лимит переводчика (128 KiB)')
                if index and (row['scene'] is None or row['scene'] != rows[index - 1]['scene']):
                    boundary = index
                    if used >= SCENE_BATCH_CHARS:
                        end = index
                        break
                used += len(row['source'])
                if used > SCENE_BATCH_CHARS and boundary:
                    end = boundary
                    break
            else:
                # The window may have stopped in the middle of its last scene.
                if len(rows) == batch_size and boundary and rows[-1]['scene'] is not None:
                    end = boundary
            batch = rows[:end]
            cursor = batch[-1]['position']
            yield batch

    @staticmethod
    def _scene_fields(row: dict) -> dict:
        if row['scene'] is None:
            kind = text_kind(row['id_value'])
            if kind == 'line':
                return dict(name='?', gender='unknown', kind=kind)
            parts = [part for part in row['id_value'].split('_') if any(c.isalpha() for c in part)]
            return dict(kind=kind, field=parts[-1]) if len(parts) > 1 else dict(kind=kind)
        person = dict(scene=row['scene'], name=row['speaker'] or '?', gender=row['gender'] or 'unknown')
        return dict(person, **({'address': row['address']} if row['address'] else {}), kind=row['kind'])

    def _cursor_of(self, row) -> int:
        return row['position'] if self.scenes else row['seq']

    def batches(self, batch_size: int, cursor: int = 0):
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError('Размер batch должен быть 1..1000')
        if self.scenes:
            yield from self._scene_batches(batch_size, cursor)
            return
        while True:
            # Bound provider payload by bytes too, not only row count.
            size = 0
            batch = []
            scope = '' if self.selection is None else ' AND seq IN (' + ','.join('?' for _ in self.selection) + ')'
            changed, since = self._changed('')
            rows = self.core.db.execute(f'SELECT seq,source,source_hash,context FROM records WHERE {PENDING}'
                                        + scope + changed + ' AND seq>? ORDER BY seq LIMIT ?',
                                        (*(self.selection or ()), *since, cursor, batch_size))
            for row in rows:
                length = len(row['source'].encode('utf-8')) + len(row['context'].encode('utf-8')) + 256
                if length > 128 * 1024:
                    raise ValueError('Строка превышает лимит переводчика (128 KiB)')
                if batch and size + length > 128 * 1024:
                    break
                batch.append(row)
                size += length
            rows.close()
            if not batch:
                return
            cursor = batch[-1]['seq']
            yield batch

    def resolve(self, rows):
        resolved, pending, keys = {}, {}, {}
        for row in rows:
            item_context, key_context = row['context'], row['context']
            if self.scenes:
                # The same text of the same kind of speaker is one paid answer, whatever scene or field it stands in.
                fields = self._scene_fields(row)
                item_context = json.dumps(fields, ensure_ascii=False)
                key_context = json.dumps({name: value for name, value in fields.items() if name not in ('scene', 'field')},
                                         ensure_ascii=False, sort_keys=True)
            key = digest(json.dumps([row['source_hash'], key_context, self.glossary.version,
                                     self.provider.identity, 'en', 'ru', 'validator-v1']))
            keys[row['seq']] = key
            exact = self.glossary.exact(row['source'], row['context'])
            cached = self.core.db.execute('SELECT target FROM cache WHERE key=?', (key,)).fetchone()
            if is_filler(row['source']):
                resolved[key] = (row['source'], 'placeholder')
            elif exact is not None:
                resolved[key] = (exact, 'glossary')
            elif cached is not None and not validate(row['source'], cached[0]):
                resolved[key] = (cached[0], 'cache')
            else:
                pending.setdefault(key, TranslationItem(key, row['source'], item_context,
                                                        self.glossary.hints(row['source'], row['context'])))
        return resolved, list(pending.values()), keys

    def estimate(self, items: list[TranslationItem], input_price: float | None,
                 output_price: float | None) -> tuple[int, float | None]:
        if not self.provider.requires_permission:
            input_price, output_price = input_price or 0, output_price or 0
        # UTF-8 byte count is a deliberately conservative planning proxy, not a tokenizer.
        input_tokens = len(payload(items).encode('utf-8')) + len(SYSTEM.encode('utf-8')) + 128
        if isinstance(self.provider, OpenAICompatibleProvider):
            input_tokens = len(json.dumps(self.provider.request_body(items), ensure_ascii=False).encode('utf-8')) + 128
        if getattr(self.provider, 'profile', None) == 'RelayModels' or input_price is None or output_price is None:
            return input_tokens, None
        cost = (input_tokens * input_price + self.provider.max_output_tokens * output_price) / 1_000_000
        return input_tokens, cost

    def preview(self, *, batch_size: int = 100, input_price: float | None = None, output_price: float | None = None) -> dict:
        if not self.provider.requires_permission:
            input_price, output_price = input_price or 0, output_price or 0
        Budget(input_price=input_price, output_price=output_price)
        unknown = getattr(self.provider, 'profile', None) == 'RelayModels' or None in (input_price, output_price)
        requests, records, input_tokens, cost = 0, 0, 0, None if unknown else 0.0
        for rows in self.batches(batch_size):
            _, pending, _ = self.resolve(rows)
            records += len(rows)
            if pending:
                requests += 1
                tokens, value = self.estimate(pending, input_price, output_price)
                input_tokens += tokens
                cost = None if value is None else cost + value
        return dict(schema_version=1, provider=self.provider.identity, pending_records=records,
                    requests=requests, max_attempts=requests * self.attempts,
                    estimated_input_tokens=input_tokens, estimated_cost=cost,
                    estimated_cost_with_retries=None if cost is None else cost * self.attempts,
                    pricing='UNKNOWN' if cost is None else 'USER_SUPPLIED_PER_MILLION_TOKENS',
                    estimate='UTF8_BYTES_UPPER_PROXY; cache reuse across batches may reduce requests',
                    network_sent=False)

    def _wait(self, delay: float, cancelled: Event | None):
        # At most 0.25 seconds between cancellation checks, including backoff.
        while delay > 0:
            check_cancel(cancelled)
            step = min(0.25, delay)
            self.sleep(step)
            delay -= step
        check_cancel(cancelled)

    def request(self, run_id: str, items: list[TranslationItem], budget: Budget,
                cancelled: Event | None) -> dict[str, str]:
        _, cost = self.estimate(items, budget.input_price, budget.output_price)
        for attempt in range(self.attempts):
            check_cancel(cancelled)
            if self.last_request is not None:
                self._wait(max(0, self.interval - (self.clock() - self.last_request)), cancelled)
            # Durable reservation before the call. Ambiguous crashes count as spent.
            with self.core.db:
                self.core.db.execute('BEGIN IMMEDIATE')
                used = self.core.db.execute('SELECT count(*),coalesce(sum(estimated_cost),0) '
                                            'FROM usage WHERE run_id=?', (run_id,)).fetchone()
                if self.provider.requires_permission and (
                        used[0] >= budget.max_requests or (cost is not None and used[1] + cost > budget.max_cost)):
                    raise ValueError('Лимит запросов/бюджета исчерпан. Уменьши строки или выходные токены; для продолжения увеличь лимит и подтверди новый preview.')
                usage = self.core.db.execute('INSERT INTO usage(run_id,provider,estimated_cost) '
                                             'VALUES (?,?,?)', (run_id, self.provider.identity, cost)).lastrowid
            self.last_request = self.clock()
            try:
                if isinstance(self.provider, OpenAICompatibleProvider):
                    result = self.provider.translate(items, allow_network=budget.allowed)
                else:
                    result = self.provider.translate(items)
                if (not isinstance(result, dict) or set(result) != {i.key for i in items} or
                        any(not isinstance(v, str) for v in result.values())):
                    raise ProviderError('Провайдер изменил ID/структуру batch')
                with self.core.db:
                    self.core.db.execute("UPDATE usage SET state='RECEIVED',actual_usage_json=? WHERE attempt=?",
                        (json.dumps(getattr(self.provider, 'last_usage', None)), usage))
                return result
            except ProviderError as exc:
                with self.core.db:
                    self.core.db.execute("UPDATE usage SET state='FAILED',actual_usage_json=? WHERE attempt=?",
                        (json.dumps(getattr(self.provider, 'last_usage', None)), usage))
                if not exc.retryable or attempt + 1 == self.attempts:
                    raise
                self._wait(max(min(60, 2 ** attempt), min(60, exc.retry_after)), cancelled)
        raise RuntimeError('Недостижимое состояние retry')

    def run(self, run_id: str, *, batch_size: int = 100, budget: Budget | None = None,
            cancelled: Event | None = None, after_batch=None) -> dict:
        if not run_id or len(run_id) > 200:
            raise ValueError('Нужен короткий ID задания')
        budget = budget or Budget()
        if self.provider.requires_permission:
            if not budget.allowed:
                raise PermissionError('Сначала preview и явное разрешение платных/сетевых вызовов')
            # Unknown price: only a pilot of exact rows, or one wave under a token cap the provider reports against.
            capped_wave = (self.wave is not None or self.changed_since is not None) and budget.token_cap is not None and budget.max_requests >= 1
            if (getattr(self.provider, 'profile', None) == 'RelayModels' or
                    budget.input_price is None or budget.output_price is None) and not capped_wave and (
                    not budget.bounded_pilot or self.selection is None or budget.max_requests < 1):
                raise ValueError('Стоимость неизвестна: требуется подтверждённый ограниченный пилот')
            preview = self.preview(batch_size=batch_size, input_price=budget.input_price,
                                   output_price=budget.output_price)
            used = self.core.db.execute('SELECT count(*),coalesce(sum(estimated_cost),0) FROM usage '
                                        'WHERE run_id=?', (run_id,)).fetchone()
            if (not budget.bounded_pilot and used[0] + preview['requests'] > budget.max_requests or
                    preview['estimated_cost'] is not None and
                    used[1] + preview['estimated_cost'] > budget.max_cost):
                raise ValueError('Бюджет меньше предварительной оценки. Уменьши число строк или выходных токенов; проверь тарифы и обнови preview.')
        signature = self.signature()
        existing = self.core.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if existing is not None and existing['signature'] != signature:
            raise ValueError('Источники/словарь/провайдер изменились: нужен новый ID задания')
        with self.core.db:
            self.core.db.execute('INSERT OR IGNORE INTO runs(id,signature) VALUES (?,?)', (run_id, signature))
            self.core.db.execute("UPDATE runs SET state='RUNNING' WHERE id=?", (run_id,))
        cursor = existing['cursor'] if existing else 0
        session_tokens, session_chars, session_items, session_requests = 0, 0, 0, 0
        try:
            for rows in self.batches(batch_size, cursor):
                check_cancel(cancelled)
                resolved, pending, keys = self.resolve(rows)
                if pending and budget.token_cap is not None and self._tokens_spent(run_id) >= budget.token_cap:
                    return self._pause(run_id, 'TOKEN_CAP')
                if pending and budget.bounded_pilot and self.core.db.execute(
                        'SELECT count(*) FROM usage WHERE run_id=?', (run_id,)).fetchone()[0] >= budget.max_requests:
                    with self.core.db:
                        self.core.db.execute("UPDATE runs SET state='PAUSED' WHERE id=?", (run_id,))
                    return dict(self.core.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())
                if pending:
                    response = self.request(run_id, pending, budget, cancelled)
                    resolved.update({key: (target, self.provider.identity) for key, target in response.items()})
                    session_chars += sum(len(item.source) for item in pending)
                    session_items += len(pending)
                    session_requests += 1
                    used_now = self._reported_tokens(getattr(self.provider, 'last_usage', None))
                    session_tokens += used_now or 0
                    stop = None
                    if budget.token_cap is not None and used_now is None:
                        stop = 'USAGE_MISSING'
                    elif (budget.tokens_per_char and session_chars >= budget.min_chars_for_gap and
                          session_tokens > ESTIMATE_GAP * (session_chars * budget.tokens_per_char
                                                           + session_items * budget.tokens_per_item
                                                           + session_requests * budget.tokens_per_request)):
                        stop = 'ESTIMATE_GAP'
                check_cancel(cancelled)
                with self.core.db:
                    self.core.db.execute('BEGIN IMMEDIATE')
                    if self.signature() != signature:
                        raise ValueError('Snapshot источников/словаря изменился во время перевода')
                    current_cursor = self.core.db.execute('SELECT cursor FROM runs WHERE id=?', (run_id,)).fetchone()[0]
                    if current_cursor != cursor:
                        raise ValueError('Задание уже изменено другим worker')
                    for before in rows:
                        current = self.core.record(before['seq'])
                        if current['manual'] or current['source_hash'] != before['source_hash'] or not current['active']:
                            continue
                        target, provider = resolved[keys[before['seq']]]
                        errors = validate(before['source'], target)
                        # JSON can contain escaped lone surrogates, which SQLite cannot store.
                        try:
                            target.encode('utf-8')
                        except UnicodeError:
                            target = None  # INVALID remains explicit; never cache/export this result.
                        self.core.db.execute('UPDATE records SET target=?,translated_hash=source_hash,status=?, '
                                             'provider=? WHERE seq=?', (target, 'INVALID' if errors else
                                             'MACHINE_TRANSLATED', provider, before['seq']))
                        self.core.audit(before['seq'], 'AUTOMATIC', current)
                        if not errors:
                            self.core.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?)',
                                                 (keys[before['seq']], target))
                    self.core.db.execute('UPDATE runs SET cursor=?,processed=processed+? WHERE id=?',
                                         (self._cursor_of(rows[-1]), len(rows), run_id))
                cursor = self._cursor_of(rows[-1])
                if after_batch is not None:
                    after_batch(cursor)
                if pending and stop is not None:
                    return self._pause(run_id, stop)
            with self.core.db:
                self.core.db.execute("UPDATE runs SET state='DONE' WHERE id=?", (run_id,))
        except BaseException:
            with self.core.db:
                self.core.db.execute("UPDATE runs SET state='INTERRUPTED' WHERE id=?", (run_id,))
            raise
        return dict(self.core.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone(),
                    tokens_spent=self._tokens_spent(run_id))

    @staticmethod
    def _reported_tokens(usage) -> int | None:
        if not isinstance(usage, dict) or usage.get('input_tokens') is None or usage.get('output_tokens') is None:
            return None
        return usage['input_tokens'] + usage['output_tokens']

    def _tokens_spent(self, run_id: str) -> int:
        rows = self.core.db.execute('SELECT actual_usage_json FROM usage WHERE run_id=? AND actual_usage_json IS NOT NULL', (run_id,))
        return sum(self._reported_tokens(json.loads(row[0])) or 0 for row in rows)

    def _pause(self, run_id: str, reason: str) -> dict:
        with self.core.db:
            self.core.db.execute("UPDATE runs SET state='PAUSED' WHERE id=?", (run_id,))
        return dict(self.core.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone(), stop_reason=reason,
                    tokens_spent=self._tokens_spent(run_id))
