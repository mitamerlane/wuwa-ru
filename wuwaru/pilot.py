"""Bounded pilot preparation around TranslationEngine; public text stays offline."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .billing import pricing
from .core_contracts import ResourceSourceType
from .engine import Budget, TranslationEngine
from .glossary import Glossary
from .manifest import check_cancel
from .paths import inside
from .providers import OpenAICompatibleProvider
from .translation_db import TranslationDB, digest
from .validation import TOKEN

MODEL = 'gpt-6-luna'
INPUT_PRICE, OUTPUT_PRICE = 0.10, 0.50  # USD/MTok, Standard, checked 2026-10-25.
BATCH_SIZE, MAX_COST, MAX_REQUESTS = 25, 0.10, 24
CATEGORIES = ('ui', 'quest', 'dialogue', 'ability', 'item')


def synthetic_rows():
    """Authored here, independent of game text or characters."""
    templates = (
        'Open the lantern menu for station {n}. Press {{Action}} to confirm.',
        'At dusk, the glass gardener left {n} blue seeds beside the silent observatory.\nFind the missing ledger.',
        'Mira: "Did you count all {n} lanterns?"\nOren: "One of them keeps answering me, {{Player}}."',
        '<b>Lantern Pulse {n}</b>\nRestore {{Amount}} energy to nearby allies every 3 seconds.',
        'Glass Seed {n}\nA warm seed wrapped in paper. Deliver {{Count}} to the garden keeper.',
    )
    return [(f'{category}-{n:02}', template.format(n=n), category,
             f'Authored lantern garden test; category={category}; scene=station-{n}; '
             + ('speakers=Mira,Oren; tone=curious' if category == 'dialogue' else 'tone=clear'))
            for category, template in zip(CATEGORIES, templates) for n in range(1, 41)]


def prepare_synthetic(workspace: Path) -> TranslationDB:
    workspace = Path(workspace).absolute()
    source = inside(workspace, '.pilot-source.sqlite')
    if not source.exists():
        with closing(sqlite3.connect(source)) as db, db:
            db.execute('CREATE TABLE SyntheticText (Id TEXT PRIMARY KEY, Content TEXT)')
            db.executemany('INSERT INTO SyntheticText VALUES (?,?)', [(key, text) for key, text, _, _ in synthetic_rows()])
    core = TranslationDB(inside(workspace, '.pilot-synthetic'))
    try:
        if not core.db.execute('SELECT count(*) FROM resources').fetchone()[0]:
            core.import_source('pilot/synthetic.db', source,
                source_type=ResourceSourceType.SYNTHETIC_FIXTURE,
                source_metadata={'purpose': 'self-authored pilot; no game corpus'})
            with core.db:
                for key, _, category, context in synthetic_rows():
                    core.db.execute('UPDATE records SET category=?,context=? WHERE id_value=?', (category, context, key))
            Glossary(core).add('Lantern Pulse', 'Фонарный импульс', category='ability', verified=True)
            Glossary(core).add('Glass Seed', 'Стеклянное семя', category='item', verified=True)
        return core
    except BaseException:
        core.close()
        raise


def select_public(core: TranslationDB, cancelled=None):
    """40 length quantiles/category; category is an existing path/ID heuristic."""
    selected = []
    for category in CATEGORIES:
        where = "active=1 AND source_type='PUBLIC_TEXTMAP' AND category=? AND length(trim(source))>0"
        # Exclude CJK-only/placeholder-only records locally; do not fabricate English.
        rows = core.db.execute('SELECT * FROM records WHERE ' + where + ' ORDER BY length(source),seq', (category,))
        eligible = []
        for row in rows:
            check_cancel(cancelled)
            if re.search('[A-Za-z]{2}', TOKEN.sub('', row['source'])):
                eligible.append(row['seq'])
        if len(eligible) < 40:
            raise ValueError(f'Недостаточно английских строк категории {category}: {len(eligible)}/40')
        # Only integer IDs retained during scan, never full corpus text.
        for n in range(40):
            selected.append(dict(core.record(eligible[(2 * n + 1) * len(eligible) // 80])))
    return selected


def provider(*, max_output_tokens=8192):
    return OpenAICompatibleProvider('https://api.openai.com/v1', MODEL, max_output_tokens=max_output_tokens)


def preview_pilot(core: TranslationDB, *, public=False, cancelled=None, profile=None,
                  max_requests=MAX_REQUESTS, max_rows=200, max_output_tokens=8192):
    if (type(max_requests) is not int or not 1 <= max_requests <= 24 or
            type(max_rows) is not int or not 1 <= max_rows <= 200 or
            type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192):
        raise ValueError("Пилот: 1..24 попытки, 1..200 строк, 1..8192 выходных токенов")
    rows = select_public(core, cancelled) if public else [dict(r) for r in core.db.execute('SELECT * FROM records WHERE active=1 ORDER BY seq')]
    if len(rows) != 200:
        raise ValueError('Пилот должен содержать ровно 200 строк')
    rows = rows[:max_rows]
    active_provider = profile.provider(max_output_tokens=max_output_tokens) if profile else provider(max_output_tokens=max_output_tokens)
    prices = pricing(profile)
    input_price, output_price = prices['input'], prices['output']
    known_price = input_price is not None and output_price is not None
    engine = TranslationEngine(core, active_provider, attempts=1, selection=[r['seq'] for r in rows])
    glossary = Glossary(core)
    sample = []
    for row in rows:
        check_cancel(cancelled)
        hints = glossary.hints(row['source'], row['context'])
        sample.append(dict(seq=row['seq'], resource=row['resource'], table=row['table_name'],
            id_type=row['id_type'], id=row['id_value'], source_hash=row['source_hash'],
            category=row['category'], source=row['source'], context=row['context'], glossary=hints,
            placeholders=TOKEN.findall(row['source']),
            semantic_context_known=row['context'] != row['resource'] + '/' + row['table_name']))
    requests = []
    for batch in engine.batches(BATCH_SIZE):
        check_cancel(cancelled)
        _, pending, keys = engine.resolve(batch)
        if pending:
            requests.append(dict(seq_to_key=keys, body=engine.provider.request_body(pending)))
    estimate = engine.preview(batch_size=BATCH_SIZE, input_price=input_price, output_price=output_price)
    if not known_price:
        estimate.update(estimated_cost=None, estimated_cost_with_retries=None, pricing='UNKNOWN')
    report = dict(schema_version=1, model=profile.model if profile else MODEL,
        provider=profile.name if profile else 'OpenAI', base_url=profile.base_url if profile else 'https://api.openai.com/v1',
        records=len(rows), network_sent=False,
        source_type='PUBLIC_TEXTMAP' if public else 'SYNTHETIC_FIXTURE',
        rights='UNKNOWN_EXTERNAL_TRANSMISSION_BLOCKED' if public else 'SELF_AUTHORED',
        pricing=prices,
        preview=estimate,
        limits=dict(max_cost_usd=MAX_COST if known_price else None, max_requests=max_requests,
                    max_rows=max_rows, max_output_tokens=max_output_tokens, batch_size=BATCH_SIZE),
        glossary_version=glossary.version, glossary_matches=sum(bool(r['glossary']) for r in sample),
        semantic_context_known=sum(r['semantic_context_known'] for r in sample),
        resources=[dict(r) for r in core.db.execute('SELECT name,sha256,inspection FROM resources')
                   if r['name'] in {row['resource'] for row in rows}],
        sample=sample, requests=requests)
    used = 0 if public else core.db.execute('SELECT count(*) FROM usage WHERE run_id=?',
        (pilot_run_id(profile, max_rows=max_rows, max_output_tokens=max_output_tokens),)).fetchone()[0]
    report['budget_usage'] = dict(attempts_used=used, attempts_remaining=max(0, max_requests-used),
                                automatic_retries=False)
    report['preview_hash'] = digest(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def run_pilot(core: TranslationDB, *, confirmed=False, expected=None, cancelled=None, after_batch=None, profile=None,
              max_requests=MAX_REQUESTS, max_rows=200, max_output_tokens=8192):
    if not confirmed:
        raise PermissionError('Платный пилот требует явного подтверждения после preview')
    # Allow only the exact authored source/context and glossary; never trust a source_type label alone.
    actual = {(r['id_value'], r['source'], r['category'], r['context']) for r in core.db.execute(
        "SELECT * FROM records WHERE active=1 AND source_type='SYNTHETIC_FIXTURE' AND resource='pilot/synthetic.db'")}
    terms = [tuple(r) for r in core.db.execute('SELECT term,russian,category,context,verified FROM glossary ORDER BY revision')]
    if (actual != set(synthetic_rows()) or core.db.execute('SELECT count(*) FROM records').fetchone()[0] != 200
            or terms != [('Lantern Pulse', 'Фонарный импульс', 'ability', '', 1),
                         ('Glass Seed', 'Стеклянное семя', 'item', '', 1)]):
        raise PermissionError('Сеть разрешена только для неизменённого собственного synthetic pilot')
    report = preview_pilot(core, cancelled=cancelled, profile=profile, max_requests=max_requests,
                           max_rows=max_rows, max_output_tokens=max_output_tokens)
    if expected != report['preview_hash']:
        raise ValueError('Preview устарел: сначала обнови предпросмотр')
    active_provider = profile.provider(max_output_tokens=max_output_tokens) if profile else provider(max_output_tokens=max_output_tokens)
    run_id = pilot_run_id(profile, max_rows=max_rows, max_output_tokens=max_output_tokens)
    return TranslationEngine(core, active_provider, attempts=1, selection=[r['seq'] for r in report['sample']]).run(run_id, batch_size=BATCH_SIZE,
        budget=Budget(allowed=True, max_requests=max_requests, max_cost=MAX_COST, bounded_pilot=True,
                      input_price=report['pricing']['input'], output_price=report['pricing']['output']),
        cancelled=cancelled, after_batch=after_batch)


def pilot_run_id(profile=None, *, max_rows=200, max_output_tokens=8192):
    base = ('api-synthetic-' + profile.provider(max_output_tokens=max_output_tokens).identity.split(':')[-1][:32]
            if profile else 'luna-synthetic-pilot-v1')
    return base + f'-rows{max_rows}-tokens{max_output_tokens}'


def pilot_results(core, *, profile=None, max_rows=5, max_output_tokens=1024):
    """Read persisted API outputs, never substitute current manual text for model text."""
    from .editor import Editor
    from .qa import QualityAssurance
    from dataclasses import asdict
    active = profile.provider(max_output_tokens=max_output_tokens) if profile else provider(max_output_tokens=max_output_tokens)
    run_id = pilot_run_id(profile, max_rows=max_rows, max_output_tokens=max_output_tokens)
    run = core.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    usage = []
    for item in core.db.execute('SELECT * FROM usage WHERE run_id=? ORDER BY attempt', (run_id,)):
        item = dict(item)
        item['actual_usage'] = json.loads(item.pop('actual_usage_json') or 'null')
        usage.append(item)
    editor, qa, rows = Editor(core), QualityAssurance(core), []
    if run and any(u['provider'] == active.identity and u['state'] == 'RECEIVED' for u in usage):
        for current in core.db.execute('SELECT * FROM records ORDER BY seq LIMIT ?', (max_rows,)):
            for history in core.db.execute("SELECT revision,after_json FROM history WHERE seq=? AND event='AUTOMATIC' ORDER BY revision DESC", (current['seq'],)):
                saved = json.loads(history['after_json'])
                if saved['provider'] != active.identity or saved['source_hash'] != current['source_hash']:
                    continue
                issues = qa.issues(saved)
                rows.append(dict(current, model_target=saved['target'], revision=history['revision'],
                    version=editor.version(current['seq']), qa=[f'{i.severity}: {i.code}' for i in issues],
                    can_approve=not current['manual'] and not any(i.severity == 'ERROR' for i in issues)))
                break
    return dict(run=dict(run) if run else None, rows=rows, usage=usage,
                provider=profile.name if profile else 'OpenAI', model=active.model,
                workspace=str(core.workspace), profile=asdict(profile) if profile else None,
                limits=dict(max_rows=max_rows, max_output_tokens=max_output_tokens))


def approve_pilot_result(core, *, seq, expected, revision):
    from .editor import Editor
    from .qa import QualityAssurance
    editor = Editor(core)
    with core.db:
        core.db.execute('BEGIN IMMEDIATE')
        current = editor.checked(seq, expected)
        if current['manual']:
            raise ValueError('Ручная правка защищена. Для изменения используй редактор.')
        history = core.db.execute("SELECT after_json FROM history WHERE seq=? AND revision=? AND event='AUTOMATIC'", (seq, revision)).fetchone()
        if not history:
            raise ValueError('Сохранённый ответ не найден')
        saved = json.loads(history[0])
        if (not saved['provider'].startswith('compatible-v2:') or saved['source_hash'] != current['source_hash']
                or any(i.severity == 'ERROR' for i in QualityAssurance(core).issues(saved))):
            raise ValueError('Утверждение заблокировано: происхождение, исходник или QA')
    # Editor.save rechecks the optimistic version inside its own transaction.
    editor.save(seq, saved['target'], expected, status='APPROVED')
