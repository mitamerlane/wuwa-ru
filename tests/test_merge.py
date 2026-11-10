import csv
import json
import sqlite3
from contextlib import closing
from threading import Event
from unittest.mock import patch

from tests.test_discovery import FixtureCase
from wuwaru.public_text import PublicTextSourceAdapter
from wuwaru.translation_db import TranslationDB, MIGRATIONS, APPLICATION_ID
from wuwaru.merge import merge_import, rollback_import, coverage, candidates


class MergeTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.core = TranslationDB(self.home / 'work')
        self.addCleanup(self.core.close)
        source = self.home / 'en.json'
        source.write_text(json.dumps({'a': 'Hello', 'b': 'Hello', 'c': 'Sword',
                                     'd': 'Keep', 'e': 'Open {name}'}), encoding='utf-8')
        PublicTextSourceAdapter(self.core).import_file(source, resource='public/en.json',
            repository='https://github.com/example/data', revision='a'*40, data_version='3.7',
            data_date=None, source_path='Textmaps/en/en.json')
        self.core.set_context(1, 'greeting')
        self.core.set_context(2, 'farewell')
        self.path = self.home / 'ru.json'
        self.config = dict(schema_version=1, format='json', fields=dict(
            source='en', target='ru', id='id', context='ctx'), defaults=dict(version='3.7'),
            provenance=dict(name='Synthetic', url='https://example.org', version='r1',
                author='Fixture author', rights_confirmed=True, rights_evidence='synthetic-owned',
                usage='offline translation merge'))

    def write(self, rows):
        self.path.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')

    def apply(self):
        preview = merge_import(self.core, self.path, self.config)
        return merge_import(self.core, self.path, self.config, apply=True,
                            expected=preview['preview_hash'])

    def test_preview_alignments_context_version_typed_id_and_manual(self):
        self.core.set_manual(4, 'Сохранить', approved=True)
        self.write([dict(id='c', en='Sword', ru='Меч'),
                    dict(en='Hello', ctx='greeting', ru='Привет'),
                    dict(en='Hello', ru='Здравствуйте'),
                    dict(id='d', en='Keep', ru='Хранить'),
                    dict(id='e', en='Old', ru='Старый'),
                    dict(id=1, en='Sword', ru='Меч')])
        before = self.core.db.total_changes
        preview = merge_import(self.core, self.path, self.config)
        self.assertEqual(self.core.db.total_changes, before)
        self.assertEqual(preview['eligible_rows'], 2)
        self.assertEqual(set(preview['alignments']), {'EXACT', 'CONTEXT_MATCH', 'AMBIGUOUS', 'UNMATCHED'})
        result = self.apply()
        self.assertEqual(result['applied_rows'], 2)
        self.assertEqual(self.core.record(1)['target'], 'Привет')
        self.assertIsNone(self.core.record(2)['target'])
        self.assertEqual(self.core.record(4)['target'], 'Сохранить')
        self.assertEqual(self.core.record(3)['status'], 'NEEDS_REVIEW')
        details = candidates(self.core, seq=3)
        self.assertEqual(details[0]['provenance']['author'], 'Fixture author')
        self.assertEqual(self.apply()['applied_rows'], 0)
        self.config['defaults']['version'] = '3.6'
        self.assertEqual(merge_import(self.core, self.path, self.config)['eligible_rows'], 0)

    def test_conflicts_across_imports_and_full_rollback(self):
        self.write([dict(id='c', en='Sword', ru='Меч')])
        first = self.apply()
        self.write([dict(id='c', en='Sword', ru='Клинок')])
        second = self.apply()
        self.assertEqual(second['conflict_rows'], 1)
        self.assertEqual(self.core.record(3)['target'], 'Меч')
        self.assertEqual(len(candidates(self.core, seq=3)), 2)
        rollback_import(self.core, second['import_id'])
        rollback_import(self.core, first['import_id'])
        self.assertIsNone(self.core.record(3)['target'])
        self.assertEqual(coverage(self.core)['remaining_rows'], 5)
        self.assertEqual(len(candidates(self.core, seq=3)), 0)
        self.assertEqual(len(candidates(self.core, seq=3, include_rolled_back=True)), 2)

    def test_same_batch_conflicts_invalid_and_rollback_refuses_later_edit(self):
        self.write([dict(id='c', en='Sword', ru='Меч'), dict(id='c', en='Sword', ru='Клинок'),
                    dict(id='e', en='Open {name}', ru='Открыть')])
        result = self.apply()
        self.assertEqual(result['conflict_rows'], 1)
        self.assertEqual(result['applied_rows'], 0)
        self.assertIn('INVALID', result['decisions'])
        self.write([dict(id='c', en='Sword', ru='Меч')])
        rollback_import(self.core, result['import_id'])
        imported = self.apply()
        self.core.set_manual(3, 'Ручной меч', approved=True)
        with self.assertRaisesRegex(ValueError, 'измен'):
            rollback_import(self.core, imported['import_id'])
        self.assertEqual(self.core.record(3)['target'], 'Ручной меч')

    def test_stale_preview_rights_atomic_failure_and_cancel(self):
        self.write([dict(id='c', en='Sword', ru='Меч')])
        preview = merge_import(self.core, self.path, self.config)
        self.core.set_manual(3, 'Меч', approved=True)
        with self.assertRaisesRegex(ValueError, 'preview|Предпросмотр'):
            merge_import(self.core, self.path, self.config, apply=True, expected=preview['preview_hash'])
        self.config['provenance']['rights_confirmed'] = False
        with self.assertRaises(ValueError):
            self.apply()
        self.config['provenance']['rights_confirmed'] = True
        self.write([dict(id='d', en='Keep', ru='Хранить')])
        with patch.object(self.core, 'audit', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertIsNone(self.core.record(4)['target'])
        self.assertEqual(self.core.db.execute('SELECT count(*) FROM merge_imports').fetchone()[0], 0)
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(Exception):
            merge_import(self.core, self.path, self.config, cancelled=cancelled)

    def test_csv_sqlite_and_json_object_explicit_schema(self):
        for fmt in ('csv', 'sqlite', 'json'):
            with self.subTest(format=fmt):
                path = self.home / ('pairs.' + fmt)
                config = {**self.config, 'format': fmt, 'fields': dict(source='en', target='ru', id='id')}
                if fmt == 'csv':
                    with path.open('w', encoding='utf-8', newline='') as stream:
                        writer = csv.writer(stream)
                        writer.writerow(['id', 'en', 'ru'])
                        writer.writerow(['c', 'Sword', 'Меч'])
                elif fmt == 'sqlite':
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute('CREATE TABLE pairs(id TEXT,en TEXT,ru TEXT)')
                        db.execute("INSERT INTO pairs VALUES ('c','Sword','Меч')")
                    config['table'] = 'pairs'
                else:
                    path.write_text('{"c":{"en":"Sword","ru":"Меч"}}', encoding='utf-8')
                    config['fields'] = dict(id='Id', source=['Content', 'en'], target=['Content', 'ru'])
                self.assertEqual(merge_import(self.core, path, config)['eligible_rows'], 1)

    def test_missing_en_or_unknown_version_never_fills_target(self):
        self.write([dict(id='c', ru='Меч')])
        self.assertEqual(self.apply()['applied_rows'], 0)
        self.write([dict(id='c', en='Sword', ru='Меч')])
        self.config['defaults'].clear()
        self.assertEqual(self.apply()['applied_rows'], 0)

    def test_coverage_separates_filled_reviewed_unique_and_queue(self):
        self.write([dict(id='c', en='Sword', ru='Меч')])
        self.apply()
        report = coverage(self.core)
        self.assertEqual(report['filled_rows'], 1)
        self.assertEqual(report['reviewed_rows'], 0)
        self.assertEqual(report['remaining_rows'], 4)
        self.assertEqual(report['remaining_unique_en'], 3)
        self.assertEqual(report['luna_queue_rows'], 4)

    def test_rollback_is_all_or_nothing_when_second_row_changed(self):
        self.write([dict(id='c', en='Sword', ru='Меч'), dict(id='d', en='Keep', ru='Хранить')])
        report = self.apply()
        self.core.set_manual(4, 'Сохранить', approved=True)
        with self.assertRaises(ValueError):
            rollback_import(self.core, report['import_id'])
        self.assertEqual(self.core.record(3)['target'], 'Меч')
        self.assertEqual(self.core.db.execute('SELECT state FROM merge_imports').fetchone()[0], 'APPLIED')

    def test_changed_file_and_apply_without_preview_refused(self):
        self.write([dict(id='c', en='Sword', ru='Меч')])
        preview = merge_import(self.core, self.path, self.config)
        with self.assertRaises(ValueError):
            merge_import(self.core, self.path, self.config, apply=True)
        self.write([dict(id='c', en='Sword', ru='Клинок')])
        with self.assertRaises(ValueError):
            merge_import(self.core, self.path, self.config, apply=True, expected=preview['preview_hash'])

    def test_integer_csv_requires_explicit_type_and_never_guesses(self):
        path = self.home / 'integer.csv'
        path.write_text('id,en,ru\n7,Sword,Меч\n', encoding='utf-8')
        with self.core.db:
            self.core.db.execute("UPDATE records SET id_type='integer',id_value='7' WHERE seq=3")
        config = {**self.config, 'format': 'csv'}
        self.assertEqual(merge_import(self.core, path, config)['eligible_rows'], 0)
        config['id_type'] = 'integer'
        self.assertEqual(merge_import(self.core, path, config)['eligible_rows'], 1)
        path.write_text('id,en,ru\n007,Sword,Меч\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            merge_import(self.core, path, config)

    def test_cancel_mid_apply_rolls_back_whole_session(self):
        self.write([dict(id='c', en='Sword', ru='Меч'), dict(id='d', en='Keep', ru='Хранить')])
        preview = merge_import(self.core, self.path, self.config)
        cancelled = Event()
        original_audit = self.core.audit
        def cancel_after_audit(*args):
            original_audit(*args)
            cancelled.set()
        with patch.object(self.core, 'audit', side_effect=cancel_after_audit):
            with self.assertRaises(Exception):
                merge_import(self.core, self.path, self.config, apply=True,
                             expected=preview['preview_hash'], cancelled=cancelled)
        self.assertIsNone(self.core.record(3)['target'])
        self.assertEqual(self.core.db.execute('SELECT count(*) FROM merge_imports').fetchone()[0], 0)

    def test_v6_migration_preserves_existing_reuse_provenance(self):
        work = self.home / 'legacy'
        work.mkdir()
        with closing(sqlite3.connect(work / 'translations.sqlite')) as db, db:
            for migration in MIGRATIONS[:6]:
                for statement in migration:
                    db.execute(statement)
            db.execute(f'PRAGMA application_id={APPLICATION_ID}')
            db.execute('PRAGMA user_version=6')
            db.execute("INSERT INTO reuse_imports VALUES ('hash','{\"author\":\"Original\"}')")
            db.execute("INSERT INTO reuse_candidates VALUES ('hash',0,0,'UNMATCHED','{}')")
        core = TranslationDB(work)
        try:
            self.assertEqual(core.db.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS))
            self.assertEqual(core.db.execute('SELECT provenance_json FROM reuse_imports').fetchone()[0], '{"author":"Original"}')
            self.assertEqual(core.db.execute('SELECT count(*) FROM reuse_candidates').fetchone()[0], 1)
        finally:
            core.close()

    def test_merge_during_provider_response_invalidates_snapshot(self):
        from wuwaru.engine import TranslationEngine
        from wuwaru.providers import MockProvider
        self.write([dict(id='c', en='Sword', ru='Меч')])
        merge = self.apply
        class Interleaved(MockProvider):
            def translate(self, items):
                result = super().translate(items)
                merge()
                return result
        with self.assertRaisesRegex(ValueError, 'Snapshot'):
            TranslationEngine(self.core, Interleaved()).run('merge-race')
        self.assertEqual(self.core.record(3)['target'], 'Меч')
        self.assertEqual(self.core.record(3)['status'], 'NEEDS_REVIEW')

    def test_structural_hostile_inputs_and_sqlite_view_refused(self):
        for rows in ([dict(id=True, en='Sword', ru='Меч')],
                     [dict(id='c', en='Sword', ru='Меч', resource='../game')],
                     [dict(id='c', en='Sword', ru='\ud800')]):
            with self.subTest(rows=repr(rows)):
                self.path.write_text(json.dumps(rows), encoding='utf-8')
                if 'resource' in rows[0]:
                    self.config['fields']['resource'] = 'resource'
                with self.assertRaises(ValueError):
                    self.apply()
        path = self.home / 'evil.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE VIEW pairs AS SELECT 'c' id,'Sword' en,'Меч' ru")
        with self.assertRaises(ValueError):
            merge_import(self.core, path, {**self.config, 'format': 'sqlite', 'table': 'pairs'})
        path.with_name(path.name + '-wal').write_bytes(b'synthetic')
        with self.assertRaises(ValueError):
            merge_import(self.core, path, {**self.config, 'format': 'sqlite', 'table': 'pairs'})

    def test_pending_conflicts_remain_across_sources_and_resolution_visible(self):
        from wuwaru.editor import Editor
        self.write([dict(id='c', en='Sword', ru='Меч'), dict(id='c', en='Sword', ru='Клинок')])
        self.apply()
        self.write([dict(id='c', en='Sword', ru='Меч')])
        self.assertEqual(self.apply()['conflict_rows'], 1)
        self.assertIsNone(self.core.record(3)['target'])
        editor = Editor(self.core)
        self.assertEqual(len(editor.page(merge_filter='CONFLICT')), 1)
        self.core.set_manual(3, 'Меч', approved=True)
        self.assertEqual(editor.merge_status(3), 'RESOLVED')
        self.assertEqual(editor.page(merge_filter='CONFLICT'), [])
        self.assertTrue(candidates(self.core, seq=3))

    def test_stale_or_invalid_targets_are_not_coverage(self):
        with self.core.db:
            self.core.db.execute("UPDATE records SET target='Меч',translated_hash=NULL WHERE seq=3")
        self.assertEqual(coverage(self.core)['filled_rows'], 0)
        self.assertEqual(coverage(self.core)['remaining_rows'], 5)

    def test_cli_roundtrip_and_candidate_pagination(self):
        import contextlib
        import io
        from wuwaru.__main__ import main
        self.write([dict(id='c', en='Sword', ru='Меч')])
        config = self.home / 'config.json'
        config.write_text(json.dumps(self.config), encoding='utf-8')
        base = ['translation', '--workspace', str(self.core.workspace)]
        merge = ['merge', '--source', str(self.path), '--config', str(config)]
        def run(args):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = main(base + args)
            self.assertEqual(code, 0, stream.getvalue())
            return json.loads(stream.getvalue())
        preview = run(merge)
        imported = run(merge + ['--apply', '--expected', preview['preview_hash']])
        listing = run(['merge-candidates', '--record', '3'])['candidates']
        self.assertEqual(listing[0]['import_id'], imported['import_id'])
        self.assertEqual(candidates(self.core, after=listing[0]['cursor']), [])
        self.assertEqual(run(['coverage'])['filled_rows'], 1)
        self.assertEqual(run(['merge-rollback', '--import-id', imported['import_id']])['restored_rows'], 1)
