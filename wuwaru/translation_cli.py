"""Russian M2 command registration; no game, launcher or installation operations."""
from pathlib import Path

from .engine import Budget, TranslationEngine
from .export import export_databases
from .glossary import Glossary
from .providers import MockProvider, OpenAICompatibleProvider
from .translation_db import TranslationDB


def register(commands):
    parser = commands.add_parser('translation', help='Офлайн-ядро перевода SQLite staging (M2)')
    parser.add_argument('--workspace', type=Path, required=True, help='Рабочая папка вне игры/staging')
    sub = parser.add_subparsers(dest='translation_command', required=True)
    imp = sub.add_parser('import', help='Импортировать отдельную SQLite из staging')
    imp.add_argument('--source', type=Path, required=True)
    imp.add_argument('--resource', required=True, help='Стабильное относительное имя ресурса .db')
    public = sub.add_parser('import-public', help='Импорт английских JSON по закреплённому lock, без сети')
    public.add_argument('--lock', type=Path, required=True)
    public.add_argument('--source-root', type=Path, required=True)
    public.add_argument('--source-name', required=True)
    reuse = sub.add_parser('import-reuse', help='Офлайн EN→RU: preview или импорт кандидатов с проверкой совпадений')
    reuse.add_argument('--corpus', type=Path, required=True)
    reuse.add_argument('--apply', action='store_true', help='Сохранить кандидатов, не заменяя существующие переводы')
    merge = sub.add_parser('merge', help='M2.6: preview/apply JSON, CSV или SQLite по явной схеме')
    merge.add_argument('--source', type=Path, required=True)
    merge.add_argument('--config', type=Path, required=True)
    merge.add_argument('--apply', action='store_true')
    merge.add_argument('--expected', help='preview_hash предыдущего предпросмотра')
    rollback = sub.add_parser('merge-rollback', help='Атомарно откатить сессию импорта')
    rollback.add_argument('--import-id', required=True)
    listing = sub.add_parser('merge-candidates', help='История вариантов перевода с пагинацией')
    listing.add_argument('--record', type=int)
    listing.add_argument('--import-id', default='')
    listing.add_argument('--after', type=int, default=0)
    listing.add_argument('--include-rolled-back', action='store_true')
    sub.add_parser('coverage', help='Фактическое заполнение, вычитка и остаток для Luna, без API')
    prepare = sub.add_parser('prepare-public', help='Статистика, пилот и оценка gpt-6-luna без вызовов API')
    prepare.add_argument('--name', required=True, help='Новый output/… каталог')
    prepare.add_argument('--pilot-size', type=int, default=500)
    prepare.add_argument('--batch-size', type=int, default=100)
    prepare.add_argument('--glossary-tokens', type=int, default=1500)
    prepare.add_argument('--context-tokens', type=int, default=0, help='Дополнительные токены контекста на строку')
    prepare.add_argument('--output-ratio', type=float, default=1.8)
    prepare.add_argument('--reserve', type=float, default=0.30)
    sub.add_parser('status', help='Статусы активных строк и счётчики вызовов')
    qa = sub.add_parser('qa', help='Потоковый QA-отчёт JSON/CSV, без GUI и сетевых вызовов')
    qa.add_argument('--name', required=True, help='Новый каталог output/... или staging/...')
    for action in ('preview', 'run'):
        item = sub.add_parser(action, help='Оценить запросы' if action == 'preview' else 'Перевести/продолжить')
        item.add_argument('--provider', choices=('mock', 'compatible'), default='mock')
        item.add_argument('--base-url', help='HTTPS base URL совместимого API, без ключей')
        item.add_argument('--model', help='Явно выбранная модель')
        item.add_argument('--key-env', default='WUWARU_API_KEY', help='Имя локальной переменной ключа')
        item.add_argument('--max-output-tokens', type=int, default=4096)
        item.add_argument('--input-price', type=float, help='Цена миллиона входных токенов')
        item.add_argument('--output-price', type=float, help='Цена миллиона выходных токенов в той же валюте')
        item.add_argument('--batch-size', type=int, default=100)
        if action == 'run':
            item.add_argument('--run-id', required=True, help='Тот же ID продолжает checkpoint')
            item.add_argument('--allow-api', action='store_true', help='Явно разрешить передачу текста/платные запросы')
            item.add_argument('--max-requests', type=int, default=0)
            item.add_argument('--max-cost', type=float, default=0)
            item.add_argument('--requests-per-minute', type=float, default=60)
    export = sub.add_parser('export', help='Новая валидированная копия SQLite внутри workspace')
    export.add_argument('--name', required=True, help='Новый относительный каталог')
    export.add_argument('--allow-machine', action='store_true', help='Разрешить ещё не проверенные машинные строки')
    manual = sub.add_parser('manual', help='Сохранить защищённую ручную правку')
    manual.add_argument('--record', type=int, required=True, help='Внутренний seq; игровой ID не меняется')
    manual.add_argument('--text', required=True)
    manual.add_argument('--approve', action='store_true')
    context = sub.add_parser('context', help='Уточнить контекст записи (персонаж/квест/экран)')
    context.add_argument('--record', type=int, required=True)
    context.add_argument('--text', required=True)
    glossary = sub.add_parser('glossary', help='Добавить новую ревизию термина')
    glossary.add_argument('--term', required=True)
    glossary.add_argument('--russian', required=True)
    glossary.add_argument('--category', choices=('character', 'location', 'ability', 'item', 'other'), required=True)
    glossary.add_argument('--context', default='')
    glossary.add_argument('--verified', action='store_true')


def execute(args) -> dict:
    core = TranslationDB(args.workspace)
    try:
        action = args.translation_command
        if action == 'merge':
            from .merge import merge_import, read_config
            return merge_import(core, args.source, read_config(args.config), apply=args.apply, expected=args.expected)
        if action == 'merge-rollback':
            from .merge import rollback_import
            return rollback_import(core, args.import_id)
        if action == 'merge-candidates':
            from .merge import candidates
            return dict(schema_version=1, candidates=candidates(core, seq=args.record,
                import_id=args.import_id, after=args.after, include_rolled_back=args.include_rolled_back))
        if action == 'coverage':
            from .merge import coverage
            return coverage(core)
        if action == 'import':
            return core.import_source(args.resource, args.source)
        if action == 'import-public':
            from .public_corpus import import_corpus
            return import_corpus(core, args.lock, args.source_root, args.source_name)
        if action == 'import-reuse':
            from .reuse_import import import_reuse
            return import_reuse(core, args.corpus, apply=args.apply)
        if action == 'prepare-public':
            from .public_corpus import prepare_corpus
            return prepare_corpus(core, args.name, pilot_size=args.pilot_size, batch_size=args.batch_size,
                glossary_tokens=args.glossary_tokens, context_tokens=args.context_tokens,
                output_ratio=args.output_ratio, reserve=args.reserve)
        if action == 'status':
            return dict(schema_version=1, statuses=dict(core.db.execute(
                'SELECT status,count(*) FROM records WHERE active=1 GROUP BY status')),
                removed=core.db.execute('SELECT count(*) FROM records WHERE active=0').fetchone()[0],
                attempts=core.db.execute('SELECT count(*) FROM usage').fetchone()[0],
                glossary_version=Glossary(core).version, installation_allowed=False)
        if action == 'export':
            return export_databases(core, args.name, allow_machine=args.allow_machine)
        if action == 'qa':
            from .qa import QualityAssurance
            return QualityAssurance(core).report(args.name)
        if action == 'manual':
            core.set_manual(args.record, args.text, approved=args.approve)
            return dict(record=args.record, status=core.record(args.record)['status'])
        if action == 'context':
            core.set_context(args.record, args.text)
            return dict(record=args.record, status=core.record(args.record)['status'])
        if action == 'glossary':
            return dict(revision=Glossary(core).add(args.term, args.russian, category=args.category,
                                                  context=args.context, verified=args.verified))
        if args.provider == 'compatible':
            if not args.base_url or not args.model or args.input_price is None or args.output_price is None:
                raise ValueError('Нужны base-url, model и явно заданные обе цены; актуальные тарифы не угадываются')
            provider = OpenAICompatibleProvider(args.base_url, args.model, key_env=args.key_env,
                                                max_output_tokens=args.max_output_tokens)
        else:
            provider = MockProvider()
        interval = 0
        if action == 'run' and provider.requires_permission:
            if not 0 < args.requests_per_minute <= 60000:
                raise ValueError('Частота запросов должна быть 0..60000')
            interval = 60 / args.requests_per_minute
        engine = TranslationEngine(core, provider, interval=interval)
        if action == 'preview':
            return engine.preview(batch_size=args.batch_size, input_price=args.input_price or 0,
                                  output_price=args.output_price or 0)
        return engine.run(args.run_id, batch_size=args.batch_size,
                          budget=Budget(allowed=args.allow_api, max_requests=args.max_requests,
                                        max_cost=args.max_cost, input_price=args.input_price or 0,
                                        output_price=args.output_price or 0))
    finally:
        core.close()
