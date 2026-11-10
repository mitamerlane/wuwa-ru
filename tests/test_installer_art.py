"""Installer background: key art lookup at build time and cropping in the window."""
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import build_installer  # noqa: E402
from installer import ui  # noqa: E402

TMP = ROOT / '.test-tmp' / 'installer-art'


class KeyArtLookupTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        self.cache = TMP / 'Steam' / 'appcache' / 'librarycache' / build_installer.STEAM_APP_ID
        self.cache.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def _hero(self, sub: str, mtime: float, size: int = 10) -> Path:
        path = self.cache / sub / 'library_hero.jpg'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * size)
        os.utime(path, (mtime, mtime))
        return path

    def test_newest_hero_wins(self):
        now = time.time()
        self._hero('old', now - 100)
        new = self._hero('new', now)
        self.assertEqual(build_installer.find_key_art([TMP / 'Steam']), new)

    def test_oversized_or_missing_is_ignored(self):
        self._hero('big', time.time(), build_installer.ART_LIMIT + 1)
        self.assertIsNone(build_installer.find_key_art([TMP / 'Steam']))
        self.assertIsNone(build_installer.find_key_art([TMP / 'nowhere']))

    def test_no_art_means_no_embedded_file(self):
        out = TMP / 'background.jpg'
        with mock.patch.object(build_installer, 'steam_roots', return_value=[TMP / 'none']):
            self.assertIsNone(build_installer.stage_background(None, out))
        self.assertFalse(out.exists())


class CoverCropTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cover_fills_window_from_focus(self):
        img = QImage(1920, 620, QImage.Format_RGB32)
        img.fill(QColor('black'))
        for x in range(0, 150):  # red stripe left of the crop anchor (~180 px)
            for y in range(620):
                img.setPixelColor(x, y, QColor('red'))
        pm = ui.cover(img, ui.W, ui.H)
        self.assertEqual((pm.width(), pm.height()), (ui.W, ui.H))
        self.assertNotEqual(pm.toImage().pixelColor(0, 10), QColor('red'))

    def test_missing_background_falls_back(self):
        with mock.patch.object(ui.core, 'resource_path', return_value=TMP / 'none.jpg'):
            pm, real = ui.load_art()
        self.assertFalse(real)
        self.assertEqual((pm.width(), pm.height()), (ui.W, ui.H))


if __name__ == '__main__':
    unittest.main()
