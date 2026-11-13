"""One cancellable Qt worker owns its own SQLite connection for its lifetime."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from threading import Event

from PySide6.QtCore import QThread, Signal

from .editor import Editor
from .export import export_databases
from .glossary import Glossary
from .manifest import check_cancel
from .qa import QualityAssurance, output_path
from .translation_db import TranslationDB


class EditorJob(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(int)

    def __init__(self, workspace, action, arguments, parent=None):
        super().__init__(parent)
        self.workspace, self.action, self.arguments = workspace, action, arguments
        self.cancelled = Event()

    def run(self):
        core = None
        try:
            check_cancel(self.cancelled)
            if self.action == 'connect-public':
                from .portable import PathManager, connect_public_source
                args = dict(self.arguments)
                paths = PathManager(Path(args.pop('data_root')))
                self.result.emit(connect_public_source(paths, self.workspace,
                                                      cancelled=self.cancelled, **args))
                return
            if self.action == 'migrate-workspace':
                from dataclasses import asdict
                from .portable import MigrationService, PathManager
                paths = PathManager(Path(self.arguments['data_root']))
                result = MigrationService(paths).make_self_contained(
                    Path(self.arguments['source']), cancelled=self.cancelled)
                self.result.emit({key: str(value) if isinstance(value, Path) else value
                                  for key, value in asdict(result).items()})
                return
            if self.action == 'rollback-workspace':
                from .portable import MigrationService, PathManager
                paths = PathManager(Path(self.arguments['data_root']))
                MigrationService(paths).rollback_self_contained()
                self.result.emit({'destination': str(paths.last_project())})
                return
            if self.action == 'game-discovery':
                from .portable import GameDiscoveryService, PathManager
                self.result.emit(GameDiscoveryService(
                    PathManager(Path(self.arguments['data_root']))).discover())
                return
            if self.action == 'project-files':
                import sqlite3
                from .portable import project_files_report
                report = project_files_report(self.workspace, self.arguments.get('backup_root'))
                database = self.workspace / 'translations.sqlite'
                db = sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)
                try:
                    report['integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
                    report['record_count'] = db.execute(
                        'SELECT count(*) FROM records WHERE active=1').fetchone()[0]
                finally:
                    db.close()
                self.result.emit({key: str(value) if isinstance(value, Path) else
                                  [({name: str(item_path) if isinstance(item_path, Path) else item_path
                                    for name, item_path in item.items()} if isinstance(item, dict) else str(item))
                                   for item in value] if isinstance(value, list) else value
                                  for key, value in report.items()})
                return
            if self.action == 'project-backup':
                from .portable import PathManager, create_project_backup
                report = create_project_backup(
                    PathManager(Path(self.arguments['data_root'])), self.workspace)
                self.result.emit({key: str(value) if isinstance(value, Path) else value
                                  for key, value in report.items()})
                return
            if self.action.startswith('native-'):
                self.result.emit(self.native(self.action, self.arguments))
                return
            if self.action == 'delta-update':
                from .delta_updates import run_delta_update
                from .portable import PathManager
                self.result.emit(run_delta_update(PathManager(Path(self.arguments['data_root'])), self.workspace,
                                                  cancelled=self.cancelled.is_set))
                return
            if self.action == 'api-models':
                from .provider_config import ProviderProfile
                profile = ProviderProfile(**self.arguments['profile'])
                models = profile.provider().list_models(allow_network=True)
                check_cancel(self.cancelled)
                self.result.emit(dict(profile=self.arguments['profile'], models=models))
                return
            core = TranslationDB(self.workspace)
            core.db.execute('PRAGMA busy_timeout=250')
            core.db.set_progress_handler(lambda: int(self.cancelled.is_set()), 2000)
            editor, args, action = Editor(core), self.arguments, self.action
            if action == 'overview':
                from .product import project_overview
                result = project_overview(core)
            elif action in ('pilot-preview', 'pilot-run', 'pilot-results', 'pilot-approve'):
                import json
                from .pilot import prepare_synthetic, preview_pilot, run_pilot, pilot_results, approve_pilot_result
                from .provider_config import ProviderProfile
                from .paths import inside
                limits = {key: args[key] for key in ('max_requests', 'max_rows', 'max_output_tokens') if key in args}
                public = args.get('public', False)
                profile = ProviderProfile(**args['profile']) if 'profile' in args else None
                if public and action == 'pilot-run':
                    raise PermissionError('Передача PUBLIC_TEXTMAP запрещена: права UNKNOWN')
                pilot = core if public else prepare_synthetic(self.workspace)
                try:
                    if action in ('pilot-results', 'pilot-approve'):
                        if public:
                            raise PermissionError('Просмотр API доступен для собственного synthetic проекта')
                        if action == 'pilot-approve':
                            approve_pilot_result(pilot, seq=args['seq'], expected=args['expected'], revision=args['revision'])
                        self.result.emit(pilot_results(pilot, profile=profile,
                            **{k: v for k, v in limits.items() if k != 'max_requests'}))
                        return
                    if action == 'pilot-run':
                        run_pilot(pilot, confirmed=args.get('confirmed', False), expected=args.get('expected'),
                                  cancelled=self.cancelled, after_batch=self.progress.emit, profile=profile, **limits)
                    result = preview_pilot(pilot, public=public, cancelled=self.cancelled, profile=profile, **limits)
                    if not public:
                        result['results'] = pilot_results(pilot, profile=profile,
                            **{k: v for k, v in limits.items() if k != 'max_requests'})
                        result['network_sent'] = bool(result['results']['usage'])
                    folder = inside(self.workspace, 'reports')
                    folder.mkdir(exist_ok=True)
                    path = inside(folder, 'pilot-public.json' if public else 'pilot-synthetic.json')
                    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                    result['report_path'] = str(path)
                finally:
                    if pilot is not core:
                        pilot.close()
            elif action in ('mock-preview', 'mock-run'):
                from .engine import Budget, TranslationEngine
                from .product import seed_mock_demo
                from .providers import MockProvider
                from .translation_db import TranslationDB as DemoDB
                seed_mock_demo(self.workspace)
                demo = DemoDB(self.workspace / '.mock-demo')
                try:
                    engine = TranslationEngine(demo, MockProvider(), interval=0)
                    preview = engine.preview(batch_size=100)
                    max_requests = args.get('max_requests', 10)
                    prior_requests = demo.db.execute('SELECT count(*) FROM usage WHERE run_id=?',
                        (args.get('run_id', 'product-1-mock-demo'),)).fetchone()[0]
                    if prior_requests + preview['requests'] > max_requests:
                        raise ValueError('Предпросмотр превышает лимит batch-вызовов Mock run')
                    if action == 'mock-preview':
                        result = dict(preview=preview, records=seed_mock_demo(self.workspace)['records'])
                    else:
                        run = engine.run(args.get('run_id', 'product-1-mock-demo'),
                                         budget=Budget(), cancelled=self.cancelled,
                                         after_batch=lambda count: self.progress.emit(count))
                        result = dict(preview=preview, run=run,
                                      rows=[dict(row) for row in demo.db.execute(
                                          'SELECT seq,source,target,status,manual FROM records '
                                          'WHERE active=1 ORDER BY seq')])
                finally:
                    demo.close()
            elif action == 'resource-catalog':
                from datetime import datetime
                from .resource_catalog import LocalResourceCatalog
                from .resource_catalog_index import ResourceCatalogIndex
                root = Path(args['root']).absolute()
                folder = self.workspace / 'catalogs'
                folder.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                manifest = folder / f'resources-{stamp}.jsonl'
                report = LocalResourceCatalog(root, cancelled=self.cancelled).write_entry_manifest(manifest)
                index = ResourceCatalogIndex.build(manifest, folder / f'resources-{stamp}.sqlite',
                                                   cancelled=self.cancelled)
                result = dict(manifest=str(manifest), index=str(index), **report)
            elif action == 'pak-export':
                from .pak_builder import build_package
                from .paths import inside
                result = build_package(Path(args['source']), inside(core.workspace, args['name']),
                                       cancelled=self.cancelled)
            elif action == 'page':
                with core.db:
                    core.db.execute('BEGIN')
                    result = editor.page(**args, cancelled=self.cancelled)
            elif action == 'detail':
                with core.db:
                    core.db.execute('BEGIN')
                    seq = args['seq']
                    result = dict(row=dict(core.record(seq)), version=editor.version(seq),
                                  issues=editor.issues(seq), history=editor.history(seq),
                                  links=[r[0] for r in core.db.execute('SELECT related FROM context_links WHERE seq=?', (seq,))])
                    import json
                    result['provenance'] = json.loads(core.db.execute(
                        'SELECT inspection FROM resources WHERE name=?', (result['row']['resource'],)).fetchone()[0])
                    result['row']['merge_status'] = editor.merge_status(seq)
                    from .merge import candidates
                    result['merge_candidates'] = candidates(core, seq=seq)
            elif action == 'save':
                editor.save(**args)
                result = None
            elif action == 'restore':
                editor.restore(**args)
                result = None
            elif action == 'statuses':
                editor.statuses(**args, cancelled=self.cancelled)
                result = None
            elif action == 'category':
                editor.categorize(**args)
                result = None
            elif action == 'context':
                with core.db:
                    core.db.execute('BEGIN IMMEDIATE')
                    editor.checked(args['seq'], args['expected'])
                    # set_context's transaction commits all work, including the check.
                    core.set_context(args['seq'], args['context'])
                result = None
            elif action == 'link':
                editor.link_context(**args)
                result = None
            elif action == 'history':
                result = editor.history(**args)
            elif action == 'term':
                with core.db:
                    core.db.execute('BEGIN')
                    result = dict(rows=editor.page(term=args['term'], after=args.get('after', 0), cancelled=self.cancelled),
                                  variants=editor.variants(args['term'], after=args.get('variant_after', '')))
            elif action == 'glossary':
                result = Glossary(core).add(**args, verified=True)
            elif action == 'report':
                result = QualityAssurance(core).report(**args, cancelled=self.cancelled, progress=self.progress.emit)
            elif action == 'export':
                output_path(core, args['name'])
                result = export_databases(core, **args, cancelled=self.cancelled)
            elif action == 'import':
                result = core.import_source(**args, cancelled=self.cancelled)
            elif action == 'import-public':
                from .public_corpus import import_corpus
                result = import_corpus(core, **args, cancelled=self.cancelled)
            elif action == 'merge':
                from .merge import merge_import
                result = merge_import(core, **args, cancelled=self.cancelled)
            elif action == 'merge-rollback':
                from .merge import rollback_import
                result = rollback_import(core, **args, cancelled=self.cancelled)
            elif action == 'merge-candidates':
                from .merge import candidates
                result = candidates(core, **args)
            elif action == 'coverage':
                from .merge import coverage
                result = coverage(core)
            else:
                raise ValueError('Неизвестная операция редактора')
            self.result.emit(result)
        except Exception as exc:
            self.failed.emit('Операция отменена' if self.cancelled.is_set() else str(exc)
                             if type(exc) in (ValueError, PermissionError) else f'{type(exc).__name__}: {exc}')
        finally:
            if core is not None:
                core.close()

    def native(self, action, args):
        """Play page steps. Module attributes are looked up at call time so tests can replace them."""
        from . import native_helper, native_launch, native_package
        from .paths import inside
        native = Path(args['native'])
        game = Path(args['game']) if args.get('game') else None
        if game is not None:  # a saved launcher folder means the game below it, never its leftovers
            from .discovery import inspect_installation
            try:
                game = inspect_installation(game).root
            except (OSError, ValueError):
                pass
        if action == 'native-inspect':
            key, error = args.get('key'), None
            if key is None:
                try:
                    key = native_package.current_key(Path(args['project_db']), native, tuple(args['kinds']))
                except FileNotFoundError:
                    error = 'Нет входных файлов в Data/Native: lang_multi_text.en.db и NotoSans-600.ttf'
                except (OSError, ValueError, sqlite3.Error) as exc:  # ruling R2: foreign inputs → NO_INPUTS
                    error = str(exc)
            check_cancel(self.cancelled)
            return native_launch.inspect(game, native, key, input_error=error, build_id=args.get('build_id'))
        if action == 'native-build':
            installed = native_launch.pak_path(game) if game is not None else None
            return native_package.build_package(Path(args['project_db']), native, tuple(args['kinds']),
                                                cancelled=self.cancelled, installed=installed)
        if action == 'native-inputs':
            try:  # a wrong hash is an expected answer for the page, not a worker failure
                native_package.install_inputs(Path(args['table']), Path(args['font']), native)
            except ValueError as exc:
                return {'error': str(exc)}
            return {'error': None}
        if action == 'native-apply':
            request = Path(args['request'])
            code = native_helper.run_elevated(request)
            path = request.with_name(request.stem + '.result.json')
            result = None
            if path.is_file():
                try:
                    result = json.loads(native_launch.read_bounded(path, 1024 * 1024).decode('utf-8'))
                except (OSError, ValueError):
                    result = None
            return {'code': code, 'result': result if isinstance(result, dict) else None}
        raise ValueError('Неизвестная операция запуска')
