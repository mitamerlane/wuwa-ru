"""Uninstall wizard, offscreen: confirm → progress → done; data stays unless asked. Functional only."""
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from installer import core, ui  # noqa: E402
from tests.test_installer_core import TMP, isolate_system  # noqa: E402


class UninstallWizardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        self.target = TMP / 'app'
        (self.target / 'Data/Native').mkdir(parents=True)
        (self.target / 'WuWaRussian.exe').write_bytes(b'x')
        (self.target / 'Data/settings.json').write_text('{}')
        (self.target / core.MANIFEST).write_text(json.dumps({'files': ['WuWaRussian.exe']}))
        isolate_system(self)
        self.addCleanup(shutil.rmtree, TMP, True)

    def run_wizard(self, remove_data=False, in_game=False):
        if in_game:
            (self.target / 'Data/Native/installed.json').write_text('{}')
        wizard = ui.Wizard(TMP / 'none.zip', self.target)
        self.addCleanup(wizard.close)
        wizard.show()
        self.assertEqual(wizard.stack.currentIndex(), ui.UNINSTALL_PAGE)
        self.assertEqual(wizard.un_warn.isVisible(), in_game)
        self.assertIn('Удаление', wizard.windowTitle())
        folder = Path(__file__).resolve().parents[1] / '.test-tmp' / 'screens'
        folder.mkdir(parents=True, exist_ok=True)
        wizard.grab().save(str(folder / f"uninstall-{'game' if in_game else 'plain'}.png"))
        wizard.cb_data.setChecked(remove_data)
        wizard.un_btn.click()
        loop, timer = QEventLoop(), QTimer()
        timer.timeout.connect(lambda: loop.quit() if not wizard.worker.isRunning() else None)
        timer.start(10)
        QTimer.singleShot(10000, loop.quit)
        loop.exec()
        timer.stop()
        self.app.processEvents()
        return wizard

    def test_confirm_progress_done_keeps_data(self):
        wizard = self.run_wizard(in_game=True)
        self.assertEqual(wizard.stack.currentIndex(), ui.UNINSTALL_DONE)
        self.assertEqual(wizard.un_bar.value(), 1000)
        self.assertFalse((self.target / 'WuWaRussian.exe').exists())
        self.assertTrue((self.target / 'Data/settings.json').exists())
        self.assertIn('остались', wizard.un_done_text.text())

    def test_remove_data_on_request(self):
        wizard = self.run_wizard(remove_data=True)
        self.assertEqual(wizard.stack.currentIndex(), ui.UNINSTALL_DONE)
        self.assertFalse(self.target.exists())

    def test_failure_returns_to_confirm_with_message(self):
        with mock.patch.object(core, 'uninstall', side_effect=core.InstallError('Файл занят')):
            wizard = self.run_wizard()
        self.assertEqual(wizard.stack.currentIndex(), ui.UNINSTALL_PAGE)
        self.assertEqual(wizard.un_msg.text(), 'Файл занят')


if __name__ == '__main__':
    unittest.main()
