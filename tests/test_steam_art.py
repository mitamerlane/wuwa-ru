import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wuwaru.steam_art import find_hero

APP = '3513350'


class SteamArtTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.test-tmp')
        self.addCleanup(self.temp.cleanup)
        self.steam = Path(self.temp.name) / 'Steam'
        self.cache = self.steam / 'appcache' / 'librarycache' / APP
        self.cache.mkdir(parents=True)

    def hero(self, folder: str, mtime: int, size: int = 16) -> Path:
        path = self.cache / folder / 'library_hero.jpg'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\xff\xd8' + b'0' * (size - 2))
        os.utime(path, (mtime, mtime))
        return path

    def test_picks_newest(self):
        self.hero('a' * 40, 1_000_000)
        newest = self.hero('b' * 40, 3_000_000)
        self.hero('c' * 40, 2_000_000)
        self.assertEqual(find_hero([self.steam]), newest)

    def test_skips_oversized(self):
        self.hero('a' * 40, 1_000_000)
        self.hero('b' * 40, 3_000_000, size=64)
        self.assertEqual(find_hero([self.steam], limit=32), self.cache / ('a' * 40) / 'library_hero.jpg')

    def test_none_without_cache(self):
        self.assertIsNone(find_hero([Path(self.temp.name) / 'missing']))
        self.assertIsNone(find_hero([self.steam]))
        self.assertIsNone(find_hero([]))

    def test_ignores_deeper_files(self):
        deep = self.cache / 'x' / 'y' / 'library_hero.jpg'
        deep.parent.mkdir(parents=True)
        deep.write_bytes(b'\xff\xd8')
        self.assertIsNone(find_hero([self.steam]))

    def test_skips_directory_junction(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'library_hero.jpg').write_bytes(b'\xff\xd8')
        real = self.hero('a' * 40, 1_000_000)
        link = self.cache / ('b' * 40)
        if sys.platform == 'win32':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            self.assertEqual(find_hero([self.steam]), real)
        finally:
            if sys.platform == 'win32':
                link.rmdir()
            else:
                link.unlink()

    def test_skips_symlink(self):
        target = Path(self.temp.name) / 'target.jpg'
        target.write_bytes(b'\xff\xd8')
        real = self.hero('a' * 40, 1_000_000)
        folder = self.cache / ('b' * 40)
        folder.mkdir()
        try:
            (folder / 'library_hero.jpg').symlink_to(target)
        except OSError as error:
            self.skipTest(f'file symlink needs privileges here ({error}); directory case is test_skips_directory_junction')
        os.utime(target, (3_000_000, 3_000_000))
        self.assertEqual(find_hero([self.steam]), real)


if __name__ == '__main__':
    unittest.main()
