"""Installer `--update` mode, offscreen: straight to progress, installs over, reopens the launcher. Functional only."""
import os
import shutil
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from installer import core, ui  # noqa: E402
from tests.test_installer_core import TMP, isolate_system, make_zip  # noqa: E402


class UpdateWizardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        isolate_system(self)
        self.addCleanup(shutil.rmtree, TMP, True)
        self.target = TMP / 'app'
        old = TMP / 'old.zip'
        make_zip(old, {'WuWaRussian.exe': b'v1', 'seed/Data/settings.json': b'{}'}, '0.1.0')
        core.install(old, self.target)
        (self.target / 'Data/settings.json').write_text('{"mine": 1}')
        self.payload = TMP / 'payload.zip'
        make_zip(self.payload, {'WuWaRussian.exe': b'v2', 'seed/Data/settings.json': b'{}'}, '0.2.0')
        self.register = mock.patch.object(core, 'register_uninstall').start()
        self.addCleanup(mock.patch.stopall)

    def run_update(self):
        with mock.patch.object(ui.subprocess, 'Popen') as popen:
            wizard = ui.Wizard(self.payload, None, core.update_target(str(self.target)), 0)
            self.addCleanup(wizard.close)
            wizard.show()
            self.assertEqual(wizard.stack.currentIndex(), ui.PROGRESS_PAGE)
            self.assertIn('Обновление', wizard.windowTitle())
            self.assertFalse(wizard.cancel_btn.isVisible())
            loop, timer = QEventLoop(), QTimer()
            timer.timeout.connect(lambda: loop.quit() if wizard.worker and not wizard.worker.isRunning() else None)
            timer.start(10)
            QTimer.singleShot(10000, loop.quit)
            loop.exec()
            timer.stop()
            self.app.processEvents()
        return wizard, popen

    def test_update_installs_over_keeps_data_and_reopens_launcher(self):
        wizard, popen = self.run_update()
        self.assertEqual((self.target / 'WuWaRussian.exe').read_bytes(), b'v2')
        self.assertEqual((self.target / 'Data/settings.json').read_text(), '{"mine": 1}')
        self.assertEqual(core.read_manifest(self.target)['version'], '0.2.0')
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [str(self.target / 'WuWaRussian.exe')])
        self.assertFalse(wizard.isVisible())

    def test_failed_update_shows_reason_and_does_not_launch(self):
        with mock.patch.object(core, 'install', side_effect=core.InstallError('Файл занят')):
            wizard, popen = self.run_update()
        self.assertEqual(wizard.stack.currentIndex(), ui.DONE_PAGE)
        self.assertIn('Файл занят', wizard.done_text.text())
        self.assertEqual(wizard.done_title.text(), 'Обновить не удалось')
        popen.assert_not_called()

    def test_launcher_that_does_not_exit_blocks_the_update(self):
        with mock.patch.object(core, 'wait_for_exit', return_value=False):
            wizard = ui.Wizard(self.payload, None, self.target, 4242)
            self.addCleanup(wizard.close)
            loop, timer = QEventLoop(), QTimer()
            timer.timeout.connect(lambda: loop.quit() if wizard.worker and not wizard.worker.isRunning() else None)
            timer.start(10)
            QTimer.singleShot(10000, loop.quit)
            loop.exec()
            timer.stop()
            self.app.processEvents()
        self.assertEqual(wizard.stack.currentIndex(), ui.DONE_PAGE)
        self.assertIn('не закрылся', wizard.done_text.text())
        self.assertEqual((self.target / 'WuWaRussian.exe').read_bytes(), b'v1')


if __name__ == '__main__':
    unittest.main()
