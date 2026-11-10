"""Player window (LauncherWindow), offscreen: the whole Play page suite runs on it too, plus the
frameless compact shell, settings and the clock-paced progress. Functional evidence only."""
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QPushButton

from gui_tests.test_play_page import PlayPageTests
from wuwaru import launcher, play_page
from wuwaru.native_launch import Phase


class LauncherPlayTests(PlayPageTests):
    # Editor-only shell (tabs, «Ещё», editor settings page); the launcher has its own tests below.
    test_play_page_is_start_page = None
    test_more_menu_reaches_all_pages = None
    test_no_inputs_connect_checks_hash_before_copy = None

    def open(self, paths):
        window = launcher.LauncherWindow(self.home / 'work', paths)
        self.addCleanup(window.close)
        window.show()
        self.wait_idle(window)
        return window

    def test_frameless_compact_window(self):
        window = self.window
        self.assertTrue(window.windowFlags() & Qt.FramelessWindowHint)
        self.assertEqual((window.width(), window.height()), (launcher.WIDTH, launcher.HEIGHT))
        self.assertLess(window.width(), 1440)
        self.assertLess(window.height(), 920)
        self.assertIs(window.stack.currentWidget(), window.play_page)
        self.assertEqual([button.text() for button in window.title_bar.sections], ['Играть', 'Настройки'])

    def test_no_translator_tools(self):
        texts = {button.text() for button in self.window.findChildren(QPushButton)}
        for tool in ('Редактор', 'Перевод', 'Проверка', 'Экспорт', 'Ещё'):
            self.assertNotIn(tool, texts)
        self.assertFalse(hasattr(self.window, 'table'))

    def test_settings_section_and_remove(self):
        self.window.title_bar.sections[1].click()
        self.assertIs(self.window.stack.currentWidget(), self.window.settings_page)
        self.assertEqual(self.window.settings_game.toolTip(), str(self.game))
        self.assertNotIn('(speaker)', self.window.native_kind_boxes['speaker'].text())
        self.ours = True
        self.show(Phase.READY)
        self.window.show_section(1)
        self.assertTrue(self.window.settings_remove.isEnabled())
        with patch('wuwaru.play_page.RemoveDialog.exec', return_value=0):
            self.window.settings_remove.click()
            self.wait_idle()
        self.assertIs(self.window.stack.currentWidget(), self.window.play_page)
        self.assertEqual(self.elevated(), [])

    def test_activation_refreshes_the_play_page(self):
        self.show(Phase.READY)
        self.window.ACTIVATION_DELAY_MS = 0
        self.phase = Phase.GAME_RUNNING
        with patch.object(self.window, 'isActiveWindow', return_value=True):
            self.window.show_section(1)  # settings: no inspect on activation
            QApplication.sendEvent(self.window, QEvent(QEvent.ActivationChange))
            self.wait_idle(delay=50)
            self.assertIs(self.window.play_phase, Phase.READY)
            self.window.show_section(0)
            QApplication.sendEvent(self.window, QEvent(QEvent.ActivationChange))
            self.wait_idle(delay=50)
        self.assertIs(self.window.play_phase, Phase.GAME_RUNNING)

    def test_inspect_time_is_learned_for_the_progress(self):
        self.assertIn('native-inspect', self.paths._settings().get('timings', {}))
        self.assertFalse(self.window.play_progress.isVisible())

    def test_screens(self):
        folder = Path(__file__).resolve().parents[1] / '.test-tmp' / 'screens'
        folder.mkdir(parents=True, exist_ok=True)
        self.show(Phase.READY)
        self.assertTrue(self.window.grab().save(str(folder / 'launcher-play.png')))
        self.window._timed_start('native-inspect', 'ПРОВЕРКА…')
        self.assertTrue(self.window.grab().save(str(folder / 'launcher-progress.png')))
        self.window._timed_finish(ok=False)
        self.window.show_section(1)
        self.assertTrue(self.window.grab().save(str(folder / 'launcher-settings.png')))


class TimedFractionTests(unittest.TestCase):
    def test_follows_the_clock_and_never_completes_early(self):
        f = play_page.timed_fraction
        self.assertEqual(f(0, 10), 0)
        self.assertAlmostEqual(f(5, 10), 0.5)
        self.assertAlmostEqual(f(9, 10), 0.9)
        self.assertLessEqual(f(1000, 10), 0.99)
        self.assertLess(f(9.5, 10), f(12, 10))


def _release(version):
    from wuwaru import updater
    return updater.Release(version, f'WuWaRussian-Setup-{version}.exe', 'https://github.com/x/setup',
                           'https://github.com/x/update.json', 'Новое', '')


class LauncherUpdateTests(LauncherPlayTests):
    """Update check in the launcher: network and installer are mocked; nothing is downloaded."""

    def until(self, condition, timeout=5000):
        from PySide6.QtCore import QElapsedTimer
        clock = QElapsedTimer()
        clock.start()
        while not condition() and clock.elapsed() < timeout:
            QApplication.processEvents()
        self.assertTrue(condition(), 'condition not reached')

    def check(self, release=None, error=None, root=None):
        from wuwaru import updater
        side = updater.UpdateError(error) if error else None
        with patch.object(updater, 'latest_release', return_value=release, side_effect=side), \
                patch.object(updater, 'install_root', return_value=root), \
                patch.object(self.window, 'open_update_dialog', return_value=0) as dialog:
            self.window.show_section(1)
            self.window.update_button.click()
            self.until(lambda: not self.window._update_checking)
        return dialog

    def test_no_check_on_start_when_not_installed(self):
        from wuwaru import update_ui, updater
        with patch.object(updater, 'install_root', return_value=None), \
                patch.object(update_ui.QTimer, 'singleShot') as timer:
            self.window.schedule_update_check()
        timer.assert_not_called()
        with patch.object(updater, 'install_root', return_value=self.home), \
                patch.object(update_ui.QTimer, 'singleShot') as timer:
            self.window.schedule_update_check()
        timer.assert_called_once()

    def test_up_to_date(self):
        dialog = self.check(_release('0.0.1'))
        dialog.assert_not_called()
        self.assertIn('последняя версия', self.window.update_status.text())

    def test_offline_message_only_on_manual_check(self):
        self.check(error='Не удалось связаться с GitHub: нет сети')
        self.assertIn('нет сети', self.window.update_status.text())

    def test_newer_version_but_not_installed_points_to_releases(self):
        dialog = self.check(_release('99.0.0'), root=None)
        dialog.assert_not_called()
        self.assertIn('99.0.0', self.window.update_status.text())

    def test_newer_version_opens_dialog_with_data_folder(self):
        dialog = self.check(_release('99.0.0'), root=self.home / 'install')
        dialog.assert_called_once()
        release, folder, root = dialog.call_args.args
        self.assertEqual(release.version, '99.0.0')
        self.assertEqual(folder, self.paths.data / 'Updates')
        self.assertEqual(root, self.home / 'install')
        self.assertIs(self.window.isVisible(), True)

    def test_dialog_downloads_then_hands_over_to_installer(self):
        from wuwaru import update_ui, updater
        setup = self.home / 'WuWaRussian-Setup-99.0.0.exe'
        with patch.object(updater, 'download', return_value=setup) as download, \
                patch.object(updater, 'start_installer') as start:
            dialog = update_ui.UpdateDialog(_release('99.0.0'), self.home / 'Updates', self.home / 'install',
                                            self.window)
            self.addCleanup(dialog.close)
            dialog.show()
            folder = Path(__file__).resolve().parents[1] / '.test-tmp' / 'screens'
            folder.mkdir(parents=True, exist_ok=True)
            dialog.grab().save(str(folder / 'launcher-update.png'))
            dialog.update_button.click()
            self.until(lambda: start.called)
        self.assertEqual(download.call_args.args[:2], (_release('99.0.0'), self.home / 'Updates'))
        start.assert_called_once_with(setup, self.home / 'install')
        self.assertEqual(dialog.result(), 1)

    def test_dialog_shows_refusal_and_offers_retry(self):
        from wuwaru import update_ui, updater
        with patch.object(updater, 'download', side_effect=updater.UpdateError('Подпись не сходится')), \
                patch.object(updater, 'start_installer') as start:
            dialog = update_ui.UpdateDialog(_release('99.0.0'), self.home / 'Updates', self.home / 'install',
                                            self.window)
            self.addCleanup(dialog.close)
            dialog.show()
            dialog.update_button.click()
            self.until(lambda: dialog.update_button.isEnabled())
        start.assert_not_called()
        self.assertEqual(dialog.status.text(), 'Подпись не сходится')
        self.assertEqual(dialog.update_button.text(), 'Повторить')
