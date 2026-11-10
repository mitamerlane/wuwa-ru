import os
import tempfile
import unittest
from pathlib import Path
import json
import shutil
import hashlib
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QInputDialog

from wuwaru.gui import EditorWindow
from wuwaru.gui import ProjectChooser
from wuwaru.portable import PathManager
from wuwaru.portable import GameDiscoveryService
from wuwaru.product import project_overview, seed_mock_demo
from wuwaru.translation_db import TranslationDB


class ProductDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # The Play page inspects the found game at start; keep these windows away from the real system.
        for item in (patch.object(GameDiscoveryService, 'discover', return_value={'installations': [], 'warnings': []}),
                     patch('wuwaru.play_page.find_hero', return_value=None)):
            item.start()
            self.addCleanup(item.stop)
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.test-tmp')
        self.root = Path(self.temp.name)
        core = TranslationDB(self.root / 'project')
        source = self.root / 'source.db'
        import sqlite3
        db = sqlite3.connect(source)
        try:
            db.execute('CREATE TABLE TextMap (Id INTEGER PRIMARY KEY, Content TEXT)')
            db.execute('INSERT INTO TextMap VALUES (1, "Hello, Rover!")')
            db.execute('INSERT INTO TextMap VALUES (2, "Welcome back")')
            db.commit()
        finally:
            db.close()
        core.import_source('synthetic.db', source)
        core.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_project_overview_counts_and_public_provenance_are_explicit(self):
        core = TranslationDB(self.root / 'project')
        textmap = self.root / 'public.json'
        textmap.write_text(json.dumps([{'Id': 'test-en', 'Content': 'Confirm'}]), encoding='utf-8')
        from wuwaru.public_text import PublicTextSourceAdapter
        PublicTextSourceAdapter(core).import_file(
            textmap, resource='public/example/Textmaps/en/test.json',
            repository='https://github.com/example/data', revision='a' * 40,
            data_version='game 3.7.0; resource 3.7.8', data_date=None,
            source_path='Textmaps/en/test.json')
        overview = project_overview(core)
        core.close()
        self.assertEqual(overview['records'], 3)
        self.assertEqual(overview['translated'], 0)
        self.assertEqual(overview['coverage_percent'], 0)
        self.assertEqual(overview['source_counts']['SQLITE_STAGING'], 2)
        self.assertEqual(overview['source_counts']['PUBLIC_TEXTMAP'], 1)
        self.assertEqual(overview['public_sources'][0]['data_version'], 'game 3.7.0; resource 3.7.8')
        self.assertFalse(overview['installation_allowed'])

    def test_mock_demo_is_separate_and_resumable(self):
        overview = seed_mock_demo(self.root / 'project')
        self.assertEqual(overview['source_type'], 'SYNTHETIC_FIXTURE')
        self.assertEqual(overview['records'], 3)
        core = TranslationDB(self.root / 'project' / '.mock-demo')
        self.assertEqual(core.db.execute('SELECT count(*) FROM records WHERE active=1').fetchone()[0], 3)
        self.assertEqual(core.db.execute('SELECT count(*) FROM records WHERE source_type="SYNTHETIC_FIXTURE"').fetchone()[0], 3)
        core.close()

    def test_main_window_has_product_navigation_and_native_blockers(self):
        window = EditorWindow(self.root / 'project')
        self.addCleanup(window.close)
        self.wait_idle(window)
        self.assertEqual(window.navigation_labels, [
            'Главная', 'Игровые ресурсы', 'База переводов', 'Автоматический перевод',
            'Редактор', 'Проверка качества', 'Экспорт', 'Настройки', 'Нативная совместимость',
            'Файлы проекта', 'Играть'])
        self.assertIn('Реальные языковые SQLite — недоступны', window.home_status.text())
        self.assertIn('Применение перед запуском проверено опытами REAPPLY и WAVES', window.home_status.text())
        self.assertNotIn('заблокирована', window.home_status.text())
        self.assertIn('допуск ACE не подтверждены', window.compatibility_status.text())
        self.assertNotIn('Причина блокировки', window.compatibility_status.text())

    def test_project_chooser_lists_verified_existing_workspace(self):
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        with patch('wuwaru.gui.QSettings.value', return_value=''):
            chooser = ProjectChooser(paths, known_roots=[self.root])
        self.addCleanup(chooser.close)
        self.assertEqual(len(chooser.candidates), 1)
        self.assertEqual(chooser.candidates[0].path, (self.root / 'project').resolve())
        chooser.projects.setCurrentRow(0)
        chooser.open_selected()
        self.assertEqual(chooser.workspace, (self.root / 'project').resolve())

    def test_game_discovery_result_is_rendered_without_claiming_reader_or_client_version(self):
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        with patch.object(GameDiscoveryService, 'discover', return_value={
                'installations': [{'path': 'C:/Games/Wuthering Waves', 'origins': ['steam_manifest'],
                                   'client_version': None, 'steam_build_id': '123',
                                   'reader_state': 'NOT_CHECKED'}], 'warnings': []}):
            window = EditorWindow(self.root / 'project', paths)
            self.addCleanup(window.close)
            self.wait_idle(window)
        self.assertIn('UNKNOWN', window.game_status.text())
        self.assertIn('Reader: NOT_CHECKED', window.game_status.text())

    def wait_idle(self, window):
        loop = QEventLoop()
        timer = QTimer()
        timer.timeout.connect(lambda: loop.quit() if window.job is None else None)
        timer.start(10)
        QTimer.singleShot(10000, loop.quit)
        loop.exec()
        timer.stop()
        self.assertIsNone(window.job)
        self.assertFalse(window.last_error, window.last_error)

    def test_gui_luna_preview_confirmation_and_transport_without_network(self):
        import io
        from PySide6.QtWidgets import QMessageBox
        from wuwaru.validation import TOKEN
        from wuwaru.provider_config import WindowsCredentials
        vault = patch.object(WindowsCredentials, 'get', return_value=None)
        vault.start()
        self.addCleanup(vault.stop)
        class Transport:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                body = json.loads(request.data)
                values = json.loads(body['messages'][1]['content'])
                result = {'translations': [dict(key=r['key'], target='Проверка ' + ''.join(TOKEN.findall(r['source']))) for r in values]}
                return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps(result)}}]}).encode())
        transport = Transport()
        window = EditorWindow(self.root / 'project')
        self.addCleanup(window.close)
        self.wait_idle(window)
        self.assertFalse(window.pilot_run_button.isEnabled())
        window.api_provider.setCurrentText('OpenAI')
        window.api_input_price.setText('0.10')
        window.api_output_price.setText('0.50')
        window.pilot_rows.setValue(200)
        window.pilot_requests.setValue(24)
        window.pilot_tokens.setValue(8192)
        with patch('socket.socket', side_effect=AssertionError('external network')):
            window.pilot_preview_button.click()
            self.wait_idle(window)
            self.assertEqual(window.pilot_report['records'], 200)
            self.assertEqual(window.pilot_report['preview']['requests'], 8)
            self.assertTrue(window.pilot_run_button.isEnabled())
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
                window.pilot_run_button.click()
            self.assertIsNone(window.job)
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes), \
                    patch.dict(os.environ, {'WUWARU_API_KEY': 'synthetic-test-only'}), \
                    patch('urllib.request.build_opener', return_value=transport):
                window.pilot_run_button.click()
                self.wait_idle(window)
            self.assertEqual(transport.calls, 8)
            self.assertEqual(window.api_results.rowCount(), 200)
            self.assertEqual(window.mock_results.rowCount(), 0)
            self.assertIn('Русский ответ', window.api_results.horizontalHeaderItem(1).text())
            self.assertIn('Полный ответ модели', window.api_detail.toPlainText())
            self.assertIn('UNKNOWN', window.api_result_summary.text())
            self.assertIn('имитация', window.translation_tabs.tabText(2))
            window.api_results.setCurrentCell(0, 0)
            self.assertTrue(window.api_approve_button.isEnabled())
            window.api_approve_button.click()
            self.wait_idle(window)
            self.assertIn('APPROVED', window.api_results.item(0, 3).text())
            self.assertFalse(window.api_approve_button.isEnabled())
            window.load_pilot_results()
            self.wait_idle(window)
            self.assertEqual(window.api_results.rowCount(), 200)
            self.assertEqual(transport.calls, 8)
            self.assertEqual(window.pilot_report['preview']['requests'], 0)
            self.assertFalse(window.pilot_run_button.isEnabled())
        core = TranslationDB(self.root / 'project')
        try:
            self.assertEqual(core.db.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
            self.assertIsNone(core.record(1)['target'])
        finally:
            core.close()

    def test_relay_unknown_price_confirmation_warning_and_changed_limits(self):
        from PySide6.QtWidgets import QMessageBox
        from wuwaru.providers import OpenAICompatibleProvider
        from wuwaru.provider_config import WindowsCredentials
        with patch.object(WindowsCredentials, 'get', return_value=None), \
                patch('socket.socket', side_effect=AssertionError('external network')):
            window = EditorWindow(self.root / 'project')
            self.addCleanup(window.close)
            self.wait_idle(window)
            window.api_provider.setCurrentText('RelayModels')
            self.assertEqual(window.pilot_rows.value(), 5)
            self.assertEqual(window.pilot_requests.value(), 1)
            self.assertEqual(window.pilot_tokens.value(), 1024)
            self.assertFalse(window.api_input_price.isEnabled())
            self.assertFalse(window.api_output_price.isEnabled())
            window.pilot_preview_button.click()
            self.wait_idle(window)
            self.assertEqual(window.pilot_report['records'], 5)
            self.assertIn('Стоимость неизвестна', window.pilot_status.text())
            self.assertTrue(window.pilot_run_button.isEnabled())
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.No) as question, \
                    patch.object(OpenAICompatibleProvider, 'translate') as transport:
                window.pilot_run_button.click()
                transport.assert_not_called()
                warning = question.call_args.args[2]
                self.assertIn('Стоимость неизвестна', warning)
                self.assertIn('не гарантируют денежный потолок', warning)
                self.assertIn('лимит API-ключа на стороне RelayModels', warning)
                self.assertEqual(question.call_args.args[-1], QMessageBox.No)
            self.assertIsNone(window.job)
            window.pilot_requests.setValue(2)
            self.assertIsNone(window.pilot_report)
            self.assertFalse(window.pilot_run_button.isEnabled())

    def test_api_profile_switch_vault_persistence_models_and_unknown_price(self):
        from dataclasses import asdict
        from PySide6.QtWidgets import QLineEdit
        from tests.test_providers import FakeOpener
        from wuwaru.provider_config import WindowsCredentials, ProviderSettings
        vault = {}
        paths = PathManager(self.root / 'portable')
        paths.initialize()
        with patch.object(WindowsCredentials, 'get', side_effect=lambda target: vault.get(target)), \
             patch.object(WindowsCredentials, 'set', side_effect=lambda target, key: vault.update({target: key})), \
             patch.object(WindowsCredentials, 'delete', side_effect=lambda target: vault.pop(target, None)):
            window = EditorWindow(self.root / 'project', paths)
            self.addCleanup(window.close)
            self.wait_idle(window)
            self.assertEqual(window.api_key.echoMode(), QLineEdit.Password)
            window.api_key.setText('synthetic-openai-key')
            window.save_api_settings()
            self.assertEqual(window.api_key.text(), '')
            openai = window.current_api_profile()
            window.api_provider.setCurrentText('RelayModels')
            self.assertEqual(window.api_url.text(), 'https://api.relaymodels.com/v1')
            self.assertIsNone(window.current_api_profile().key())
            window.api_key.setText('synthetic-relay-key')
            relay = window.save_api_settings()
            self.assertNotEqual(relay.credential_target, openai.credential_target)
            self.assertEqual(openai.key(), 'synthetic-openai-key')
            self.assertEqual(relay.key(), 'synthetic-relay-key')
            with patch('socket.socket', side_effect=AssertionError('external network')):
                window.check_api_config()
                self.assertIn('Стоимость неизвестна', window.api_status.text())
                window.pilot_preview_button.click()
                self.wait_idle(window)
                self.assertIsNone(window.pilot_report['preview']['estimated_cost'])
                self.assertTrue(window.pilot_run_button.isEnabled())
                opener = FakeOpener(b'{"data":[{"id":"gpt-6-luna"},{"id":"manual-model"}]}')
                with patch('urllib.request.build_opener', return_value=opener):
                    window.api_check_button.click()
                    self.wait_idle(window)
                self.assertEqual(opener.calls, 1)
                self.assertEqual(opener.request.get_method(), 'GET')
                self.assertIn('gpt-6-luna', window.api_status.text())
                window.api_model.setCurrentText('manual-model')
                window.api_input_price.setText('0.1')
                window.api_output_price.setText('0.5')
                window.save_api_settings()
                window.api_model.setCurrentText('gpt-6-luna')
                self.assertEqual(window.api_input_price.text(), '')
                window.api_model.setCurrentText('manual-model')
                self.assertEqual(window.api_input_price.text(), '')
                self.assertIsNone(window.current_api_profile().input_price)
                window.save_api_settings()
            stored = ProviderSettings(paths)
            self.assertEqual(stored.selected, 'RelayModels')
            self.assertEqual(stored.profile('RelayModels').model, 'manual-model')
            raw = paths.settings_path.read_text('utf-8')
            self.assertNotIn('synthetic-relay-key', raw)
            self.assertNotIn('synthetic-openai-key', raw)
            reopened = EditorWindow(self.root / 'project', paths)
            self.addCleanup(reopened.close)
            self.wait_idle(reopened)
            self.assertEqual(reopened.api_provider.currentText(), 'RelayModels')
            self.assertEqual(reopened.api_model.currentText(), 'manual-model')
            reopened.delete_api_key()
            self.assertIsNone(relay.key())
            self.assertEqual(openai.key(), 'synthetic-openai-key')
            core = TranslationDB(self.root / 'project')
            try:
                self.assertIsNone(core.record(1)['target'])
                self.assertEqual(core.db.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
            finally:
                core.close()

    def test_gui_mock_engine_and_real_project_manual_edits_are_isolated(self):
        core = TranslationDB(self.root / 'project')
        core.set_manual(1, 'Привет, Ровер!', approved=True)
        core.set_manual(2, 'С возвращением', approved=True)
        core.close()
        window = EditorWindow(self.root / 'project')
        self.addCleanup(window.close)
        self.wait_idle(window)
        window.task('mock-preview', {}, window.show_mock_preview)
        self.wait_idle(window)
        self.assertIn('Тестовых строк: 3', window.translation_estimate.text())
        window.task('mock-run', {}, window.show_mock_result)
        self.wait_idle(window)
        self.assertEqual(window.mock_results.rowCount(), 3)
        self.assertIn('DONE', window.mock_progress.text())
        saved = TranslationDB(self.root / 'project')
        try:
            self.assertEqual(saved.record(1)['target'], 'Привет, Ровер!')
            self.assertEqual(saved.record(2)['target'], 'С возвращением')
            self.assertEqual(saved.db.execute('SELECT count(*) FROM records WHERE source_type="SYNTHETIC_FIXTURE"').fetchone()[0], 0)
        finally:
            saved.close()
        with patch.object(QInputDialog, 'getText', return_value=('output/gui-export', True)):
            window.output('export')
        self.wait_idle(window)
        output = self.root / 'project' / 'output/gui-export'
        self.assertTrue(list(output.rglob('*.db')), f"{window.message.text()} · {list(output.rglob('*'))}")
        staging = self.root / 'staging' / 'Client/Content/Aki/ConfigDB/en'
        staging.mkdir(parents=True)
        shutil.copy2(self.root / 'source.db', staging / 'lang_test.db')
        pak_results = []
        window.task('pak-export', dict(source=staging.parents[4], name='output/gui-pak'), pak_results.append)
        self.wait_idle(window)
        self.assertEqual(pak_results[0]['evidence'], 'SYNTHETIC_ONLY')
        self.assertFalse(pak_results[0]['installation_allowed'])
        self.assertTrue((self.root / 'project/output/gui-pak/synthetic.pak').is_file())

    def test_resource_catalog_worker_keeps_synthetic_game_tree_read_only(self):
        from tests.test_discovery import fixture
        game = fixture(self.root / 'game')
        before = {path.relative_to(game): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in game.rglob('*') if path.is_file()}
        window = EditorWindow(self.root / 'project')
        self.addCleanup(window.close)
        self.wait_idle(window)
        results = []
        window.task('resource-catalog', dict(root=game), lambda result: (window.show_resource_catalog(result), results.append(result)))
        self.wait_idle(window)
        self.assertEqual(results[0]['package_count'], 2)
        self.assertTrue(Path(results[0]['index']).is_file())
        after = {path.relative_to(game): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in game.rglob('*') if path.is_file()}
        self.assertEqual(after, before)
