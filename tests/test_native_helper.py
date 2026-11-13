import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.test_discovery import FixtureCase, fixture
from tests.test_native_launch import CHANGED, mount_bytes
from wuwaru import native_helper, native_launch
from wuwaru.native_package import sha256

PAK = b'synthetic russian pak'
SHA1 = hashlib.sha1(PAK).hexdigest().upper()
ENTRY = f'{native_launch.MOUNT_STEM},900,{SHA1},,,'


def journal(native):
    return [json.loads(line) for line in (native / 'journal.jsonl').read_text(encoding='utf-8').splitlines()]


def with_ours(mount):
    return native_launch.with_entry(mount, ENTRY)


class HelperCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.game = fixture(self.home / 'game')
        self.mount = self.game / native_launch.MOUNT
        self.mount.write_bytes(mount_bytes())
        self.native = self.home / 'Data Folder' / 'Native'
        (self.native / 'staging').mkdir(parents=True)
        self.pak = self.game / native_launch.PAK_RELATIVE
        self.pak.parent.mkdir(parents=True)
        self.staging = self.native / 'staging' / 'k1.pak'
        self.staging.write_bytes(PAK)
        self.package = {'schema_version': 1, 'key': 'k1', 'pak_sha256': sha256(self.staging),
                        'pak_sha1': SHA1, 'pak_bytes': len(PAK)}
        (self.native / 'package.json').write_text(json.dumps(self.package), encoding='utf-8')

    def state(self, **kwargs):
        kwargs.setdefault('running', lambda game: [])
        kwargs.setdefault('guard', lambda game: None)
        return native_launch.inspect(self.game, self.native, 'k1', **kwargs)

    def request(self, plan, game=None):
        return native_helper.write_request(self.native, game or self.game, plan, self.mount.read_bytes())

    def execute(self, request, **kwargs):
        kwargs.setdefault('discover_roots', lambda: [self.game.resolve()])
        kwargs.setdefault('running', lambda game: [])
        kwargs.setdefault('guard', lambda game: None)
        return native_helper.execute(request, **kwargs)

    def raw_request(self, payload):
        requests = self.native / 'requests'
        requests.mkdir(exist_ok=True)
        path = requests / '00000000-0000-4000-8000-000000000001.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def result(self, request):
        return json.loads(request.with_name(request.stem + '.result.json').read_text(encoding='utf-8'))

    def pak_folder(self):
        return sorted(p.name for p in self.pak.parent.iterdir())


class ExecuteTests(HelperCase):
    def test_apply_mount_writes_entry_and_journal(self):
        self.pak.write_bytes(PAK)
        plan = native_launch.plan_play(self.state(), self.native)
        self.assertEqual([op['op'] for op in plan], ['apply_mount'])
        request = self.request(plan)
        self.assertEqual(request.parent, self.native / 'requests')
        stored = json.loads(request.read_text(encoding='utf-8'))
        self.assertEqual(stored['schema_version'], 1)
        result = self.execute(request)
        self.assertTrue(result['ok'])
        self.assertIsNone(result['error'])
        self.assertEqual(self.mount.read_bytes(), with_ours(mount_bytes()))
        self.assertEqual(self.result(request), result)
        self.assertEqual([op['op'] for op in result['operations']], ['apply_mount'])
        events = journal(self.native)
        self.assertEqual(events[-1]['request'], request.stem)
        self.assertTrue(events[-1]['ok'])
        self.assertEqual(sorted(p.name for p in self.mount.parent.iterdir()),
                         ['MountLang_en.txt', 'MountResource.txt'])

    def test_write_request_keeps_original_mount(self):
        self.pak.write_bytes(PAK)
        self.mount.write_bytes(with_ours(mount_bytes()))
        plan = native_launch.plan_play(self.state(), self.native)
        self.request(plan)
        original = self.native / 'mount-original.txt'
        self.assertEqual(original.read_bytes(), mount_bytes())
        stamp = original.stat().st_mtime_ns
        self.request(plan)
        self.assertEqual(original.stat().st_mtime_ns, stamp)
        with self.assertRaises(ValueError):  # the backup must be the Mount the plan was made from
            native_helper.write_request(self.native, self.game, plan, mount_bytes(first=CHANGED))

    def test_install_then_mount_copies_and_keeps_staging(self):
        self.pak.write_bytes(b'old pak')
        plan = native_launch.plan_play(self.state(), self.native)
        self.assertEqual([op['op'] for op in plan], ['install_package', 'apply_mount'])
        result = self.execute(self.request(plan))
        self.assertTrue(result['ok'])
        self.assertEqual([op['op'] for op in result['operations']], ['install_package', 'apply_mount'])
        self.assertEqual(self.pak.read_bytes(), PAK)
        self.assertEqual(self.pak.stat().st_nlink, 1)
        self.assertEqual(self.staging.read_bytes(), PAK)  # the helper never deletes staging
        self.assertEqual(self.mount.read_bytes(), with_ours(mount_bytes()))
        self.assertEqual(self.pak_folder(), [native_launch.PAK_NAME])

    def test_refuses_when_game_started_after_plan(self):
        self.pak.write_bytes(b'old pak')
        request = self.request(native_launch.plan_play(self.state(), self.native))
        before = self.mount.read_bytes()
        with self.assertRaises(RuntimeError):
            self.execute(request, running=lambda game: [{'Name': 'Client-Win64-Shipping.exe'}])
        self.assertEqual(self.mount.read_bytes(), before)
        self.assertEqual(self.pak.read_bytes(), b'old pak')
        result = self.result(request)
        self.assertFalse(result['ok'])
        self.assertEqual(result['operations'], [])
        self.assertFalse(journal(self.native)[-1]['ok'])

    def test_game_started_between_install_and_mount_is_reported(self):
        self.pak.write_bytes(b'old pak')
        request = self.request(native_launch.plan_play(self.state(), self.native))
        calls = []

        def running(game):
            calls.append(game)
            return [{'Name': 'Client-Win64-Shipping.exe'}] if len(calls) >= 3 else []
        with self.assertRaises(RuntimeError):
            self.execute(request, running=running)
        self.assertEqual(self.pak.read_bytes(), PAK)
        self.assertEqual(self.mount.read_bytes(), mount_bytes())
        result = self.result(request)
        self.assertFalse(result['ok'])
        self.assertEqual([op['op'] for op in result['operations']], ['install_package'])
        self.assertIn('install_package', [e.get('op') for e in journal(self.native)])

    def test_default_inventory_ignores_foreign_launchers_and_names_game_processes(self):
        self.pak.write_bytes(PAK)
        foreign = {'ProcessId': 1, 'Name': 'EpicGamesLauncher.exe',
                   'ExecutablePath': r'C:\Program Files (x86)\Epic Games\Launcher\EpicGamesLauncher.exe'}
        request = self.request(native_launch.plan_play(self.state(), self.native))
        with mock.patch.object(native_launch, 'consumers', return_value=[foreign]):
            result = native_helper.execute(request, discover_roots=lambda: [self.game.resolve()],
                                           guard=lambda game: None)
        self.assertTrue(result['ok'])
        self.mount.write_bytes(mount_bytes())
        request = self.request(native_launch.plan_play(self.state(), self.native))
        game = {'ProcessId': 2, 'Name': 'Wuthering Waves.exe', 'ExecutablePath': str(self.game / 'Wuthering Waves.exe')}
        with mock.patch.object(native_launch, 'consumers', return_value=[foreign, game]):
            with self.assertRaises(RuntimeError) as raised:
                native_helper.execute(request, discover_roots=lambda: [self.game.resolve()],
                                      guard=lambda game: None)
        self.assertIn('(Wuthering Waves.exe)', str(raised.exception))
        self.assertNotIn('Epic', str(raised.exception))
        self.assertNotIn(str(self.game), self.result(request)['error'])
        self.assertEqual(self.mount.read_bytes(), mount_bytes())

    def test_guard_failure_refuses(self):
        self.pak.write_bytes(PAK)
        request = self.request(native_launch.plan_play(self.state(), self.native))

        def updating(game):
            raise ValueError('Active or pending Steam update')
        with self.assertRaises(ValueError):
            self.execute(request, guard=updating)
        self.assertEqual(self.mount.read_bytes(), mount_bytes())

    def test_refuses_unknown_operation_and_foreign_root(self):
        self.pak.write_bytes(PAK)
        good = native_launch.plan_play(self.state(), self.native)
        before = self.mount.read_bytes()
        bad_payloads = [
            {'schema_version': 1, 'game': str(self.game), 'operations': [{'op': 'delete_tree', 'path': 'C:/'}]},
            {'schema_version': 2, 'game': str(self.game), 'operations': good},
            {'schema_version': True, 'game': str(self.game), 'operations': good},
            {'schema_version': 1, 'game': str(self.game), 'operations': good, 'extra': 1},
            {'schema_version': 1, 'game': str(self.game), 'operations': [dict(good[0], extra='x')]},
            {'schema_version': 1, 'game': str(self.game),
             'operations': [dict(good[0], entry='Lang_en/Base/pakchunk10-WindowsNoEditor,900,' + SHA1 + ',,,')]},
            {'schema_version': 1, 'game': str(self.game),
             'operations': [dict(good[0], entry=ENTRY + '\n::Mount::')]},
            {'schema_version': 1, 'game': str(self.game), 'operations': good + good},
            {'schema_version': 1, 'game': str(self.game), 'operations': []},
            {'schema_version': 1, 'game': 'game', 'operations': good},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                request = self.raw_request(payload)
                request.with_name(request.stem + '.result.json').unlink(missing_ok=True)
                with self.assertRaises(ValueError):
                    self.execute(request)
                self.assertEqual(self.mount.read_bytes(), before)
                self.assertFalse(self.result(request)['ok'])
        with self.assertRaises(ValueError):
            native_helper.write_request(self.native, self.game, [{'op': 'delete_tree'}], before)
        foreign = fixture(self.home / 'other game')
        (foreign / native_launch.MOUNT).write_bytes(mount_bytes())
        request = self.request(good, game=foreign)
        with self.assertRaises(ValueError):
            self.execute(request)
        self.assertEqual((foreign / native_launch.MOUNT).read_bytes(), mount_bytes())
        self.assertFalse(self.result(request)['ok'])

    def test_refuses_changed_mount_and_unaccepted_version(self):
        self.pak.write_bytes(PAK)
        plan = native_launch.plan_play(self.state(), self.native)
        request = self.request(plan)
        changed = mount_bytes(eol='\r\n')
        self.mount.write_bytes(changed)
        with self.assertRaises(ValueError):
            self.execute(request)
        self.assertEqual(self.mount.read_bytes(), changed)
        other = mount_bytes(first=CHANGED)
        self.mount.write_bytes(other)
        plan[0]['expected_sha256'] = sha256(self.mount)
        request = self.request(plan)
        with self.assertRaises(ValueError):
            self.execute(request)
        self.assertEqual(self.mount.read_bytes(), other)

    def test_refuses_mount_entry_for_different_pak(self):
        self.pak.write_bytes(b'not the package the entry names')
        plan = native_launch.plan_play(self.state(), self.native)
        with self.assertRaises(ValueError):
            self.execute(self.request(plan[-1:]))
        self.assertEqual(self.mount.read_bytes(), mount_bytes())

    def test_refuses_staging_outside_native_staging(self):
        self.pak.write_bytes(b'old pak')
        outside = self.home / 'elsewhere.pak'
        outside.write_bytes(PAK)
        plan = native_launch.plan_play(self.state(), self.native)
        for path in (outside, self.native / 'staging' / '..' / 'package.json'):
            with self.subTest(path=path):
                plan[0]['staging'] = str(path)
                with self.assertRaises(ValueError):
                    self.execute(self.request(plan))
                self.assertEqual(self.pak.read_bytes(), b'old pak')
                self.assertTrue(outside.exists())

    def test_refuses_staging_hash_mismatch(self):
        self.pak.write_bytes(b'old pak')
        plan = native_launch.plan_play(self.state(), self.native)
        self.staging.write_bytes(b'tampered after plan')
        with self.assertRaises(ValueError):
            self.execute(self.request(plan))
        self.assertEqual(self.pak.read_bytes(), b'old pak')
        self.assertEqual(self.mount.read_bytes(), mount_bytes())

    def test_refuses_hardlinked_staging(self):
        self.pak.write_bytes(b'old pak')
        plan = native_launch.plan_play(self.state(), self.native)
        os.link(self.staging, self.home / 'user-copy.pak')
        with self.assertRaises(ValueError):
            self.execute(self.request(plan))
        self.assertEqual(self.pak.read_bytes(), b'old pak')
        self.assertEqual(self.mount.read_bytes(), mount_bytes())
        self.assertEqual(self.pak_folder(), [native_launch.PAK_NAME])

    def test_refuses_junction_staging(self):
        self.pak.write_bytes(b'old pak')
        plan = native_launch.plan_play(self.state(), self.native)
        real = self.native / 'real-staging'
        (self.native / 'staging').rename(real)
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.native / 'staging'), str(real)],
                              capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        with self.assertRaises(ValueError):
            self.execute(self.request(plan))
        self.assertEqual(self.pak.read_bytes(), b'old pak')
        self.assertTrue((real / 'k1.pak').exists())

    def test_staging_swapped_for_hardlink_after_validation(self):
        """The swap happens during the second process inventory, after staging was opened."""
        self.pak.write_bytes(b'old pak')
        request = self.request(native_launch.plan_play(self.state(), self.native))
        user_file = self.home / 'user-controlled.pak'
        user_file.write_bytes(PAK)
        calls, swap = [], {}

        def running(game):
            calls.append(game)
            if len(calls) == 2:
                link = self.home / 'link.pak'
                os.link(user_file, link)
                try:
                    os.replace(link, self.staging)
                    swap['replaced'] = True
                except PermissionError:  # the open handle denies delete/rename on Windows
                    swap['replaced'] = False
                    link.unlink()
            return []
        result = self.execute(request, running=running)
        self.assertIn('replaced', swap)
        self.assertTrue(result['ok'])
        self.assertEqual(self.pak.stat().st_nlink, 1)
        user_file.write_bytes(b'changed later by the user')
        self.assertEqual(self.pak.read_bytes(), PAK)

    def test_staging_rewritten_in_place_after_validation(self):
        self.pak.write_bytes(b'old pak')
        request = self.request(native_launch.plan_play(self.state(), self.native))
        calls = []

        def running(game):
            calls.append(game)
            if len(calls) == 2:
                with self.staging.open('r+b') as stream:
                    stream.write(b'EVIL')
            return []
        with self.assertRaises(ValueError):
            self.execute(request, running=running)
        self.assertEqual(self.pak.read_bytes(), b'old pak')
        self.assertEqual(self.mount.read_bytes(), mount_bytes())
        self.assertEqual(self.pak_folder(), [native_launch.PAK_NAME])

    def test_install_cross_volume_verifies_hash(self):
        """No rename at all now: a corrupted copy is refused, the old PAK and staging stay."""
        self.pak.write_bytes(b'old pak')
        request = self.request(native_launch.plan_play(self.state(), self.native))

        def corrupted(source, target):
            os.write(target, b'corrupted copy')
        with mock.patch('os.rename', side_effect=AssertionError('rename must not be used')), \
                mock.patch.object(native_helper, '_copy', side_effect=corrupted):
            with self.assertRaises(ValueError):
                self.execute(request)
        self.assertEqual(self.pak.read_bytes(), b'old pak')
        self.assertEqual(self.staging.read_bytes(), PAK)
        self.assertEqual(self.mount.read_bytes(), mount_bytes())
        self.assertEqual(self.pak_folder(), [native_launch.PAK_NAME])
        self.assertFalse(self.result(request)['ok'])

    def test_failed_replace_deletes_only_temp(self):
        self.pak.write_bytes(b'old pak')
        request = self.request(native_launch.plan_play(self.state(), self.native))
        with mock.patch('os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                self.execute(request)
        self.assertEqual(self.pak.read_bytes(), b'old pak')
        self.assertEqual(self.staging.read_bytes(), PAK)
        self.assertEqual(self.pak_folder(), [native_launch.PAK_NAME])

    def test_request_runs_once(self):
        self.pak.write_bytes(PAK)
        request = self.request(native_launch.plan_play(self.state(), self.native))
        first = self.execute(request)
        self.assertTrue(first['ok'])
        self.mount.write_bytes(mount_bytes())
        with self.assertRaises(ValueError):
            self.execute(request)
        self.assertEqual(self.mount.read_bytes(), mount_bytes())
        self.assertEqual(self.result(request), first)

    def test_refuses_hardlinked_journal(self):
        self.pak.write_bytes(PAK)
        request = self.request(native_launch.plan_play(self.state(), self.native))
        victim = self.home / 'victim.txt'
        victim.write_bytes(b'not a journal')
        os.link(victim, self.native / 'journal.jsonl')
        with self.assertRaises(ValueError):
            self.execute(request)
        self.assertEqual(victim.read_bytes(), b'not a journal')
        self.assertEqual(self.mount.read_bytes(), mount_bytes())

    def redirect(self, suffix):
        """Simulates a junction swapped in between the link check and the open."""
        real = native_helper._final_path

        def final_path(descriptor):
            path = real(descriptor)
            return 'C:\\Windows\\System32\\redirected' + suffix if path.endswith(suffix) else path
        return mock.patch.object(native_helper, '_final_path', side_effect=final_path)

    def test_refuses_redirected_journal(self):
        self.pak.write_bytes(PAK)
        request = self.request(native_launch.plan_play(self.state(), self.native))
        with self.redirect('journal.jsonl'):
            with self.assertRaises(ValueError):
                self.execute(request)
        self.assertEqual(self.mount.read_bytes(), mount_bytes())
        self.assertEqual((self.native / 'journal.jsonl').read_bytes(), b'')

    def test_refuses_redirected_result_and_request(self):
        self.pak.write_bytes(PAK)
        request = self.request(native_launch.plan_play(self.state(), self.native))
        with self.redirect('.result.json'):
            with self.assertRaises(ValueError):
                self.execute(request)
        self.assertEqual(request.with_name(request.stem + '.result.json').read_bytes(), b'')
        self.assertEqual(self.mount.read_bytes(), mount_bytes())  # reserved before any game write
        request = self.request(native_launch.plan_play(self.state(), self.native))
        with self.redirect(request.name):
            with self.assertRaises(ValueError):
                self.execute(request)
        self.assertEqual(self.mount.read_bytes(), mount_bytes())

    def test_refuses_junction_requests_folder(self):
        self.pak.write_bytes(PAK)
        request = self.request(native_launch.plan_play(self.state(), self.native))
        real = self.native / 'real-requests'
        (self.native / 'requests').rename(real)
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.native / 'requests'), str(real)],
                              capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        with self.assertRaises(ValueError):
            self.execute(request)
        self.assertEqual(self.mount.read_bytes(), mount_bytes())
        self.assertEqual(sorted(p.name for p in real.iterdir()), [request.name])

    def test_remove_only_our_pak(self):
        self.mount.write_bytes(with_ours(mount_bytes()))
        self.pak.write_bytes(b'someone else, same name')
        plan = [{'op': 'remove', 'pak_sha256': self.package['pak_sha256'],
                 'expected_sha256': sha256(self.mount)}]
        with self.assertRaises(ValueError):
            self.execute(self.request(plan))
        self.assertEqual(self.pak.read_bytes(), b'someone else, same name')
        self.assertEqual(self.mount.read_bytes(), with_ours(mount_bytes()))

    def test_remove_hash_must_come_from_package_json(self):
        self.mount.write_bytes(with_ours(mount_bytes()))
        self.pak.write_bytes(b'someone else, same name')
        named = [{'op': 'remove', 'pak_sha256': sha256(self.pak), 'expected_sha256': sha256(self.mount)}]
        with self.assertRaises(ValueError):
            self.execute(self.request(named))
        (self.native / 'package.json').unlink()
        with self.assertRaises(ValueError):
            self.execute(self.request(named))
        self.assertEqual(self.pak.read_bytes(), b'someone else, same name')
        self.assertEqual(self.mount.read_bytes(), with_ours(mount_bytes()))

    def test_remove_without_package_json_keeps_pak(self):
        self.mount.write_bytes(with_ours(mount_bytes()))
        self.pak.write_bytes(PAK)
        (self.native / 'package.json').unlink()
        plan = native_launch.plan_remove(self.state())
        self.assertIsNone(plan[0]['pak_sha256'])
        result = self.execute(self.request(plan))
        self.assertTrue(result['ok'])
        self.assertEqual(self.pak.read_bytes(), PAK)
        self.assertEqual(self.mount.read_bytes(), mount_bytes())
        self.assertIn('kept: no package.json', [op.get('pak') for op in result['operations']])

    def record(self, pak_hash):
        (self.native / 'installed.json').write_text(json.dumps(
            {'schema_version': 1, 'key': 'k0', 'pak_sha256': pak_hash, 'pak_sha1': SHA1}), encoding='utf-8')

    def test_remove_hash_from_installed_json_after_rebuild(self):
        self.mount.write_bytes(with_ours(mount_bytes()))
        self.pak.write_bytes(PAK)
        self.record(sha256(self.pak))
        (self.native / 'package.json').write_text(json.dumps(dict(self.package, key='k2', pak_sha256='e' * 64)),
                                                  encoding='utf-8')
        plan = native_launch.plan_remove(native_launch.inspect(self.game, self.native, 'k2',
                                                               running=lambda game: [], guard=lambda game: None))
        self.assertEqual(plan[0]['pak_sha256'], sha256(self.pak))
        result = self.execute(self.request(plan))
        self.assertTrue(result['ok'])
        self.assertFalse(self.pak.exists())
        self.assertEqual(self.mount.read_bytes(), mount_bytes())

    def test_remove_hash_matching_neither_record_is_refused(self):
        self.mount.write_bytes(with_ours(mount_bytes()))
        self.pak.write_bytes(b'someone else, same name')
        self.record('d' * 64)
        named = [{'op': 'remove', 'pak_sha256': sha256(self.pak), 'expected_sha256': sha256(self.mount)}]
        with self.assertRaises(ValueError):
            self.execute(self.request(named))
        self.assertEqual(self.pak.read_bytes(), b'someone else, same name')
        self.assertEqual(self.mount.read_bytes(), with_ours(mount_bytes()))

    def test_remove_with_installed_json_and_without_package_json(self):
        self.mount.write_bytes(with_ours(mount_bytes()))
        self.pak.write_bytes(PAK)
        self.record(sha256(self.pak))
        (self.native / 'package.json').unlink()
        plan = native_launch.plan_remove(self.state())
        self.assertEqual(plan[0]['pak_sha256'], sha256(self.pak))
        result = self.execute(self.request(plan))
        self.assertTrue(result['ok'])
        self.assertFalse(self.pak.exists())
        self.assertEqual(self.mount.read_bytes(), mount_bytes())
        none = [{'op': 'remove', 'pak_sha256': None, 'expected_sha256': sha256(self.mount)}]
        with self.assertRaises(ValueError):  # a record exists, so «no hash» is not the rule any more
            self.execute(self.request(none))

    def test_remove_under_changed_version(self):
        changed = mount_bytes(first=CHANGED)
        self.mount.write_bytes(with_ours(changed))
        self.pak.write_bytes(PAK)
        result = self.execute(self.request(native_launch.plan_remove(self.state())))
        self.assertTrue(result['ok'])
        self.assertFalse(self.pak.exists())
        self.assertEqual(self.mount.read_bytes(), changed)
        self.assertEqual([op.get('write') for op in result['operations']], ['mount', 'pak'])

    def test_remove_rechecks_pak_before_mount_write(self):
        self.mount.write_bytes(with_ours(mount_bytes()))
        self.pak.write_bytes(PAK)
        request = self.request(native_launch.plan_remove(self.state()))
        calls = []

        def running(game):
            calls.append(game)
            if len(calls) == 2:
                self.pak.write_bytes(b'swapped in after the pre-check')
            return []
        with self.assertRaises(ValueError):
            self.execute(request, running=running)
        self.assertEqual(self.mount.read_bytes(), with_ours(mount_bytes()))
        self.assertEqual(self.result(request)['operations'], [])

    def test_remove_with_missing_pak_and_without_line_writes_nothing(self):
        stat = self.mount.stat()
        plan = native_launch.plan_remove(self.state())
        result = self.execute(self.request(plan))
        self.assertTrue(result['ok'])
        self.assertEqual(self.mount.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertEqual([op.get('write') for op in result['operations']], [None])


class ElevationTests(HelperCase):
    def fake_api(self, succeed, exit_code=0):
        calls = {}

        def shell_execute(pointer):
            info = pointer._obj
            calls['verb'], calls['file'] = info.lpVerb, info.lpFile
            calls['parameters'], calls['mask'] = info.lpParameters, info.fMask
            if succeed:
                info.hProcess = 42
            return succeed

        def exit_code_of(handle, pointer):
            pointer._obj.value = exit_code
            return True
        shell32 = mock.Mock(ShellExecuteExW=shell_execute)
        kernel32 = mock.Mock(WaitForSingleObject=mock.Mock(return_value=0),
                             GetExitCodeProcess=exit_code_of, CloseHandle=mock.Mock(return_value=True))
        return calls, (shell32, kernel32)

    def test_run_elevated_reports_cancel(self):
        calls, api = self.fake_api(False)
        request = self.native / 'requests' / 'a b.json'
        with mock.patch.object(native_helper, '_api', return_value=api), \
                mock.patch.object(native_helper.ctypes, 'get_last_error', return_value=1223, create=True):
            self.assertEqual(native_helper.run_elevated(request), 1223)
        self.assertEqual(calls['verb'], 'runas')
        api[1].WaitForSingleObject.assert_not_called()

    def test_run_elevated_waits_and_quotes_spaces(self):
        calls, api = self.fake_api(True, exit_code=2)
        request = self.native / 'requests' / 'a b.json'
        with mock.patch.object(native_helper, '_api', return_value=api), \
                mock.patch.object(sys, 'frozen', False, create=True):
            self.assertEqual(native_helper.run_elevated(request), 2)
        self.assertEqual(calls['file'], sys.executable)
        self.assertEqual(calls['parameters'],
                         subprocess.list2cmdline(['-m', 'wuwaru.gui', '--native-helper', str(request)]))
        self.assertIn('"', calls['parameters'])
        self.assertTrue(calls['mask'] & native_helper.SEE_MASK_NOCLOSEPROCESS)
        api[1].WaitForSingleObject.assert_called_once_with(42, native_helper.INFINITE)
        api[1].CloseHandle.assert_called_once_with(42)
        with mock.patch.object(native_helper, '_api', return_value=api), \
                mock.patch.object(sys, 'frozen', True, create=True):
            native_helper.run_elevated(request)
        self.assertEqual(calls['parameters'], subprocess.list2cmdline(['--native-helper', str(request)]))

    def test_launch_steam(self):
        with mock.patch('os.startfile', create=True) as startfile:
            native_helper.launch_steam()
        startfile.assert_called_once_with('steam://rungameid/3513350')

    def test_helper_main(self):
        self.assertIsNone(native_helper.helper_main(['--workspace', 'x']))
        self.pak.write_bytes(PAK)
        good = self.request(native_launch.plan_play(self.state(), self.native))
        bad = self.raw_request({'schema_version': 9})
        roots = mock.patch.object(native_helper, '_discovered_roots', return_value=[self.game.resolve()])
        quiet = mock.patch.multiple(native_helper, game_processes=mock.Mock(return_value=[]),
                                    update_guard=mock.Mock(return_value=None))
        with roots, quiet:
            self.assertEqual(native_helper.helper_main(['--native-helper', str(good)]), 0)
            self.assertEqual(native_helper.helper_main(['--native-helper', str(bad)]), 2)
        self.assertEqual(self.mount.read_bytes(), with_ours(mount_bytes()))


if __name__ == '__main__':
    unittest.main()


class OtherVersionTests(HelperCase):
    def test_the_helper_writes_into_the_version_folder_it_finds(self):
        base = self.game / native_launch.RESOURCES
        (base / '3.7.0').rename(base / '3.8.0')
        self.mount = native_launch.mount_path(self.game)
        self.pak = native_launch.pak_path(self.game)
        plan = native_launch.plan_play(self.state(), self.native)
        self.assertEqual([op['op'] for op in plan], ['install_package', 'apply_mount'])
        self.assertTrue(self.execute(self.request(plan))['ok'])
        self.assertEqual(self.pak.read_bytes(), PAK)
        self.assertEqual(self.pak.parent, base / '3.8.0/Lang_en/Base')
        self.assertTrue(native_launch.has_entry(self.mount.read_bytes()))
        self.assertFalse((base / '3.7.0').exists())
