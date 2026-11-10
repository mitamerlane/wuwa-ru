"""Bounded, offline import of public Textmaps. No game SQLite mapping is claimed."""
from __future__ import annotations

import json
import re
from pathlib import Path
from threading import Event

from .manifest import check_cancel, fingerprint
from .paths import relative_parts
from .translation_db import TranslationDB, offline_path

MAX_FILE = 256 * 1024 * 1024
MAX_VALUE = 4 * 1024 * 1024
MAX_ROWS = 2_000_000


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Повторяющийся ключ JSON')
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError('Недопустимая константа JSON: ' + value)


def iter_textmap(path: Path, *, cancelled: Event | None = None, chunk_size=65536,
                 textmap: bool = True):
    """Stream an Arikatsu array or Dimbreath ID→text object, including compact JSON.

    Memory is bounded by one JSON value plus a chunk; no whole-file json.load.
    Duplicate top-level IDs are checked by the store's disk-backed unique index.
    """
    path = offline_path(path)
    if not 0 < path.stat().st_size <= MAX_FILE or not 1 <= chunk_size <= 65536:
        raise ValueError('Недопустимый размер JSON или блока')
    decoder = json.JSONDecoder(object_pairs_hook=unique_object, parse_constant=invalid_constant)
    with path.open(encoding='utf-8-sig') as stream:
        buffer, end, total = '', False, 0

        def fill():
            nonlocal buffer, end, total
            check_cancel(cancelled)
            part = stream.read(chunk_size)
            total += len(part.encode('utf-8'))
            if total > MAX_FILE:
                raise ValueError('Превышен лимит чтения JSON')
            end = not part
            buffer += part
            if len(buffer) > MAX_VALUE + chunk_size:
                raise ValueError('Слишком большая запись JSON')

        def peek():
            nonlocal buffer
            while True:
                buffer = buffer.lstrip(' \r\n\t')
                if buffer or end:
                    return buffer[:1]
                fill()

        def consume(char):
            nonlocal buffer
            if peek() != char:
                raise ValueError('Ожидался разделитель JSON: ' + char)
            buffer = buffer[1:]

        def value():
            nonlocal buffer
            peek()
            while True:
                try:
                    result, offset = decoder.raw_decode(buffer)
                    # A number can span chunks; demand a following delimiter/EOF.
                    if offset == len(buffer) and not end:
                        fill()
                        continue
                    if offset > MAX_VALUE:
                        raise ValueError('Слишком большая запись JSON')
                    buffer = buffer[offset:]
                    return result
                except json.JSONDecodeError as exc:
                    if end:
                        raise ValueError('Некорректный JSON') from exc
                    fill()
                except RecursionError as exc:
                    raise ValueError('Слишком глубокий JSON') from exc

        opening = peek()
        if opening not in ('[', '{'):
            raise ValueError('Нужен массив Id/Content или объект ID→text')
        consume(opening)
        closing = ']' if opening == '[' else '}'
        count = 0
        if peek() != closing:
            while True:
                check_cancel(cancelled)
                if opening == '[':
                    row = value()
                else:
                    identifier = value()
                    if not isinstance(identifier, str):
                        raise ValueError('Ключ JSON должен быть строкой')
                    consume(':')
                    row = dict(Id=identifier, Content=value())
                if not isinstance(row, dict):
                    raise ValueError('Нужен объект записи JSON')
                if textmap and not {'Id', 'Content'} <= row.keys():
                    raise ValueError('Нет Id/Content')
                identifier, text = row.get('Id'), row.get('Content')
                if textmap and (type(identifier) not in (int, str) or
                    (type(identifier) is int and not -(2**63) <= identifier < 2**63) or
                    (isinstance(identifier, str) and not 0 < len(identifier) <= 4096) or
                    (text is not None and not isinstance(text, str))):
                    raise ValueError('Недопустимые типы Id/Content')
                # Reject unpaired surrogates and overlong fields, preserving original bytes.
                if len(json.dumps(row, ensure_ascii=False).encode('utf-8')) > MAX_VALUE:
                    raise ValueError('Слишком большая запись JSON в UTF-8')
                count += 1
                if count > MAX_ROWS:
                    raise ValueError('Превышен лимит строк JSON')
                yield row
                if peek() == closing:
                    break
                consume(',')
        consume(closing)
        if peek():
            raise ValueError('Лишние данные после JSON')


def category_for(source_path: str, identifier) -> str:
    """Documented heuristic only; original path/ID remain available for review."""
    key = (source_path + '/' + str(identifier)).casefold()
    for category, tokens in (
        ('dialogue', ('flowtext', 'subtitle', 'talk_', 'dialog_', 'dialogue', 'plot_')),
        ('quest', ('quest', 'adventuretask')),
        ('item', ('item', 'weapon', 'phantom', 'cook', 'recipe')),
        ('character', ('speaker', 'roleinfo', 'favorstory', 'favorword')),
        ('location', ('area/', 'country/', 'location')),
        ('ability', ('skill', 'buff', 'attribute')),
        ('ui', ('ui_', '/ui', 'hotkey', 'button', 'errorcode', 'menutext', 'confirmbox')),
    ):
        if any(token in key for token in tokens):
            return category
    return 'other'


class PublicTextSourceAdapter:
    def __init__(self, core: TranslationDB):
        self.core = core

    def import_file(self, source: Path, *, resource: str, repository: str, revision: str,
                    data_version: str, data_date: str | None, source_path: str,
                    expected_sha256: str | None = None, cancelled: Event | None = None):
        source = offline_path(source)
        relative_parts(resource)
        relative_parts(source_path)
        if (not resource.endswith('.json') or not source_path.endswith('.json') or
                not source_path.startswith(('Textmaps/en/', 'TextMap/en/'))):
            raise ValueError('Public ресурс должен иметь имя .json')
        if source.resolve().is_relative_to(self.core.workspace.resolve()):
            raise ValueError('Исходный JSON должен быть вне рабочей папки')
        if (not re.fullmatch(r'[0-9a-f]{40}', revision) or
            not re.fullmatch(r'https://github.com/[\w.-]+/[\w.-]+', repository) or len(repository) > 256 or
            not isinstance(data_version, str) or not 0 < len(data_version) <= 128 or
            (data_date is not None and (not isinstance(data_date, str) or len(data_date) > 64))):
            raise ValueError('Нужны корректные provenance/version/commit')
        if not 0 < source.stat().st_size <= MAX_FILE:
            raise ValueError('Недопустимый размер JSON')
        before = fingerprint(source, True, cancelled)
        if expected_sha256 is not None and before['sha256'] != expected_sha256:
            raise ValueError('SHA-256 JSON не совпал с lock-файлом')
        inspection = dict(schema_version=1, source_type='PUBLIC_TEXTMAP',
            repository=repository, revision=revision, data_version=data_version, data_date=data_date,
            source_path=source_path, sha256=before['sha256'], size=before['size'],
            tables=[dict(name='Textmap')], local_resource_mapping=None,
            category_method='path_and_id_heuristic_v1', installation_allowed=False)

        def rows():
            for row in iter_textmap(source, cancelled=cancelled):
                metadata = json.dumps({k: v for k, v in row.items() if k not in ('Id', 'Content')},
                                      ensure_ascii=False, sort_keys=True)
                yield 'Textmap', row['Id'], row['Content'], category_for(source_path, row['Id']), metadata

        return self.core.import_snapshot(resource, source, inspection, rows(), cancelled=cancelled)
