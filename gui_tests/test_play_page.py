"""Play page, offscreen. inspect / build_package / run_elevated / launch_steam are always replaced:
no helper, no UAC, no Steam, no real game. Functional evidence only, not visual QA."""
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QEventLoop, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from tests.test_discovery import FixtureCase
from tests.test_sqlite_source import database
from wuwaru import native_launch, native_package
from wuwaru.gui import EditorWindow
from wuwaru.native_launch import MOUNT, VERIFIED_FINGERPRINT, LaunchState, Phase
from wuwaru.portable import GameDiscoveryService, PathManager
from wuwaru.translation_db import TranslationDB
from wuwaru import play_page

RISK = 'Неофициальный машинный перевод. Риск для аккаунта не оценён.'
KEY = 'c' * 64
PACKAGE = {'schema_version': 1, 'key': KEY, 'pak_sha256': 'a' * 64, 'pak_sha1': 'B' * 40,
           'pak_bytes': 49_600_000, 'cells': 38945, 'kinds': ['speaker', 'ui']}
LABELS = {
    Phase.READY: ('ИГРАТЬ', 'с русским переводом', True),
    Phase.NEEDS_INSTALL: ('ИГРАТЬ', 'установить перевод', True),
    Phase.NEEDS_REBUILD: ('СОБРАТЬ И ИГРАТЬ', '', True),
    Phase.GAME_RUNNING: ('ИГРА ЗАПУЩЕНА', '', False),
    Phase.STEAM_UPDATING: ('STEAM ОБНОВЛЯЕТ', '', False),
    Phase.VERSION_CHANGED: ('ИГРАТЬ', 'без перевода', True),
    Phase.NO_GAME: ('Указать папку игры…', '', True),
    Phase.NO_INPUTS: ('Подключить входы…', '', True),
}


class PlayPageTests(FixtureCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        source = database(self.home / 'source.db', ((1, 'Hello'), (2, 'Sword')))
        core = TranslationDB(self.home / 'work')
        core.import_source('r.db', source)
        core.close()
        self.paths = PathManager(self.home / 'portable')
        self.paths.initialize()
        self.game = self.home / 'game'
        mount = self.game / MOUNT
        mount.parent.mkdir(parents=True)
        self.mount = ('::Mount::\n' + '\n'.join(VERIFIED_FINGERPRINT) + '\n::Del::\n').encode('utf-8')
        mount.write_bytes(self.mount)
        self.phase = Phase.READY
        self.installed = PACKAGE['pak_sha256']
        self.calls = []
        self.code = 0
        self.helper_error = None
        self.hero = None
        self.ours = False
        self.package = dict(PACKAGE)
        self.record = None
        self.applied = False
        self.installed_bytes = PACKAGE['pak_bytes']
        self.operations = None
        self.resources = None
        patches = [
            patch.object(GameDiscoveryService, 'discover', return_value={
                'installations': [{'path': str(self.game), 'origins': ['steam_manifest'],
                                   'client_version': None, 'steam_build_id': '123',
                                   'reader_state': 'NOT_CHECKED'}], 'warnings': []}),
            patch('wuwaru.native_launch.inspect', side_effect=self.fake_inspect),
            patch('wuwaru.native_helper.run_elevated', side_effect=self.fake_elevated),
            patch('wuwaru.native_helper.launch_steam', side_effect=lambda: self.calls.append('steam')),
            patch('wuwaru.native_package.build_package', side_effect=self.fake_build),
            patch('wuwaru.play_page.find_hero', side_effect=lambda roots: self.hero),
            patch('wuwaru.discovery.system_locations', return_value=([self.home / 'Steam'], [], [])),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.window = self.open(self.paths)

    def open(self, paths):
        window = EditorWindow(self.home / 'work', paths)
        self.addCleanup(window.close)
        window.show()
        self.wait_idle(window)
        return window

    def fake_inspect(self, game, native, key, *, input_error=None, build_id=None, **_):
        self.calls.append(('inspect', game, native, key, build_id))
        phase = Phase.READY if key == KEY and self.phase is Phase.NEEDS_REBUILD else self.phase
        return LaunchState(phase, reasons=['синтетика'], game=game, build_id=build_id,
                           fingerprint=VERIFIED_FINGERPRINT, ours_in_mount=self.ours, resources=self.resources,
                           installed_sha256=self.installed,
                           package=None if self.package is None else dict(self.package),
                           mount_sha256=hashlib.sha256(self.mount).hexdigest(),
                           installed_record=self.record,
                           installed_bytes=self.installed_bytes if self.installed else None,
                           mount_applied=self.applied and phase is Phase.READY)

    def fake_elevated(self, request):
        request = Path(request)
        data = json.loads(request.read_text('utf-8'))
        self.calls.append(('elevated', data))
        operations = self.operations
        if operations is None:
            operations = ([{'op': 'remove', 'write': 'mount', 'mount_sha256': '0' * 64},
                           {'op': 'remove', 'write': 'pak', 'pak': 'removed'}]
                          if data['operations'][0]['op'] == 'remove' else [])
        if self.code != 1223:  # a declined UAC prompt never starts the helper, so no result file
            request.with_name(request.stem + '.result.json').write_text(json.dumps(
                {'schema_version': 1, 'ok': self.code == 0, 'error': self.helper_error,
                 'operations': operations if self.code == 0 else []}), encoding='utf-8')
        return self.code

    def fake_build(self, project_db, native, kinds, *, cancelled=None, installed=None):
        self.calls.append(('build', Path(project_db), Path(native), tuple(kinds), installed))
        self.phase = Phase.NEEDS_INSTALL
        self.package = dict(PACKAGE, key='f' * 64, pak_sha256='9' * 64, pak_sha1='C' * 40)
        return dict(self.package, rebuilt=True)

    def wait_idle(self, window=None, delay=0):
        window = window or self.window
        if delay:
            pause = QEventLoop()
            QTimer.singleShot(delay, pause.quit)
            pause.exec()
        loop, timer = QEventLoop(), QTimer()
        timer.timeout.connect(lambda: loop.quit() if window.job is None else None)
        timer.start(10)
        QTimer.singleShot(15000, loop.quit)
        loop.exec()
        timer.stop()
        self.assertIsNone(window.job, 'GUI worker did not complete')
        self.assertFalse(window.last_error, window.last_error)

    def show(self, phase):
        self.phase = phase
        self.window.refresh_play()
        self.wait_idle()
        self.assertIs(self.window.play_phase, phase)

    def elevated(self):
        return [call[1] for call in self.calls if call[0] == 'elevated']

    def test_play_page_is_start_page(self):
        self.assertIs(self.window.stack.currentWidget(), self.window.play_page)
        self.assertEqual(self.window.tabs.currentIndex(), 0)
        self.assertEqual(self.window.tabs.tabText(0), 'Играть')
        inspect = [call for call in self.calls if call[0] == 'inspect'][-1]
        self.assertEqual((inspect[1], inspect[2], inspect[4]), (self.game, self.paths.native, '123'))

    def test_button_label_per_phase(self):
        for phase, (title, subtitle, enabled) in LABELS.items():
            with self.subTest(phase=phase):
                self.show(phase)
                self.assertEqual(self.window.play_button.text(), title)
                self.assertEqual(self.window.play_button.subtitle, subtitle)
                self.assertIs(self.window.play_button.isEnabled(), enabled)
        self.show(Phase.NEEDS_REBUILD)
        self.assertTrue(self.window.play_alt_button.isVisible())
        self.assertEqual(self.window.play_alt_button.text(), 'Играть со старым')
        self.installed = None
        self.show(Phase.NEEDS_REBUILD)
        self.assertFalse(self.window.play_alt_button.isVisible())
        self.show(Phase.VERSION_CHANGED)
        self.assertEqual(self.window.play_alt_button.text(), 'Принять версию на свой риск…')
        self.show(Phase.READY)
        self.assertFalse(self.window.play_alt_button.isVisible())

    def test_ready_play_runs_helper_then_steam(self):
        self.show(Phase.READY)
        self.window.play()
        self.wait_idle()
        requests = self.elevated()
        self.assertEqual([op['op'] for op in requests[0]['operations']], ['apply_mount'])
        self.assertEqual(requests[0]['game'], str(self.game.absolute()))
        self.assertEqual(self.calls.count('steam'), 1)
        self.assertEqual(self.paths.native.joinpath('mount-original.txt').read_bytes(), self.mount)

    def test_uac_cancel_does_not_launch(self):
        self.show(Phase.READY)
        self.code = 1223
        self.window.play()
        self.wait_idle()
        self.assertEqual(len(self.elevated()), 1)
        self.assertNotIn('steam', self.calls)
        self.assertEqual(self.window.play_status.text(), 'Без разрешения перевод не подключится')

    def test_helper_failure_shows_result_error_and_does_not_launch(self):
        self.show(Phase.READY)
        self.code = 2
        self.helper_error = 'ValueError: Mount изменился после проверки'
        self.window.play()
        self.wait_idle()
        self.assertNotIn('steam', self.calls)
        self.assertIn('Mount изменился после проверки', self.window.play_status.text())

    def test_install_success_deletes_staging_then_launches(self):
        self.show(Phase.NEEDS_INSTALL)
        staging = self.paths.native / 'staging' / (KEY + '.pak')
        staging.parent.mkdir(parents=True)
        staging.write_bytes(b'package')
        self.window.play()
        self.wait_idle()
        self.assertEqual([op['op'] for op in self.elevated()[0]['operations']],
                         ['install_package', 'apply_mount'])
        self.assertFalse(staging.exists())
        self.assertEqual(self.calls.count('steam'), 1)
        self.assertEqual(json.loads((self.paths.native / 'installed.json').read_text('utf-8')),
                         {'schema_version': 1, 'key': KEY, 'pak_sha256': PACKAGE['pak_sha256'],
                          'pak_sha1': PACKAGE['pak_sha1']})

    def test_rebuild_then_uac_cancel_keeps_remove_with_installed_record(self):
        self.record = {'schema_version': 1, 'key': KEY, 'pak_sha256': PACKAGE['pak_sha256'],
                       'pak_sha1': PACKAGE['pak_sha1']}
        self.ours = True
        self.show(Phase.NEEDS_REBUILD)
        self.code = 1223
        self.window.play()
        self.wait_idle()
        self.assertEqual(self.window.play_status.text(), 'Без разрешения перевод не подключится')
        self.assertIs(self.window.play_phase, Phase.NEEDS_INSTALL)
        self.assertNotEqual(self.window.play_state.package['pak_sha256'], PACKAGE['pak_sha256'])
        self.assertTrue(self.window.remove_button.isEnabled())
        self.code = 0
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Accepted):
            self.window.remove_translation()
            self.wait_idle()
        self.assertEqual(self.elevated()[-1]['operations'][0]['op'], 'remove')
        self.assertEqual(self.elevated()[-1]['operations'][0]['pak_sha256'], PACKAGE['pak_sha256'])
        self.assertNotIn('steam', self.calls)

    def test_install_cancelled_keeps_staging(self):
        self.show(Phase.NEEDS_INSTALL)
        staging = self.paths.native / 'staging' / (KEY + '.pak')
        staging.parent.mkdir(parents=True)
        staging.write_bytes(b'package')
        self.code = 1223
        self.window.play()
        self.wait_idle()
        self.assertTrue(staging.exists())

    def test_rebuild_builds_then_installs(self):
        self.show(Phase.NEEDS_REBUILD)
        self.window.play()
        self.wait_idle()
        build = [call for call in self.calls if call[0] == 'build']
        self.assertEqual(build[0][1:4], (self.home / 'work/translations.sqlite', self.paths.native, ('speaker', 'ui')))
        self.assertEqual(Path(build[0][4]), (self.game / native_launch.PAK_RELATIVE).absolute())
        self.assertEqual([op['op'] for op in self.elevated()[0]['operations']],
                         ['install_package', 'apply_mount'])
        self.assertEqual(self.calls.count('steam'), 1)

    def test_play_with_old_reinspects_with_installed_package_key(self):
        self.show(Phase.NEEDS_REBUILD)
        self.window.play_alt_button.click()
        self.wait_idle()
        self.assertFalse([call for call in self.calls if call[0] == 'build'])
        before_helper = self.calls[:next(i for i, call in enumerate(self.calls) if call[0] == 'elevated')]
        self.assertEqual([call[3] for call in before_helper if call[0] == 'inspect'][-1], KEY)
        self.assertEqual([op['op'] for op in self.elevated()[0]['operations']], ['apply_mount'])
        self.assertEqual(self.calls.count('steam'), 1)

    def test_version_changed_with_our_line_removes_before_playing(self):
        self.ours = True
        self.show(Phase.VERSION_CHANGED)
        self.assertEqual(self.window.play_button.text(), 'УБРАТЬ ПЕРЕВОД И ИГРАТЬ')
        self.assertTrue(self.window.play_button.isEnabled())
        rows = dict((label, value) for label, value, _ in play_page.panel_for(self.window.play_state).rows)
        self.assertIn('в списке пакетов', rows['Наш пакет'])
        self.assertNotIn('не подключается', rows['Наш пакет'])
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Rejected):
            self.window.play()
            self.wait_idle()
        self.assertEqual(self.elevated(), [])
        self.assertNotIn('steam', self.calls)
        self.code = 1223
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Accepted):
            self.window.play()
            self.wait_idle()
        self.assertEqual([op['op'] for op in self.elevated()[0]['operations']], ['remove'])
        self.assertNotIn('steam', self.calls)
        self.assertEqual(self.window.play_status.text(), 'Без разрешения перевод не удалится')
        self.code, self.helper_error = 2, 'ValueError: Mount изменился после проверки'
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Accepted):
            self.window.play()
            self.wait_idle()
        self.assertNotIn('steam', self.calls)
        self.code, self.helper_error = 0, None
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Accepted):
            self.window.play()
            self.wait_idle()
        self.assertEqual(len(self.elevated()), 3)
        self.assertEqual(self.calls.count('steam'), 1)

    def test_version_changed_plays_without_translation(self):
        self.show(Phase.VERSION_CHANGED)
        self.window.play()
        self.wait_idle()
        self.assertEqual(self.calls.count('steam'), 1)
        self.assertEqual(self.elevated(), [])
        self.assertFalse((self.paths.native / 'requests').exists())

    def test_accept_version_asks_then_records_fingerprint(self):
        self.show(Phase.VERSION_CHANGED)
        with patch('wuwaru.native_launch.accept_fingerprint') as accept, \
                patch.object(QMessageBox, 'question', return_value=QMessageBox.No) as question:
            self.window.play_alt_button.click()
            self.wait_idle()
            accept.assert_not_called()
            self.assertIn('UNKNOWN', question.call_args.args[2])
            self.assertEqual(question.call_args.args[-1], QMessageBox.No)
            question.return_value = QMessageBox.Yes
            self.window.play_alt_button.click()
            self.wait_idle()
            accept.assert_called_once_with(self.paths.native, VERIFIED_FINGERPRINT)

    def test_panel_names_the_table_the_package_is_built_on(self):
        self.show(Phase.READY)  # a package.json from before tables could change: the pinned snapshot
        rows = dict((label, value) for label, value, _ in play_page.panel_for(self.window.play_state).rows)
        self.assertEqual(rows['Английская таблица'], 'WWUIResourcesFull 30b665d7e783 · 30.09.2026')
        newer = dict(native_package.PINNED_TABLE, revision='d' * 40, data_date='2026-11-07T06:00:00Z')
        self.package = dict(PACKAGE, table=newer)
        self.show(Phase.READY)
        rows = dict((label, value) for label, value, _ in play_page.panel_for(self.window.play_state).rows)
        self.assertEqual(rows['Английская таблица'], 'WWUIResourcesFull dddddddddddd · 07.11.2026')
        state = LaunchState(Phase.VERSION_CHANGED, fingerprint=VERIFIED_FINGERPRINT, table=newer)
        rows = dict((label, value) for label, value, _ in play_page.panel_for(state).rows)
        self.assertEqual(rows['Английская таблица'], 'WWUIResourcesFull dddddddddddd · 07.11.2026')

    def test_accept_version_warns_when_the_table_is_from_another_version(self):
        self.show(Phase.VERSION_CHANGED)
        for reused, expected in ((True, True), (False, False)):
            with patch('wuwaru.native_launch.table_reused', return_value=reused), \
                    patch.object(QMessageBox, 'question', return_value=QMessageBox.No) as question:
                self.window.play_alt_button.click()
                self.wait_idle()
            text = question.call_args.args[2]
            self.assertIn('WWUIResourcesFull 30b665d7e783', text)
            self.assertEqual('новые официальные строки' in text, expected)
            self.assertEqual('«Получить таблицу»' in text, expected)

    def test_version_changed_offers_the_table_of_this_game_version(self):
        button = self.window.play_table_button
        for phase in Phase:
            self.show(phase)
            self.assertFalse(button.isVisible(), phase)  # the game version is not known
        self.resources = '3.8.0'
        for phase in Phase:
            self.show(phase)
            self.assertEqual(button.isVisible(), phase is Phase.VERSION_CHANGED, phase)
        self.show(Phase.VERSION_CHANGED)
        self.assertIn('«Получить таблицу»', self.window.play_note.text())
        record = dict(native_package.PINNED_TABLE, repository='Arikatsu/WutheringWaves_Data',
                      revision='e' * 40, data_date='2026-11-10T05:39:55Z', source_blob='1' * 40)
        self.calls.clear()
        with patch('wuwaru.table_fetch.update', return_value={'record': record, 'changed': True}) as update:
            button.click()
            self.wait_idle()
        update.assert_called_once_with(self.paths.native, '3.8.0')
        self.assertIn('Таблица получена: WutheringWaves_Data eeeeeeeeeeee · 10.11.2026', self.window.play_status.text())
        self.assertEqual([call[0] for call in self.calls], ['inspect'])  # nothing but a fresh look at the game
        with patch('wuwaru.table_fetch.update', return_value={'record': record, 'changed': False}):
            button.click()
            self.wait_idle()
        self.assertIn('Таблица уже этой версии', self.window.play_status.text())

    def test_a_table_that_cannot_be_taken_says_why(self):
        import urllib.error
        self.resources = '3.9.0'
        self.show(Phase.VERSION_CHANGED)
        failures = ((ValueError('В публичном тексте игры пока нет версии 3.9.0'), 'пока нет версии 3.9.0'),
                    (urllib.error.URLError('offline'), 'GitHub не ответил'))
        for error, expected in failures:
            with self.subTest(expected), patch('wuwaru.table_fetch.update', side_effect=error):
                self.window.play_table_button.click()
                self.wait_idle()
                status = self.window.play_status.text()
                self.assertIn('Таблица не получена', status)
                self.assertIn(expected, status)
                self.assertIs(self.window.play_phase, Phase.VERSION_CHANGED)
                self.assertTrue(self.window.play_table_button.isEnabled())

    def test_remove_confirms_first(self):
        self.show(Phase.READY)
        self.assertTrue(self.window.remove_button.isEnabled())
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Rejected):
            self.window.remove_translation()
            self.wait_idle()
        self.assertEqual(self.elevated(), [])
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Accepted):
            self.window.remove_translation()
            self.wait_idle()
        self.assertEqual(self.elevated()[0]['operations'],
                         [{'op': 'remove', 'pak_sha256': PACKAGE['pak_sha256'],
                           'expected_sha256': hashlib.sha256(self.mount).hexdigest()}])
        self.assertNotIn('steam', self.calls)

    def dialog_text(self):
        dialog = play_page.RemoveDialog(self.window.play_state, self.window)
        self.addCleanup(dialog.deleteLater)
        return '\n'.join(label.text() for label in dialog.findChildren(play_page.QLabel))

    def test_remove_dialog_text_follows_mockup(self):
        text = self.dialog_text()
        self.assertIn('Удалить перевод из игры?', text)
        self.assertIn('Из папки игры уйдёт наш пакет, 47,3 МБ.', text)
        self.assertIn('Нашей строки в списке пакетов нет.', text)
        self.assertIn('Игра и лаунчер должны быть закрыты. Windows спросит разрешение.', text)

    def test_remove_dialog_size_is_the_installed_file_and_pak_only_when_deleted(self):
        # A newer build in package.json; the game still holds the recorded old PAK of 10 MiB.
        self.record = {'schema_version': 1, 'key': KEY, 'pak_sha256': PACKAGE['pak_sha256'],
                       'pak_sha1': PACKAGE['pak_sha1']}
        self.package = dict(PACKAGE, key='f' * 64, pak_sha256='9' * 64, pak_bytes=99_000_000)
        self.installed_bytes = 10 * 1024 * 1024
        self.ours = True
        self.show(Phase.NEEDS_INSTALL)
        text = self.dialog_text()
        self.assertIn('Из папки игры уйдёт наш пакет, 10,0 МБ.', text)
        self.assertIn('Из списка пакетов уйдёт наша строка.', text)
        # No record names the file: the helper keeps it, the dialog says so.
        self.package, self.record = None, None
        self.show(Phase.NO_INPUTS)
        text = self.dialog_text()
        self.assertNotIn('уйдёт наш пакет', text)
        self.assertIn('Файл пакета останется', text)

    def test_remove_needs_our_line_without_any_record(self):
        self.package, self.record, self.ours = None, None, False
        self.show(Phase.NO_INPUTS)
        self.assertFalse(self.window.remove_button.isEnabled())  # only a bare file: nothing we may delete
        self.ours = True
        self.show(Phase.NO_INPUTS)
        self.assertTrue(self.window.remove_button.isEnabled())
        self.operations = [{'op': 'remove', 'write': 'mount', 'mount_sha256': '0' * 64},
                           {'op': 'remove', 'write': None, 'pak': 'kept: no package.json'}]
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Accepted):
            self.window.remove_translation()
            self.wait_idle()
        status = self.window.play_status.text()
        self.assertIn('Наша строка убрана из списка пакетов.', status)
        self.assertIn('Пакет не удалялся: у приложения нет записи о нём.', status)
        self.assertNotIn('удалён из папки', status)

    def test_remove_message_follows_helper_operations(self):
        self.show(Phase.READY)
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Accepted):
            self.window.remove_translation()
            self.wait_idle()
        self.assertEqual(self.window.play_status.text(), 'Наша строка убрана из списка пакетов. '
                         'Пакет перевода удалён из папки игры. В проекте перевод остаётся.')
        self.operations = [{'op': 'remove', 'write': None, 'pak': 'kept: missing'}]
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Accepted):
            self.window.remove_translation()
            self.wait_idle()
        self.assertIn('Пакета в папке игры уже не было.', self.window.play_status.text())
        self.assertNotIn('удалён из папки', self.window.play_status.text())

    def test_second_play_acts_on_fresh_state(self):
        self.show(Phase.READY)
        self.window.play()
        self.wait_idle()
        # The client reset Mount after start; the page still shows the state from before.
        changed = self.mount.replace(b'::Del::', b'Lang_en/Base/pakchunk10-WuWaRussian,900,' + b'B' * 40
                                     + b',,,\n::Del::')
        (self.game / MOUNT).write_bytes(changed)
        self.mount = changed
        self.window.play()
        self.wait_idle()
        requests = self.elevated()
        self.assertEqual(len(requests), 2, self.window.play_status.text())
        self.assertEqual(requests[1]['operations'][0]['expected_sha256'], hashlib.sha256(changed).hexdigest())
        self.assertEqual(self.calls.count('steam'), 2)
        # The game was started meanwhile: the click sees it and writes nothing.
        self.phase = Phase.GAME_RUNNING
        self.window.play()
        self.wait_idle()
        self.assertEqual(len(self.elevated()), 2)
        self.assertIs(self.window.play_phase, Phase.GAME_RUNNING)
        self.assertEqual(self.calls.count('steam'), 2)
        self.phase = Phase.READY
        with patch.object(play_page.RemoveDialog, 'exec', return_value=QDialog.Accepted) as dialog:
            self.phase = Phase.GAME_RUNNING
            self.window.remove_translation()
            self.wait_idle()
        dialog.assert_not_called()
        self.assertEqual(len(self.elevated()), 2)

    def test_activation_refreshes_the_play_page(self):
        self.show(Phase.READY)
        self.window.ACTIVATION_DELAY_MS = 0
        self.phase = Phase.GAME_RUNNING
        with patch.object(self.window, 'isActiveWindow', return_value=True):
            self.window.navigate(4)  # another page: no inspect on activation
            QApplication.sendEvent(self.window, QEvent(QEvent.ActivationChange))
            self.wait_idle(delay=50)
            self.assertIs(self.window.play_phase, Phase.READY)
            self.window.navigate(self.window.page_widgets.index(self.window.play_page))
            QApplication.sendEvent(self.window, QEvent(QEvent.ActivationChange))
            self.wait_idle(delay=50)
        self.assertIs(self.window.play_phase, Phase.GAME_RUNNING)

    def test_ready_with_line_mounted_launches_without_helper(self):
        self.applied = True
        self.show(Phase.READY)
        self.assertIn('без запроса Windows', self.window.play_hint.text())
        self.window.play()
        self.wait_idle()
        self.assertEqual(self.elevated(), [])
        self.assertEqual(self.calls.count('steam'), 1)
        self.assertFalse((self.paths.native / 'requests').exists())

    def test_left_out_reasons_in_the_panel(self):
        self.package = dict(PACKAGE, left_out={'CHECK_FAILED': 210, 'NOT_IN_TABLE': 120,
                                               'STATUS_UNTRANSLATED': 5, 'STATUS_NEEDS_REVIEW': 2})
        self.show(Phase.READY)
        label = self.window.play_left_out
        self.assertTrue(label.isVisible())
        self.assertEqual(label.text(), 'Не вошло 337: не прошла проверку — 210, нет в таблице — 120, …')
        for text in ('не прошла проверку — 210', 'нет в таблице — 120', 'не переведено — 5',
                     'статус NEEDS_REVIEW — 2'):
            self.assertIn(text, label.toolTip())
        self.package = dict(PACKAGE)
        self.show(Phase.READY)
        self.assertFalse(self.window.play_left_out.isVisible())

    def test_out_of_space_build_error_in_russian(self):
        def full(*args, **kwargs):
            raise OSError('Not enough free space: %d bytes needed, %d available' % (100 * 1024 * 1024,
                                                                                    50 * 1024 * 1024))
        self.show(Phase.NEEDS_REBUILD)
        with patch('wuwaru.native_package.build_package', side_effect=full):
            self.window.play()
            self.wait_idle()
        status = self.window.play_status.text()
        self.assertIn('Сборка не завершена: не хватает места в Data/Native: нужно 100,0 МБ, свободно 50,0 МБ', status)
        self.assertEqual(self.elevated(), [])
        self.assertEqual(play_page.build_error('OSError: other'), 'OSError: other')

    def test_running_and_updating_disable_remove(self):
        for phase in (Phase.GAME_RUNNING, Phase.STEAM_UPDATING, Phase.NO_GAME):
            with self.subTest(phase=phase):
                self.show(phase)
                self.assertFalse(self.window.remove_button.isEnabled())

    def test_remove_disabled_with_reason_when_installed_file_matches_no_record(self):
        self.installed = 'd' * 64  # helper would refuse: only package.json / installed.json name the PAK
        self.ours = True
        self.show(Phase.NEEDS_INSTALL)
        self.assertFalse(self.window.remove_button.isEnabled())
        reason = 'Перевод в игре не совпадает с записями приложения — удали файл вручную'
        path = str(self.game / native_launch.PAK_RELATIVE)
        for text in (self.window.remove_button.toolTip(), self.window.play_hint.text()):
            self.assertIn(reason, text)
            self.assertIn(path, text)
        self.installed = PACKAGE['pak_sha256']
        self.show(Phase.NEEDS_INSTALL)
        self.assertTrue(self.window.remove_button.isEnabled())
        self.assertEqual(self.window.remove_button.toolTip(), 'Удалить перевод')

    def test_risk_line_always_visible(self):
        for phase in LABELS:
            with self.subTest(phase=phase):
                self.show(phase)
                self.assertTrue(self.window.play_risk.isVisible())
                self.assertEqual(self.window.play_risk.text(), RISK)
        bare = self.open(None)
        self.assertTrue(bare.play_risk.isVisible())
        self.assertFalse(bare.play_button.isEnabled())
        self.assertIn('portable', bare.play_status.text())

    def test_more_menu_reaches_all_pages(self):
        actions = {action.text(): action for action in self.window.more_button.menu().actions()}
        expected = {'Игровые ресурсы': 1, 'База переводов': 2, 'Экспорт': 6,
                    'Нативная совместимость': 8, 'Файлы проекта': 9, 'Главная': 0}
        self.assertEqual(list(actions), list(expected))
        for text, index in expected.items():
            actions[text].trigger()
            self.assertEqual(self.window.stack.currentIndex(), index)
        tabs = {'Играть': self.window.page_widgets.index(self.window.play_page),
                'Перевод': 3, 'Редактор': 4, 'Проверка': 5, 'Настройки': 7}
        self.assertEqual([self.window.tabs.tabText(i) for i in range(len(tabs))], list(tabs))
        for position, index in enumerate(tabs.values()):
            self.window.tabs.setCurrentIndex(position)
            self.assertEqual(self.window.stack.currentIndex(), index)
        self.wait_idle()
        self.window.navigate(9)
        self.assertEqual(self.window.stack.currentIndex(), 9)
        self.window.navigate(4)
        self.assertEqual(self.window.tabs.currentIndex(), 2)

    def test_no_inputs_connect_checks_hash_before_copy(self):
        self.show(Phase.NO_INPUTS)
        table, font = self.home / 'en.db', self.home / 'font.ttf'
        table.write_bytes(b'table')
        font.write_bytes(b'font')
        with patch('wuwaru.desktop_pages.QFileDialog.getOpenFileName',
                   side_effect=[(str(table), ''), (str(font), '')]):
            self.window.play()
            self.wait_idle()
        self.assertIn('не совпадает', self.window.play_status.text())
        self.assertEqual(sorted(path.name for path in self.paths.native.iterdir()), [])
        pins = (native_package.sha256(table), native_package.sha256(font))
        real = native_package.install_inputs
        with patch('wuwaru.desktop_pages.QFileDialog.getOpenFileName',
                   side_effect=[(str(table), ''), (str(font), '')]), \
                patch('wuwaru.native_package.install_inputs',
                      side_effect=lambda t, f, n: real(t, f, n, pins=pins)):
            self.window.play()
            self.wait_idle()
        self.assertEqual((self.paths.native / 'lang_multi_text.en.db').read_bytes(), b'table')
        self.assertEqual((self.paths.native / 'NotoSans-600.ttf').read_bytes(), b'font')

    def test_settings_waves_default_and_unverified_marks(self):
        boxes = self.window.native_kind_boxes
        self.assertEqual({kind for kind, box in boxes.items() if box.isChecked()}, {'speaker', 'ui'})
        for kind, box in boxes.items():
            self.assertEqual('в игре не проверено' in box.text(), kind not in ('speaker', 'ui'), kind)
        boxes['item'].setChecked(True)
        self.wait_idle()
        self.assertEqual(self.paths.native_kinds(), ('speaker', 'ui', 'item'))
        self.assertEqual([call[3] for call in self.calls if call[0] == 'inspect'][-1:], [None])

    def test_background_is_the_found_cover_or_plain_ground(self):
        self.assertIsNone(self.window.play_page.pixmap)
        self.assertFalse(self.window.play_art_note.isVisible())
        from PySide6.QtGui import QColor, QPixmap
        image = QPixmap(64, 32)
        image.fill(QColor('#335577'))
        self.hero = self.home / 'library_hero.png'
        self.assertTrue(image.save(str(self.hero)))
        window = self.open(self.paths)
        self.assertFalse(window.play_page.pixmap.isNull())
        self.assertTrue(window.play_art_note.isVisible())
        self.assertGreater(window.grab().width(), 0)

    def test_screens_render_every_phase(self):
        folder = self.home / 'screens'
        folder.mkdir(parents=True, exist_ok=True)
        self.window.resize(1200, 760)
        for phase in LABELS:
            self.show(phase)
            self.assertTrue(self.window.grab().save(str(folder / f'play-{phase.value.lower()}.png')))
        dialog = play_page.RemoveDialog(self.window.play_state, self.window)
        dialog.show()
        self.assertTrue(dialog.grab().save(str(folder / 'remove-dialog.png')))
        dialog.close()

    def test_bundled_fonts_load(self):
        folder = Path(play_page.__file__).parent / 'assets' / 'fonts'
        families = set()
        for name in ('Unbounded[wght].ttf', 'NotoSans[wdth,wght].ttf'):
            ident = QFontDatabase.addApplicationFont(str(folder / name))
            self.assertGreaterEqual(ident, 0, name)
            families.update(QFontDatabase.applicationFontFamilies(ident))
        self.assertIn('Unbounded', families)
        self.assertIn('Noto Sans', families)
        self.assertTrue(play_page.register_fonts())


if __name__ == '__main__':
    import unittest
    unittest.main()
