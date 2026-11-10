import json
import sqlite3
from threading import Event

from tests.test_discovery import FixtureCase
from tests.test_sqlite_source import database
from wuwaru.translation_db import TranslationDB
from wuwaru.editor import Editor, Conflict
from wuwaru.qa import QualityAssurance
from wuwaru.validation import TranslationValidator
from wuwaru.glossary import Glossary
from wuwaru.export import export_databases
from wuwaru.manifest import Cancelled


class EditorTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.source = database(self.home / 'source.db', ((1, 'Hello {Player}'), (2, 'Sword'), (3, 'Sword')))
        self.core = TranslationDB(self.home / 'work')
        self.addCleanup(self.core.close)
        self.core.import_source('r.db', self.source)
        self.editor = Editor(self.core)

    def save(self, seq, text, status='REVIEWED'):
        return self.editor.save(seq, text, self.editor.version(seq), status=status)

    def test_edit_rollback_and_stale_editor_conflict(self):
        initial = self.editor.version(1)
        self.save(1, 'Привет {Player}', 'APPROVED')
        revision = self.editor.history(1)[0]['revision']
        with self.assertRaises(Conflict):
            self.editor.save(1, 'Ой {Player}', initial)
        self.save(1, 'Здравствуй {Player}')
        self.editor.restore(1, revision, self.editor.version(1))
        row = self.core.record(1)
        self.assertEqual((row['target'], row['manual'], row['status']), ('Привет {Player}', 1, 'REVIEWED'))
        self.assertEqual(self.editor.history(1)[0]['event'], 'RESTORE')

    def test_structural_errors_block_save_but_warnings_allow_approval(self):
        with self.assertRaises(ValueError):
            self.save(1, 'Привет')
        self.save(2, 'Sword', 'APPROVED')
        self.assertEqual(self.core.record(2)['status'], 'APPROVED')
        self.assertTrue(any(i.severity == 'WARNING' for i in self.editor.issues(2)))

    def test_qa_report_counts_untranslated_rows_separately_from_structural_errors(self):
        from wuwaru.qa import output_path
        result = QualityAssurance(self.core).report('output/qa-untranslated')
        self.assertEqual(result['untranslated_rows'], 3)
        self.assertEqual(result['error_rows'], 0)
        self.assertEqual(result['blocked_rows'], 3)

    def test_invalid_target_type_is_structural_not_untranslated(self):
        with self.core.db:
            self.core.db.execute('UPDATE records SET target=? WHERE seq=1', (b'not text',))
        issues = QualityAssurance(self.core).issues(self.core.record(1))
        self.assertNotIn('UNTRANSLATED', {issue.code for issue in issues})
        result = QualityAssurance(self.core).report('output/qa-invalid-target')
        self.assertEqual(result['error_rows'], 1)
        self.assertEqual(result['untranslated_rows'], 2)

    def test_unicode_search_filters_paging_and_literal_wildcards(self):
        self.save(2, 'МЕЧ')
        self.editor.categorize(2, 'item', self.editor.version(2))
        self.assertEqual([r['seq'] for r in self.editor.page(query='меч', category='item', status='REVIEWED', resource='r.db')], [2])
        self.assertEqual(len(self.editor.page(limit=1)), 1)
        self.assertEqual(self.editor.page(after=1, limit=1)[0]['seq'], 2)
        self.assertEqual(self.editor.page(query='%'), [])
        self.assertEqual(self.editor.page(query='1')[0]['id_value'], '1')

    def test_batch_status_atomic_conflict_and_history(self):
        self.save(2, 'Меч')
        self.save(3, 'Клинок')
        versions = {s: self.editor.version(s) for s in (2, 3)}
        self.save(3, 'Меч')
        with self.assertRaises(Conflict):
            self.editor.statuses(versions, 'APPROVED')
        self.assertEqual(self.core.record(2)['status'], 'REVIEWED')
        self.editor.statuses({s: self.editor.version(s) for s in (2, 3)}, 'APPROVED')
        self.assertEqual(self.core.record(2)['status'], 'APPROVED')
        self.assertEqual(self.editor.history(2)[0]['event'], 'STATUS')

    def test_reimport_keeps_manual_and_restore_requires_current_source_review(self):
        self.save(2, 'Меч', 'APPROVED')
        revision = self.editor.history(2)[0]['revision']
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE Text SET Content='Great sword' WHERE Id=2")
        db.close()
        self.core.import_source('r.db', self.source)
        self.assertEqual(self.core.record(2)['target'], 'Меч')
        self.editor.restore(2, revision, self.editor.version(2))
        self.assertEqual(self.core.record(2)['status'], 'NEEDS_REVIEW')

    def test_glossary_context_exception_proper_name_and_variants(self):
        self.save(2, 'Меч')
        self.save(3, 'Клинок')
        glossary = Glossary(self.core)
        glossary.add('Sword', 'Меч', category='item', verified=True)
        self.assertIn('GLOSSARY', [i.code for i in self.editor.issues(3)])
        self.core.set_context(3, 'weapon/poetry')
        glossary.add('Sword', 'Клинок', category='item', context='weapon/poetry', verified=True)
        self.assertNotIn('GLOSSARY', [i.code for i in self.editor.issues(3)])
        self.assertEqual(len(self.editor.page(term='Sword')), 2)
        self.assertEqual(len(self.editor.variants('Sword')), 2)
        glossary.add('Sword', 'Sword', category='proper_name', context='name', verified=True)
        self.core.set_context(2, 'name')
        self.save(2, 'Sword')
        self.assertIn('INFO', [i.severity for i in self.editor.issues(2)])

    def test_context_links_reject_missing_and_qa_detects_corruption(self):
        with self.assertRaises(ValueError):
            self.editor.link_context(1, 999, self.editor.version(1))
        self.editor.link_context(1, 2, self.editor.version(1))
        with self.core.db:
            self.core.db.execute("UPDATE records SET id_value='changed',source_hash='bad' WHERE seq=1")
            self.core.db.execute('DELETE FROM records WHERE seq=2')
        codes = {i.code for i in self.editor.issues(1)}
        self.assertTrue({'ID', 'SOURCE_HASH', 'CONTEXT_REFERENCE'} <= codes)

    def test_cancel_and_report_counts_categories_and_no_false_quality_score(self):
        self.save(2, 'Меч', 'APPROVED')
        self.editor.categorize(2, 'item', self.editor.version(2))
        qa = QualityAssurance(self.core)
        event = Event()
        event.set()
        with self.assertRaises(Cancelled):
            qa.report('output/cancelled', cancelled=event)
        self.assertFalse((self.core.workspace / 'output/cancelled').exists())
        result = qa.report('output/qa')
        self.assertEqual((result['total'], result['translated'], result['approved'], result['unchecked']), (3, 1, 1, 2))
        self.assertEqual(result['untranslated_rows'], 2)
        self.assertEqual(result['categories']['item']['coverage_percent'], 100)
        self.assertEqual(result['blocked_rows'], 2)
        folder = self.core.workspace / 'output/qa'
        self.assertEqual(json.loads((folder / 'qa.json').read_text('utf-8'))['total'], 3)
        self.assertIn('severity', (folder / 'issues.csv').read_text('utf-8-sig'))
        self.assertEqual(self.editor.page(severity='ERROR'), [])
        self.assertTrue(self.editor.page(severity='INFO'))

    def test_export_blocks_errors_then_preserves_source_and_includes_qa(self):
        original = self.source.read_bytes()
        with self.assertRaises(ValueError):
            export_databases(self.core, 'output/bad')
        self.assertFalse((self.core.workspace / 'output/bad').exists())
        self.save(1, 'Привет {Player}')
        self.save(2, 'Sword', 'APPROVED')  # heuristic warnings must not block
        self.save(3, 'Меч')
        export_databases(self.core, 'output/good')
        self.assertTrue((self.core.workspace / 'output/good/qa.json').exists())
        self.assertEqual(self.source.read_bytes(), original)

    def test_validator_severity_tokens_unicode_newlines_and_english(self):
        validator = TranslationValidator()
        issues = validator.check('Hello {P}\n<b>%d</b>', 'Hi {Q}<b>%s</b>\ufffd')
        self.assertTrue({'TOKENS', 'UNICODE'} <= {i.code for i in issues if i.severity == 'ERROR'})
        self.assertTrue(any(i.severity == 'WARNING' for i in issues))
        self.assertEqual(validator.check('Hello', 'Привет'), [])

    def test_atomic_context_and_target_save_and_conflict_between_connections(self):
        other = TranslationDB(self.core.workspace)
        try:
            other_editor = Editor(other)
            token = other_editor.version(1)
            self.editor.save(1, 'Привет {Player}', self.editor.version(1), context='speaker/hero', status='APPROVED')
            with self.assertRaises(Conflict):
                other_editor.save(1, 'Здравствуй {Player}', token)
            self.assertEqual(other.record(1)['context'], 'speaker/hero')
        finally:
            other.close()

    def test_cancel_mid_batch_and_mid_report_roll_back_and_recover(self):
        from unittest.mock import patch
        self.save(2, 'Меч')
        self.save(3, 'Клинок')
        original = self.editor.version(2)
        with patch('wuwaru.editor.check_cancel', side_effect=[None, Cancelled('cancel')]):
            with self.assertRaises(Cancelled):
                self.editor.statuses({s: self.editor.version(s) for s in (2, 3)}, 'APPROVED')
        self.assertEqual(self.editor.version(2), original)
        qa = QualityAssurance(self.core)
        with patch.object(qa, 'issues', side_effect=Cancelled('cancel')):
            with self.assertRaises(Cancelled):
                qa.report('output/interrupted')
        self.assertFalse((self.core.workspace / 'output/interrupted').exists())
        self.assertEqual(QualityAssurance(self.core).report('output/recovered')['total'], 3)

    def test_output_guards_and_no_overwrite(self):
        qa = QualityAssurance(self.core)
        for name in ('../output/escape', 'elsewhere/qa', 'output', 'C:/output/qa', 'output/ads:bad'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                qa.report(name)
        qa.report('output/one')
        with self.assertRaises(FileExistsError):
            qa.report('output/one')

    def test_corrupt_types_source_and_context_resource_report_errors(self):
        with self.core.db:
            self.core.db.execute("UPDATE records SET source=x'00',target=x'01' WHERE seq=1")
            self.core.db.execute("UPDATE records SET context='',table_name='missing' WHERE seq=2")
        self.assertEqual(self.editor.issues(1)[0].code, 'RECORD_TYPE')
        codes = {i.code for i in self.editor.issues(2)}
        self.assertTrue({'ID', 'CONTEXT', 'CONTEXT_RESOURCE'} <= codes)
        report = QualityAssurance(self.core).report('output/corrupt')
        self.assertEqual((report['error_rows'], report['untranslated_rows']), (2, 2))

    def test_export_warning_glossary_and_repeated_terms_are_nonblocking(self):
        self.save(1, 'Привет {Player}')
        self.save(2, 'Меч')
        self.save(3, 'Клинок')
        Glossary(self.core).add('Sword', 'Меч', category='item', verified=True)
        result = export_databases(self.core, 'output/warnings')
        self.assertEqual(result['qa']['glossary_violations'], 1)
        self.assertGreater(result['qa']['WARNING'], 0)
        self.assertEqual(result['qa']['blocked_rows'], 0)

    def test_history_pagination_has_no_loss(self):
        for index in range(8):
            self.save(2, f'Меч {index}')
        recent = self.editor.history(2, limit=4)
        older = self.editor.history(2, before=recent[-1]['revision'], limit=4)
        self.assertEqual(len({r['revision'] for r in recent + older}), 8)

    def test_csv_ids_do_not_execute_formulas_and_corrupt_category_is_reported(self):
        from wuwaru.qa import csv_cell
        self.assertEqual(csv_cell('=1+2'), "'=1+2")
        with self.core.db:
            self.core.db.execute("UPDATE records SET category=x'00' WHERE seq=1")
        summary = QualityAssurance(self.core).report('output/bad-category')
        self.assertEqual(summary['categories']['CORRUPT_CATEGORY']['total'], 1)

    def test_report_snapshot_during_other_connection_edit(self):
        more = database(self.home / 'more.db', ((i, 'Sword') for i in range(210)))
        self.core.import_source('more.db', more)
        other = TranslationDB(self.core.workspace)
        changed = False
        def progress(_):
            nonlocal changed
            if not changed:
                other.set_manual(2, 'Меч', approved=True)
                changed = True
        try:
            result = QualityAssurance(self.core).report('output/snapshot', progress=progress)
            self.assertTrue(changed)
            self.assertEqual(result['translated'], 0)
            self.assertEqual(self.core.record(2)['target'], 'Меч')
        finally:
            other.close()

    def test_page_and_history_byte_budget_preserve_cursor(self):
        from unittest.mock import patch
        self.save(2, 'Меч')
        self.save(2, 'Клинок')
        with patch('wuwaru.editor.PAGE_BYTES', 1):
            first = self.editor.page()
            self.assertEqual(len(first), 1)
            self.assertEqual(self.editor.page(after=first[0]['seq'])[0]['seq'], 2)
            self.assertEqual(len(self.editor.history(2)), 1)
            self.assertEqual(len(self.editor.variants('Sword')), 1)

    def test_corrupt_binary_identity_still_produces_json_report(self):
        with self.core.db:
            self.core.db.execute("UPDATE records SET resource=x'01',id_value=x'02',id_type='text' WHERE seq=1")
        result = QualityAssurance(self.core).report('output/binary-identity')
        self.assertEqual((result['error_rows'], result['untranslated_rows']), (1, 3))
        issues = json.loads((self.core.workspace / 'output/binary-identity/issues.json').read_text('utf-8'))
        self.assertIsNone(issues[0]['id'])
        self.assertEqual(issues[0]['code'], 'RECORD_TYPE')
