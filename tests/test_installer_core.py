import json
import shutil
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from installer import core

TMP = Path(__file__).resolve().parents[1] / '.test-tmp' / 'installer'


def make_zip(path: Path, files: dict[str, bytes], version='1.2.3'):
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('payload.json', json.dumps({'version': version}))
        for k, v in files.items():
            zf.writestr(k, v)


def isolate_system(case):
    """uninstall() also removes shortcuts and the HKCU entry: never the real ones in tests."""
    for name, value in (('desktop_dir', TMP / 'Desktop'), ('start_menu_dir', TMP / 'StartMenu')):
        patcher = mock.patch.object(core, name, return_value=value)
        patcher.start()
        case.addCleanup(patcher.stop)
    import winreg
    patcher = mock.patch.object(winreg, 'DeleteKey')
    case.deleted_keys = patcher.start()
    case.addCleanup(patcher.stop)


class InstallerCoreTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        isolate_system(self)

    def test_uninstall_reports_progress_and_keeps_data_by_default(self):
        z = TMP / 'p.zip'
        make_zip(z, {'WuWaRussian.exe': b'x', 'lib/a.dll': b'y'})
        target = TMP / 'app'
        core.install(z, target)
        (target / 'Data/Native').mkdir(parents=True)
        (target / 'Data/Native/installed.json').write_text('{}')
        (TMP / 'Desktop').mkdir()
        (TMP / 'Desktop' / f'{core.APP_NAME}.lnk').write_bytes(b'lnk')
        self.assertTrue(core.translation_in_game(target))
        seen = []
        self.assertEqual(core.uninstall(target, lambda d, t, n: seen.append((d, t))), 2)
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))
        self.assertTrue((target / 'Data/Native/installed.json').is_file())
        self.assertFalse((TMP / 'Desktop' / f'{core.APP_NAME}.lnk').exists())
        self.deleted_keys.assert_called_once()

    def test_uninstall_with_data_removes_it_but_never_follows_a_junction(self):
        z = TMP / 'p.zip'
        make_zip(z, {'WuWaRussian.exe': b'x'})
        target = TMP / 'app'
        core.install(z, target)
        (target / 'Data/Projects/RU').mkdir(parents=True)
        (target / 'Data/Projects/RU/translations.sqlite').write_bytes(b'db')
        outside = TMP / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('keep')
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(target / 'Backups'), str(outside)],
                              capture_output=True)
        self.assertEqual(made.returncode, 0, made.stderr)
        core.uninstall(target, remove_data=True)
        self.assertFalse((target / 'Data').exists())
        self.assertEqual((outside / 'keep.txt').read_text(), 'keep')

    def test_uninstaller_is_found_by_name_and_cleans_its_own_folder(self):
        self.assertTrue(core.is_uninstaller(Path('C:/x/Uninstall.exe')))
        self.assertTrue(core.is_uninstaller(Path('C:/x/UNINSTALL.EXE')))
        self.assertFalse(core.is_uninstaller(Path('C:/x/WuWaRussian-Setup-0.1.0.exe')))
        target = TMP / 'app'
        target.mkdir()
        (target / core.MANIFEST).write_text('{}')
        self.assertEqual(core.uninstall_target(target / 'Uninstall.exe', True), target)
        with mock.patch.object(core, 'installed_location', return_value=None):
            self.assertIsNone(core.uninstall_target(TMP / 'elsewhere' / 'Uninstall.exe', True))

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_seed_data_is_written_once_and_never_owned(self):
        z = TMP / 'p.zip'
        make_zip(z, {'WuWaRussian.exe': b'x', 'seed/Data/Projects/RU/translations.sqlite': b'ready',
                     'seed/Data/settings.json': b'{}'})
        target = TMP / 'app'
        (target / 'Data').mkdir(parents=True)
        (target / 'Data/settings.json').write_bytes(b'mine')
        m = core.install(z, target)
        self.assertEqual((target / 'Data/Projects/RU/translations.sqlite').read_bytes(), b'ready')
        self.assertEqual((target / 'Data/settings.json').read_bytes(), b'mine')
        self.assertNotIn('Data/Projects/RU/translations.sqlite', m['files'])
        self.assertFalse((target / 'seed').exists())
        (target / 'Data/Projects/RU/translations.sqlite').write_bytes(b'edited')
        core.install(z, target)  # upgrade keeps the user's edits
        self.assertEqual((target / 'Data/Projects/RU/translations.sqlite').read_bytes(), b'edited')
        core.uninstall(target)
        self.assertEqual((target / 'Data/Projects/RU/translations.sqlite').read_bytes(), b'edited')

    def test_failed_install_removes_fresh_seed(self):
        z = TMP / 'p.zip'
        make_zip(z, {'seed/Data/a.db': b'a', 'WuWaRussian.exe': b'x'})
        target = TMP / 'app'
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 1
        with self.assertRaises(core.Cancelled):
            core.install(z, target, is_cancelled=cancel)
        self.assertFalse((target / 'Data/a.db').exists())

    def test_install_and_uninstall_keeps_user_data(self):
        z = TMP / 'p.zip'
        make_zip(z, {'WuWaRussian.exe': b'x', 'lib/a.dll': b'y'})
        target = TMP / 'app'
        seen = []
        m = core.install(z, target, lambda d, t, n: seen.append((d, t)))
        self.assertEqual(m['version'], '1.2.3')
        self.assertTrue((target / 'lib/a.dll').is_file())
        self.assertEqual(seen[-1][0], seen[-1][1])
        (target / 'Data').mkdir()
        (target / 'Data/mine.db').write_bytes(b'user')
        core.uninstall(target)
        self.assertFalse((target / 'WuWaRussian.exe').exists())
        self.assertEqual((target / 'Data/mine.db').read_bytes(), b'user')

    def test_upgrade_removes_only_old_app_files(self):
        z1, z2 = TMP / 'a.zip', TMP / 'b.zip'
        make_zip(z1, {'WuWaRussian.exe': b'1', 'old.dll': b'o'})
        make_zip(z2, {'WuWaRussian.exe': b'2'}, '2.0')
        t = TMP / 'app'
        core.install(z1, t)
        (t / 'user.txt').write_text('keep')
        core.install(z2, t)
        self.assertFalse((t / 'old.dll').exists())
        self.assertTrue((t / 'user.txt').exists())
        self.assertEqual((t / 'WuWaRussian.exe').read_bytes(), b'2')

    def test_traversal_rejected_and_rolled_back(self):
        z = TMP / 'bad.zip'
        make_zip(z, {'ok.txt': b'1', '../evil.txt': b'2'})
        t = TMP / 'app'
        with self.assertRaises(core.InstallError):
            core.install(z, t)
        self.assertFalse((TMP / 'evil.txt').exists())
        self.assertFalse((t / 'ok.txt').exists())

    def test_cancel_rolls_back(self):
        z = TMP / 'p.zip'
        make_zip(z, {'a': b'1', 'b': b'2'})
        t = TMP / 'app'
        with self.assertRaises(core.Cancelled):
            core.install(z, t, is_cancelled=lambda: True)
        self.assertFalse((t / 'a').exists())

    def test_resolve_target(self):
        with self.assertRaises(core.InstallError):
            core.resolve_target('relative/dir')
        with self.assertRaises(core.InstallError):
            core.resolve_target('C:\\')
        busy = TMP / 'busy'
        busy.mkdir()
        (busy / 'foreign.txt').write_text('x')
        self.assertEqual(core.resolve_target(str(busy)), busy / core.DEFAULT_SUBDIR)


if __name__ == '__main__':
    unittest.main()


class UpdateModeCoreTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, TMP, True)

    def test_update_target_must_be_an_existing_installation(self):
        target = TMP / 'app'
        with self.assertRaises(core.InstallError):
            core.update_target(str(target))
        target.mkdir()
        (target / 'WuWaRussian.exe').write_bytes(b'x')
        with self.assertRaises(core.InstallError):
            core.update_target(str(target))
        (target / core.MANIFEST).write_text(json.dumps({'app': 'Something else', 'files': []}))
        with self.assertRaises(core.InstallError):
            core.update_target(str(target))
        (target / core.MANIFEST).write_text(json.dumps({'app': core.APP_NAME, 'files': []}))
        self.assertEqual(core.update_target(str(target)), target.resolve())
        self.assertEqual(core.update_target(f'"{target}"'), target.resolve())

    def test_update_over_installation_keeps_user_data_and_seed(self):
        target = TMP / 'app'
        old = TMP / 'old.zip'
        make_zip(old, {'WuWaRussian.exe': b'v1', 'lib/old.dll': b'o', 'seed/Data/settings.json': b'{}'}, '0.1.0')
        core.install(old, target)
        (target / 'Data/settings.json').write_text('{"mine": 1}')
        new = TMP / 'new.zip'
        make_zip(new, {'WuWaRussian.exe': b'v2', 'lib/new.dll': b'n', 'seed/Data/settings.json': b'{}'}, '0.2.0')
        manifest = core.install(new, core.update_target(str(target)))
        self.assertEqual(manifest['version'], '0.2.0')
        self.assertEqual((target / 'WuWaRussian.exe').read_bytes(), b'v2')
        self.assertFalse((target / 'lib/old.dll').exists())
        self.assertEqual((target / 'Data/settings.json').read_text(), '{"mine": 1}')

    def test_wait_for_exit_returns_for_finished_and_running_processes(self):
        done = subprocess.Popen([sys.executable, '-c', 'pass'])
        done.wait()
        self.assertTrue(core.wait_for_exit(done.pid, 2))
        running = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(running.wait)
        self.addCleanup(running.kill)
        self.assertFalse(core.wait_for_exit(running.pid, 0.2))
        self.assertTrue(core.wait_for_exit(0, 0.1))
