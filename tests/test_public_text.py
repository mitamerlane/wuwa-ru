import json
from threading import Event

from tests.test_discovery import FixtureCase
from wuwaru.translation_db import TranslationDB
from wuwaru.public_text import PublicTextSourceAdapter, iter_textmap
from wuwaru.editor import Editor
from wuwaru.qa import QualityAssurance
from wuwaru.export import export_databases
from wuwaru.public_corpus import corpus_statistics, estimate_cost, pilot_rows, import_corpus


class PublicTextTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.source = self.home / 'MultiText.json'
        self.core = TranslationDB(self.home / 'work')
        self.addCleanup(self.core.close)
        self.adapter = PublicTextSourceAdapter(self.core)

    def write(self, value):
        self.source.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def load(self):
        return self.adapter.import_file(self.source, resource='public/MultiText.json',
            repository='https://github.com/example/data', revision='a' * 40,
            data_version='3.7', data_date='2026-09-29T20:35:07Z',
            source_path='Textmaps/en/multi_text/MultiText.json')

    def test_import_edit_reimport_history_qa_and_export_guard(self):
        self.write([{'Id': 'Quest_1_QuestDesc', 'Content': 'Hello {Player}', 'RedirectDbIndex': 0},
                    {'Id': 1, 'Content': ''}, {'Id': '1', 'Content': 'Sword'}])
        self.assertEqual(self.load()['new'], 3)
        row = self.core.record(1)
        self.assertEqual(row['source_type'], 'PUBLIC_TEXTMAP')
        self.assertEqual(row['category'], 'quest')
        self.assertEqual(json.loads(row['source_metadata'])['RedirectDbIndex'], 0)
        editor = Editor(self.core)
        editor.save(1, 'Привет {Player}', editor.version(1))
        self.assertEqual(self.load()['unchanged'], 3)
        self.assertEqual(self.core.record(1)['target'], 'Привет {Player}')
        self.write([{'Id': 'Quest_1_QuestDesc', 'Content': 'Goodbye {Player}', 'RedirectDbIndex': 1}])
        self.assertEqual(self.load(), dict(new=0, changed=1, unchanged=0, removed=2))
        self.assertEqual(self.core.record(1)['status'], 'NEEDS_REVIEW')
        self.assertEqual(self.core.record(1)['target'], 'Привет {Player}')
        self.assertGreaterEqual(len(editor.history(1)), 3)
        self.assertEqual(self.core.db.execute('SELECT count(*) FROM source_snapshots').fetchone()[0], 2)
        self.assertFalse(any(i.code in ('ID', 'CONTEXT_RESOURCE') for i in QualityAssurance(self.core).issues(self.core.record(1))))
        with self.assertRaisesRegex(ValueError, 'PUBLIC_TEXTMAP'):
            export_databases(self.core, 'output/native')
        self.assertEqual(len(editor.page(source_type='PUBLIC_TEXTMAP')), 1)
        self.assertEqual(editor.page(source_type='SQLITE_STAGING'), [])

    def test_stream_both_shapes_tiny_chunks(self):
        for value in ([{'Id': 'é😀', 'Content': 'Hi\\"\n'}], {'é😀': 'Hi\\"\n'}):
            self.write(value)
            rows = list(iter_textmap(self.source, chunk_size=1))
            self.assertEqual(rows[0]['Id'], 'é😀')
            self.assertEqual(rows[0]['Content'], 'Hi\\"\n')

    def test_malformed_duplicate_and_cancel_are_atomic(self):
        for data in ('[{"Id":1,"Content":"A"},{"Id":1,"Content":"B"}]',
                     '{"x":"A","x":"B"}', '[{"Id":1,"Id":2,"Content":"A"}]',
                     '[{"Id":true,"Content":"A"}]', '[{"Id":1,"Content":3}]',
                     '[{"Id":1,"Content":"A"},]', '{"x":"A",}', '[] garbage',
                     '[{"Id":1,"Content":"A"}] {}', '[', '{"x":NaN}'):
            with self.subTest(data=data):
                self.source.write_text(data, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.load()
                self.assertEqual(self.core.db.execute('SELECT count(*) FROM records').fetchone()[0], 0)
        self.write({'x': 'A'})
        cancelled = Event(); cancelled.set()
        with self.assertRaises(Exception):
            list(iter_textmap(self.source, cancelled=cancelled))

    def test_context_provenance_and_empty_snapshot(self):
        self.write({'x': 'A'})
        self.load()
        self.core.set_context(1, 'Human context')
        self.write({'x': 'B'})
        self.load()
        self.assertEqual(self.core.record(1)['context'], 'Human context')
        self.assertIn('data_version', self.core.db.execute('SELECT inspection FROM resources').fetchone()[0])
        self.write({})
        self.assertEqual(self.load()['removed'], 1)

    def test_null_content_is_counted_as_absence_and_duplicates_still_rejected(self):
        self.write([{'Id': 1, 'Content': None}, {'Id': 2, 'Content': ''}])
        self.assertEqual(self.load()['new'], 1)
        self.assertEqual(corpus_statistics(self.core)['records'], 1)
        self.assertEqual(corpus_statistics(self.core)['nonempty'], 0)
        self.write([{'Id': 1, 'Content': None}, {'Id': 1, 'Content': 'A'}])
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(self.core.record(1)['source'], '')

    def test_cost_and_deterministic_diverse_pilot_without_network(self):
        self.write({str(i): 'Hello {Player} ' + str(i) for i in range(120)})
        self.load()
        cost = estimate_cost(self.core, glossary_tokens=0, context_tokens=0)
        more = estimate_cost(self.core, glossary_tokens=1500, context_tokens=100)
        self.assertEqual(cost['records'], 120)
        self.assertEqual(cost['requests'], 2)
        self.assertEqual(cost['paid_api_calls'], 0)
        self.assertGreater(more['expected']['standard_usd'], cost['expected']['standard_usd'])
        raw = (cost['input_tokens_estimate'] * .1 + cost['output_tokens_estimate'] * .5) / 1e6
        self.assertAlmostEqual(cost['expected']['standard_usd'], raw, places=3)
        self.assertAlmostEqual(cost['expected']['batch_usd'], raw * .5, places=3)
        self.assertEqual(pilot_rows(self.core, 100), pilot_rows(self.core, 100))
        self.assertEqual(len(pilot_rows(self.core, 100)), 100)
        for options in (dict(reserve=float('nan')), dict(batch_size=0), dict(output_ratio=-1)):
            with self.assertRaises(ValueError):
                estimate_cost(self.core, **options)

    def test_pin_preflight_and_path_guards(self):
        from hashlib import sha256
        path = self.home / 'Textmaps/en/Test.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"x":"Hello"}', encoding='utf-8')
        source = dict(repository='https://github.com/example/data', revision='a'*40,
                      data_version='UNKNOWN', data_date=None,
                      files=[dict(path='Textmaps/en/Test.json', size=path.stat().st_size,
                                  sha256=sha256(path.read_bytes()).hexdigest())])
        lock = self.home / 'lock.json'
        def save_lock():
            lock.write_text(json.dumps(dict(schema_version=1, sources={'test':source})), encoding='utf-8')
        save_lock()
        self.assertEqual(import_corpus(self.core, lock, self.home, 'test')['new'], 1)
        source['files'][0]['sha256'] = '0'*64
        save_lock()
        with self.assertRaises(ValueError):
            import_corpus(self.core, lock, self.home, 'test')
        source['files'][0]['path'] = '../Test.json'
        save_lock()
        with self.assertRaises(ValueError):
            import_corpus(self.core, lock, self.home, 'test')
        self.assertEqual(self.core.record(1)['source'], 'Hello')

    def test_untrusted_lock_shapes_fail_with_validation_error(self):
        from wuwaru.public_corpus import read_lock
        lock = self.home / 'bad-lock.json'
        for value in ([], {}, {'schema_version':1, 'sources':{}},
                      {'schema_version':1, 'sources':{'test':None}}):
            lock.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError):
                read_lock(lock, 'test')
