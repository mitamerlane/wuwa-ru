"""One translation wave of one project: `preview` stays offline, `send` spends tokens.

`send` needs the exact volume a human saw in the preview and a token cap. Nothing here picks a wave on its own.
Answers stay in the project's TranslationDB; writing to the game is a separate step.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from wuwaru.engine import Budget, TranslationEngine
from wuwaru.flow_context import WAVES
from wuwaru.provider_config import ProviderProfile
from wuwaru.translation_db import TranslationDB

MODEL = 'gpt-6-luna'
# Fitted to the four runs so far: pilots A, B, C and the first 11 requests of the names wave, input plus output,
# all within 1.5%. Four points and three numbers: new kinds of text may differ, and the gap check guards them.
TOKENS_PER_CHAR, TOKENS_PER_ITEM, TOKENS_PER_REQUEST = 0.72, 24, 400
BATCH_ITEMS = 150


def relay_provider(key_env: str | None = None, key_getter=None):
    """On a server the key comes from a variable the user set there; it is read at request time and never stored."""
    getter = key_getter or ((lambda: os.environ.get(key_env)) if key_env else None)
    return ProviderProfile('RelayModels', model=MODEL).provider(scenes=True, key_getter=getter)


def exit_code(result: dict) -> int:
    return 3 if result.get('state', 'DONE') != 'DONE' else 0


def estimate_tokens(chars: int, items: int, requests: int) -> int:
    return round(chars * TOKENS_PER_CHAR + items * TOKENS_PER_ITEM + requests * TOKENS_PER_REQUEST)


def wave_preview(core: TranslationDB, wave: str | None, *, changed_since: int | None = None, provider=None) -> dict:
    """`wave=None` with `changed_since` is one game update: every kind, only the records that import touched."""
    provider = provider or relay_provider()
    engine = TranslationEngine(core, provider, scenes=True, wave=wave, changed_since=changed_since)
    requests, cells, chars, items = 0, 0, 0, 0
    for rows in engine.batches(BATCH_ITEMS):
        _, pending, _ = engine.resolve(rows)
        cells += len(rows)
        requests += bool(pending)
        chars += sum(len(item.source) for item in pending)
        items += len(pending)
    return dict(wave=wave, pending_records=cells, requests=requests, paid_items=items, source_chars=chars,
                estimated_tokens=estimate_tokens(chars, items, requests), evidence='ESTIMATE', network_sent=False)


def check_go(preview: dict, *, expect_requests: int, expect_cells: int, token_cap: int) -> None:
    if preview['requests'] != expect_requests or preview['pending_records'] != expect_cells:
        raise PermissionError('Объём изменился после предпросмотра: нужно новое разрешение')
    if token_cap < preview['estimated_tokens']:
        raise PermissionError('Предел токенов меньше оценки волны')


def send(core: TranslationDB, wave: str | None, *, run_id: str, token_cap: int, requests: int, key_env: str | None = None,
         changed_since: int | None = None, provider=None) -> dict:
    provider = provider or relay_provider(key_env)
    engine = TranslationEngine(core, provider, scenes=True, wave=wave, attempts=2, interval=1, changed_since=changed_since)
    spent_requests = core.db.execute('SELECT count(*) FROM usage WHERE run_id=?', (run_id,)).fetchone()[0]
    budget = Budget(allowed=True, max_requests=spent_requests + requests * 2, token_cap=token_cap, tokens_per_char=TOKENS_PER_CHAR,
                    tokens_per_item=TOKENS_PER_ITEM, tokens_per_request=TOKENS_PER_REQUEST)
    return engine.run(run_id, batch_size=BATCH_ITEMS, budget=budget)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('preview', 'send'))
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--wave', choices=tuple(WAVES), required=True)
    parser.add_argument('--expect-requests', type=int)
    parser.add_argument('--expect-cells', type=int)
    parser.add_argument('--token-cap', type=int)
    parser.add_argument('--key-env', help='Имя переменной окружения с ключом, на сервере вместо хранилища Windows')
    parser.add_argument('--run-id', help='Новый ID нужен, если после паузы изменился словарь или лист персонажей')
    arguments = parser.parse_args()
    core = TranslationDB(arguments.workspace)
    try:
        preview = wave_preview(core, arguments.wave)
        if arguments.command == 'preview':
            result = preview
        else:
            if None in (arguments.expect_requests, arguments.expect_cells, arguments.token_cap):
                parser.error('send требует --expect-requests, --expect-cells и --token-cap')
            check_go(preview, expect_requests=arguments.expect_requests, expect_cells=arguments.expect_cells, token_cap=arguments.token_cap)
            result = send(core, arguments.wave, run_id=arguments.run_id or f'wave-{arguments.wave}', token_cap=arguments.token_cap, requests=preview['requests'], key_env=arguments.key_env)
    finally:
        core.close()
    print(json.dumps(result, indent=1))
    return exit_code(result)


if __name__ == '__main__':
    sys.exit(main())
