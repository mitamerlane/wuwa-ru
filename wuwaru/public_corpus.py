"""Pinned public corpus import, statistics and deterministic offline pilot preparation."""
from __future__ import annotations

import heapq
import json
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from .manifest import check_cancel, fingerprint
from .paths import inside, relative_parts
from .public_text import MAX_FILE, PublicTextSourceAdapter
from .qa import output_path
from .translation_db import offline_path
from .validation import TOKEN

PRICE = dict(model='gpt-6-luna', currency='USD', verified_date='2026-10-24',
             input_per_million=0.10, output_per_million=0.50, batch_multiplier=0.5,
             url='https://developers.openai.com/api/docs/models/gpt-6-luna')


def source_catalog(lock: Path) -> dict:
    lock = offline_path(lock)
    if not 0 < lock.stat().st_size <= 2 * 1024 * 1024:
        raise ValueError('Недопустимый размер lock')
    try:
        manifest = json.loads(lock.read_text(encoding='utf-8'))
        if (manifest.get('schema_version') != 1 or not isinstance(manifest['sources'], dict) or
                not 1 <= len(manifest['sources']) <= 32):
            raise ValueError('Неподдерживаемая схема lock')
        return manifest['sources']
    except (KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise ValueError('Некорректная структура public lock') from exc


def read_lock(lock: Path, source_name: str):
    try:
        source = source_catalog(lock)[source_name]
        if (not re.fullmatch(r'https://github.com/[\w.-]+/[\w.-]+', source['repository']) or
            not re.fullmatch('[a-f0-9]{40}', source['revision']) or
            not 1 <= len(source['files']) <= 1024):
            raise ValueError('Недопустимое описание источника')
        if (not isinstance(source['data_version'], str) or not 0 < len(source['data_version']) <= 128 or
            (source['data_date'] is not None and not isinstance(source['data_date'], str))):
            raise ValueError('Недопустимая версия/дата источника')
        paths = set()
        for file in source['files']:
            path = '/'.join(relative_parts(file['path']))
            if (path != file['path'] or path.casefold() in paths or not path.endswith('.json') or
                not path.startswith(('Textmaps/en/', 'TextMap/en/')) or
                type(file['size']) is not int or not 0 < file['size'] <= MAX_FILE or
                not re.fullmatch('[a-f0-9]{64}', file['sha256'])):
                raise ValueError('Недопустимый файл в lock')
            paths.add(path.casefold())
        return source
    except (KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise ValueError('Некорректная структура public lock') from exc


def import_corpus(core, lock: Path, source_root: Path, source_name: str, *, cancelled=None):
    source = read_lock(lock, source_name)
    source_root = offline_path(source_root)
    # Preflight the entire file inventory before any import; each file remains a
    # separately atomic snapshot. Interruptions resume through unchanged hashes.
    for file in source['files']:
        check_cancel(cancelled)
        path = inside(source_root, file['path'])
        if path.stat().st_size != file['size'] or fingerprint(path, True, cancelled)['sha256'] != file['sha256']:
            raise ValueError('Корпус не совпадает с lock: ' + file['path'])
    adapter, counts = PublicTextSourceAdapter(core), dict(new=0, changed=0, unchanged=0, removed=0)
    namespace = source['repository'].removeprefix('https://github.com/')
    expected_resources = {'public/' + namespace + '/' + file['path'] for file in source['files']}
    for (name,) in core.db.execute('SELECT name FROM resources'):
        if name.startswith('public/' + namespace + '/') and name not in expected_resources:
            raise ValueError('Новый inventory удаляет ресурс; используй отдельный workspace для нового снимка')
    for file in source['files']:
        result = adapter.import_file(inside(source_root, file['path']),
            resource='public/' + namespace + '/' + file['path'],
            repository=source['repository'], revision=source['revision'],
            data_version=source['data_version'], data_date=source['data_date'], source_path=file['path'],
            expected_sha256=file['sha256'], cancelled=cancelled)
        for key in counts:
            counts[key] += result[key]
    return dict(schema_version=1, source_type='PUBLIC_TEXTMAP', source=source_name,
                **counts, installation_allowed=False)


PUBLIC = "active=1 AND source_type='PUBLIC_TEXTMAP'"


def corpus_statistics(core):
    db = core.db
    counts = db.execute(f"SELECT count(*),sum(length(trim(source))>0),count(DISTINCT source),"
                        f"sum(length(source)) FROM records WHERE {PUBLIC}").fetchone()
    categories = {row[0]: row[1] for row in db.execute(
        f'SELECT category,count(*) FROM records WHERE {PUBLIC} GROUP BY category')}
    unique_nonempty = db.execute(f"SELECT count(DISTINCT source) FROM records WHERE {PUBLIC} AND trim(source)<>''").fetchone()[0]
    templates = cjk = service_only = latin = latin_only = descriptions = replacement = 0
    template_kinds = Counter()
    for text, identifier in db.execute(f'SELECT source,id_value FROM records WHERE {PUBLIC}'):
        templates += bool(TOKEN.search(text))
        service_only += bool(text.strip()) and not TOKEN.sub('', text).strip()
        cjk += bool(re.search('[\u3400-\u9fff]', text))
        has_latin = bool(re.search('[A-Za-z]', text))
        latin += has_latin
        latin_only += has_latin and not re.search('[\u3400-\u9fff]', text)
        descriptions += bool(re.search('desc|description|_des_', identifier, re.I))
        replacement += '\ufffd' in text
        for kind, pattern in (('angle_markup', r'<[^<>\r\n]*>'), ('braces', r'\{[^{}]*\}'),
                              ('printf_like', r'%[\w.]+'), ('newline', '\n'),
                              ('escaped_newline', r'\\n')):
            template_kinds[kind] += bool(re.search(pattern, text))
    cross_file = db.execute(f'SELECT count(*),sum(variants>1) FROM ('
        f'SELECT count(DISTINCT source_hash) variants FROM records WHERE {PUBLIC} '
        'GROUP BY id_type,id_value HAVING count(DISTINCT resource)>1)').fetchone()
    resources = [dict(name=row['name'], **json.loads(row['inspection']))
                 for row in db.execute('SELECT * FROM resources')
                 if json.loads(row['inspection']).get('source_type') == 'PUBLIC_TEXTMAP']
    return dict(schema_version=1, source_type='PUBLIC_TEXTMAP', records=counts[0],
        nonempty=counts[1] or 0, empty=counts[0] - (counts[1] or 0), unique_texts=counts[2],
        unique_nonempty=unique_nonempty, repeated_text_records=counts[0]-counts[2],
        characters=counts[3] or 0, categories=categories, category_method='path_and_id_heuristic_v1',
        with_templates=templates, service_only=service_only, with_cjk=cjk,
        with_latin=latin, with_latin_without_cjk=latin_only, description_id_candidates=descriptions,
        with_replacement_character=replacement, template_kinds=dict(template_kinds),
        cross_file_id_groups=cross_file[0], cross_file_conflicting_id_groups=cross_file[1] or 0,
        local_mapped=0, local_unmapped=counts[0], complete_game_inventory=False,
        installation_allowed=False, sources=resources)


def estimate_cost(core, *, batch_size=100, glossary_tokens=1500, context_tokens=0,
                  output_ratio=1.8, reserve=0.30):
    """Planning proxies, not tokenization or a spending cap. No network operation.

    Count all nonempty records, including repeated text in different contexts.
    English chars/4, Russian output ×1.8 are explicit adjustable assumptions.
    Upper scenario uses UTF-8 bytes for inputs and a separate ×2 output allowance.
    """
    if (type(batch_size) is not int or not 1 <= batch_size <= 1000 or
        type(glossary_tokens) is not int or not 0 <= glossary_tokens <= 100000 or
        type(context_tokens) is not int or not 0 <= context_tokens <= 10000 or
        not math.isfinite(output_ratio) or not 1 <= output_ratio <= 10 or
        not math.isfinite(reserve) or not 0 <= reserve <= 10):
        raise ValueError('Некорректные параметры оценки')
    rows = text_tokens = context = byte_input = 0
    for row in core.db.execute(f"SELECT source,context,id_value FROM records WHERE {PUBLIC} AND trim(source)<>''"):
        rows += 1
        text_tokens += math.ceil(len(row['source']) / 4)
        context += math.ceil(len(row['context']) / 4) + context_tokens
        byte_input += len(row['source'].encode('utf-8')) + len(row['context'].encode('utf-8'))
    requests = math.ceil(rows / batch_size)
    # 400 system/framing tokens per request; 80 JSON/identity tokens per record.
    framing = requests * 400 + rows * 80
    glossary = requests * glossary_tokens
    base = text_tokens + framing
    expected_input = base + context + glossary
    expected_output = math.ceil(text_tokens * output_ratio) + rows * 40
    conservative_input = byte_input + context_tokens * rows + glossary + framing
    conservative_output = expected_output * 2

    def price(inputs, outputs):
        ordinary = (inputs * PRICE['input_per_million'] + outputs * PRICE['output_per_million']) / 1_000_000
        return dict(standard_usd=round(ordinary, 4), batch_usd=round(ordinary * PRICE['batch_multiplier'], 4),
                    standard_with_reserve_usd=round(ordinary * (1 + reserve), 4),
                    batch_with_reserve_usd=round(ordinary * PRICE['batch_multiplier'] * (1 + reserve), 4))

    return dict(schema_version=1, rates=PRICE, records=rows, requests=requests,
        batch_size=batch_size, source_input_tokens_estimate=text_tokens,
        context_tokens_estimate=context, glossary_tokens_estimate=glossary,
        framing_tokens_estimate=framing, input_tokens_estimate=expected_input,
        output_tokens_estimate=expected_output, output_ratio=output_ratio,
        reserve_fraction=reserve, without_context_glossary=price(base, expected_output),
        expected=price(expected_input, expected_output),
        conservative=dict(input_tokens=conservative_input, output_tokens=conservative_output,
                          **price(conservative_input, conservative_output)),
        assumptions=['chars/4 proxy, not model tokenizer', 'Russian output ratio is unmeasured until pilot',
            'all nonempty records; no source-only deduplication', 'no cache discount assumed',
            'reasoning effort none; reasoning/tool tokens not modeled',
            'short context, global endpoint, no tax or regional premium',
            'reserve covers estimation/retries, not an enforceable API budget'], paid_api_calls=0)


def pilot_rows(core, limit=500):
    if type(limit) is not int or not 100 <= limit <= 1000:
        raise ValueError('Пилот должен содержать 100..1000 строк')
    buckets = {}
    sql = (f"SELECT r.* FROM records r JOIN (SELECT min(seq) seq FROM records WHERE {PUBLIC} "
           "AND trim(source)<>'' AND length(source)<=16000 GROUP BY source_hash) u ON r.seq=u.seq")
    for row in core.db.execute(sql):
        text = row['source']
        bucket = (row['category'], min(len(text) // 100, 4), bool(TOKEN.search(text)),
                  bool(re.search('[\u3400-\u9fff]', text)))
        pool = buckets.setdefault(bucket, [])
        key = int(row['source_hash'], 16)
        item = (-key, row['seq'], dict(row))
        # Bounded deterministic reservoir per category/length/template/script stratum.
        if len(pool) < 20:
            heapq.heappush(pool, item)
        elif item > pool[0]:
            heapq.heapreplace(pool, item)
    pools = [sorted(pool, reverse=True) for _, pool in sorted(buckets.items())]
    result = []
    for index in range(20):
        for pool in pools:
            if index < len(pool):
                result.append(pool[index][2])
                if len(result) == limit:
                    return result
    selected = {row['seq'] for row in result}
    for row in core.db.execute(sql + ' ORDER BY r.source_hash LIMIT ?', (limit,)):
        if row['seq'] not in selected:
            result.append(dict(row))
            if len(result) == limit:
                break
    return result


def prepare_corpus(core, name, *, pilot_size=500, **cost_options):
    destination = output_path(core, name)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.public-') as temporary:
        folder = Path(temporary) / 'report'; folder.mkdir()
        with core.db:
            core.db.execute('BEGIN')
            statistics = corpus_statistics(core)
            pilot = pilot_rows(core, pilot_size)
            cost = estimate_cost(core, **cost_options)
            for filename, data in (('corpus.json', statistics), ('cost.json', cost)):
                (folder / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            with (folder / 'pilot.jsonl').open('w', encoding='utf-8') as stream:
                for row in pilot:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        offline_path(destination)
        os.rename(folder, destination)
    return dict(records=statistics['records'], pilot=len(pilot), cost=cost, output=str(destination))
