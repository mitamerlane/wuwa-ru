"""Run explicitly with the optional GUI environment; never silently skipped."""
import os
import time
import json

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import Qt, QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidget, QPushButton, QDialog
from tests.test_discovery import FixtureCase
from tests.test_sqlite_source import database
from wuwaru.translation_db import TranslationDB
from wuwaru.gui import EditorWindow


class GuiTests(FixtureCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        source = database(self.home / 'source.db', ((1, 'Hello {Player}'), (2, 'Sword')))
        core = TranslationDB(self.home / 'work')
        core.import_source('r.db', source)
        core.close()
        self.window = EditorWindow(self.home / 'work')
        self.addCleanup(self.window.close)
        self.window.show()
        self.wait_idle()

    def wait_idle(self):
        self.pump(lambda: self.window.job is None)
        self.assertIsNone(self.window.job, 'GUI worker did not complete')
        self.assertFalse(self.window.last_error, self.window.last_error)

    def pump(self, predicate):
        loop, timer = QEventLoop(), QTimer()
        timer.timeout.connect(lambda: loop.quit() if predicate() else None)
        timer.start(10)
        QTimer.singleShot(15000, loop.quit)
        loop.exec()
        timer.stop()

    def select(self, row=0):
        self.window.table.selectRow(row)
        self.wait_idle()

    def test_known_corpus_button_reuses_project_without_file_dialogs(self):
        from hashlib import sha256
        from unittest.mock import patch
        from wuwaru.portable import PathManager
        from wuwaru.public_corpus import import_corpus
        paths = PathManager(self.home / 'portable')
        paths.initialize()
        root = paths.imports / 'Arikatsu'
        source = root / 'Textmaps/en/Test.json'
        source.parent.mkdir(parents=True)
        source.write_text('{"x":"Confirm"}', encoding='utf-8')
        lock = paths.imports / 'public-text-sources.json'
        lock.write_text(json.dumps(dict(schema_version=1, sources={'Arikatsu': dict(
            repository='https://github.com/example/data', revision='a'*40, data_version='3.7',
            data_date=None, files=[dict(path='Textmaps/en/Test.json', size=source.stat().st_size,
            sha256=sha256(source.read_bytes()).hexdigest())])})), encoding='utf-8')
        project = paths.projects / 'Imported'
        core = TranslationDB(project)
        try:
            import_corpus(core, lock, root, 'Arikatsu')
        finally:
            core.close()
        paths.save_public_source('Arikatsu', lock, root, project)
        self.window.path_manager = paths
        with patch('wuwaru.gui.QFileDialog.getOpenFileName', side_effect=AssertionError('Unexpected picker')):
            self.window.import_public()
            self.wait_idle()
        opened = self.window.next_window
        self.window = opened
        self.addCleanup(opened.close)
        self.wait_idle()
        self.assertEqual(opened.workspace, project)
        self.assertEqual(paths.last_project(), project)
        self.assertIn('1', opened.home_summary.text())

    def test_merge_preview_conflicts_review_and_rollback_workers(self):
        from wuwaru.public_text import PublicTextSourceAdapter
        from wuwaru.merge import candidates
        source = self.home / 'merge-en.json'
        source.write_text('{"c":"Sword"}', encoding='utf-8')
        core = TranslationDB(self.home / 'work')
        try:
            PublicTextSourceAdapter(core).import_file(source, resource='public/merge.json',
                repository='https://github.com/example/data', revision='a'*40, data_version='3.7',
                data_date=None, source_path='Textmaps/en/en.json')
        finally:
            core.close()
        path = self.home / 'merge-ru.json'
        path.write_text('[{"id":"c","en":"Sword","ru":"Меч"}]', encoding='utf-8')
        config = dict(schema_version=1, format='json', fields=dict(id='id',source='en',target='ru'),
            defaults=dict(version='3.7'), provenance=dict(name='Synthetic',version='r1',author='Fixture',
                url='https://example.org',rights_confirmed=True,rights_evidence='synthetic',usage='offline merge'))
        args = dict(path=path, config=config)
        results = []
        self.window.task('merge', args, results.append)
        self.wait_idle()
        self.assertEqual(results[0]['eligible_rows'], 1)
        def accept_preview():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QDialog)
            dialog.findChild(QPushButton, 'mergeApply').click()
        QTimer.singleShot(30, accept_preview)
        self.window.merge_preview(results[0], args)
        self.pump(lambda: self.window.job is None and hasattr(self.window, 'last_import_id'))
        self.wait_idle()
        imported = self.window.last_import_id
        self.window.task('merge-rollback', dict(import_id=imported))
        self.wait_idle()
        self.window.task('merge', args, results.append)
        self.wait_idle()
        self.window.task('merge', dict(**args, apply=True, expected=results[-1]['preview_hash']), results.append)
        self.wait_idle()
        path.write_text('[{"id":"c","en":"Sword","ru":"Клинок"}]', encoding='utf-8')
        self.window.task('merge', args, results.append)
        self.wait_idle()
        self.window.task('merge', dict(**args, apply=True, expected=results[-1]['preview_hash']), results.append)
        self.wait_idle()
        self.window.merge_filter.setCurrentIndex(1)
        self.window.reload()
        self.wait_idle()
        self.assertEqual(self.window.model.rowCount(), 1)
        self.select()
        self.assertEqual(self.window.current['row']['merge_status'], 'CONFLICT')
        self.assertIn('Fixture', self.window.provenance.toPlainText())
        core = TranslationDB(self.home / 'work')
        try:
            rows = candidates(core, seq=3)
        finally:
            core.close()
        def choose_candidate():
            dialog = QApplication.activeModalWidget()
            dialog.findChild(QTableWidget).setCurrentCell(len(rows)-1, 0)
            dialog.findChild(QPushButton, 'mergeUseCandidate').click()
        QTimer.singleShot(30, choose_candidate)
        self.window.candidates_dialog(rows)
        self.assertEqual(self.window.target.toPlainText(), 'Клинок')
        self.window.save(True)
        self.wait_idle()
        self.assertEqual(self.window.current['row']['status'], 'APPROVED')
        self.assertEqual(self.window.current['row']['merge_status'], 'RESOLVED')

    def test_select_edit_keyboard_save_approve_and_reopen(self):
        self.select()
        self.window.target.setPlainText('Привет {Player}')
        self.window.target.setFocus()
        QTest.keyClick(self.window.target, Qt.Key_S, Qt.ControlModifier)
        self.wait_idle()
        self.window.save(True)
        self.wait_idle()
        core = TranslationDB(self.home / 'work')
        try:
            self.assertEqual(core.record(1)['target'], 'Привет {Player}')
            self.assertEqual(core.record(1)['status'], 'APPROVED')
        finally:
            core.close()

    def test_public_source_filter_provenance_edit_qa_history_reopen(self):
        from wuwaru.public_text import PublicTextSourceAdapter
        source = self.home / 'textmap.json'
        source.write_text('[{"Id":"Quest_1","Content":"Hello {Player}"}]', encoding='utf-8')
        core = TranslationDB(self.home / 'work')
        try:
            PublicTextSourceAdapter(core).import_file(source, resource='public/test.json',
                repository='https://github.com/example/data', revision='a'*40,
                data_version='3.7', data_date=None, source_path='Textmaps/en/Test.json')
        finally:
            core.close()
        self.window.source_type.setCurrentText('PUBLIC_TEXTMAP')
        self.window.reload()
        self.wait_idle()
        self.assertEqual(self.window.model.rowCount(), 1)
        self.select()
        self.assertIn('PUBLIC_TEXTMAP', self.window.provenance.toPlainText())
        self.assertIn('3.7', self.window.provenance.toPlainText())
        self.assertIn('sha256', self.window.provenance.toPlainText())
        self.window.target.setPlainText('Привет {Player}')
        self.window.save(True)
        self.wait_idle()
        self.assertTrue(self.window.validate_current())
        self.assertEqual(self.window.current['row']['status'], 'APPROVED')
        self.assertGreaterEqual(len(self.window.current['history']), 2)
        core = TranslationDB(self.home / 'work')
        try:
            self.assertEqual(core.record(3)['target'], 'Привет {Player}')
        finally:
            core.close()

    def test_search_empty_and_recovery_after_worker_error(self):
        self.window.search.setText('missing')
        self.window.reload()
        self.wait_idle()
        self.assertEqual(self.window.model.rowCount(), 0)
        self.window.search.clear()
        self.window.reload()
        self.wait_idle()
        self.assertEqual(self.window.model.rowCount(), 2)
        self.assertLessEqual(len(self.window.model.rows), 200)
        self.window.task('statuses', dict(versions={1: 'stale'}, status='APPROVED'))
        self.pump(lambda: self.window.job is None)
        self.assertIn('Conflict', self.window.last_error)
        self.window.reload()
        self.wait_idle()

    def test_render_unicode_and_context_save(self):
        self.select()
        self.window.target.setPlainText('Привет {Player}')
        self.window.context.setText('Тестовый персонаж / приветствие')
        self.window.save(True)
        self.wait_idle()
        self.assertIn('приветствие', self.window.context.text())
        self.window.navigate(4)
        self.assertIs(self.window.stack.currentWidget(), self.window.body)
        screenshot = self.home.parent / 'm3-editor.png'
        self.assertTrue(self.window.grab().save(str(screenshot)))
        self.assertGreater(screenshot.stat().st_size, 10000)

    def test_gui_batch_status_and_history_restore(self):
        from unittest.mock import patch
        self.select()
        self.window.target.setPlainText('Привет {Player}')
        self.window.save()
        self.wait_idle()
        self.window.target.setPlainText('Здравствуй {Player}')
        self.window.save()
        self.wait_idle()
        def restore_previous():
            dialog = self.app.activeModalWidget()
            dialog.findChild(QTableWidget).selectRow(1)
            next(b for b in dialog.findChildren(QPushButton) if b.text().startswith('Восстановить')).click()
        QTimer.singleShot(30, restore_previous)
        self.window.show_history()
        self.wait_idle()
        self.assertEqual(self.window.target.toPlainText(), 'Привет {Player}')
        self.select(1)
        self.window.target.setPlainText('Меч')
        self.window.save()
        self.wait_idle()
        self.window.table.selectAll()
        with patch('wuwaru.gui.QInputDialog.getItem', return_value=('APPROVED', True)):
            self.window.batch_status()
        self.wait_idle()
        self.assertEqual({r['status'] for r in self.window.model.rows}, {'APPROVED'})

    def test_close_during_work_requests_cancel_without_thread_destruction(self):
        self.window.task('report', {'name': 'output/gui-report'})
        self.window.close()
        self.pump(lambda: self.window.job is None)
        self.assertIsNone(self.window.job)


if __name__ == '__main__':
    import unittest
    unittest.main()
