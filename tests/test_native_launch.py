import hashlib
import json
import subprocess
import unittest
from unittest import mock

from tests.test_discovery import FixtureCase, fixture
from wuwaru import native_launch
from wuwaru.native_launch import Phase
from wuwaru.native_package import sha256


class NativeLaunchPrimitivesTests(unittest.TestCase):
    def test_process_inventory_opens_no_console_window(self):
        """A console window steals focus; the Play page re-checks on activation -> endless loop."""
        done = subprocess.CompletedProcess([], 0, stdout='[]', stderr='')
        with mock.patch.object(native_launch.subprocess, 'run', return_value=done) as run:
            self.assertEqual(native_launch.consumers(native_launch.Path('C:/Game')), [])
        self.assertEqual(run.call_args.kwargs.get('creationflags'), subprocess.CREATE_NO_WINDOW)


VERIFIED = native_launch.VERIFIED_FINGERPRINT
OURS_SHA1 = hashlib.sha1(b'synthetic pak').hexdigest().upper()
CHANGED = VERIFIED[0].replace('89C6', '0000')


def mount_bytes(eol='\n', ours=False, first=VERIFIED[0], final_eol=True):
    lines = ['::Mount::', first, VERIFIED[1]]
    if ours:
        lines.append(f'{native_launch.MOUNT_STEM},900,{OURS_SHA1},,,')
    lines.append('::Del::')
    return (eol.join(lines) + (eol if final_eol else '')).encode()


class LaunchStateTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.game = fixture(self.home / 'game')
        self.mount = self.game / native_launch.MOUNT
        self.mount.write_bytes(mount_bytes())
        self.native = self.home / 'native'
        (self.native / 'staging').mkdir(parents=True)
        self.pak = self.game / native_launch.RESOURCE / 'Lang_en/Base' / native_launch.PAK_NAME
        self.pak.parent.mkdir(parents=True)
        self.pak.write_bytes(b'synthetic pak')
        self.package = {'schema_version': 1, 'key': 'k1', 'pak_sha256': sha256(self.pak),
                        'pak_sha1': OURS_SHA1, 'pak_bytes': 13}
        (self.native / 'package.json').write_text(json.dumps(self.package), encoding='utf-8')
        (self.native / 'staging' / 'k1.pak').write_bytes(b'synthetic pak')

    def inspect(self, key='k1', **kwargs):
        kwargs.setdefault('running', lambda game: [])
        kwargs.setdefault('guard', lambda game: None)
        return native_launch.inspect(self.game, self.native, key, **kwargs)

    def test_fingerprint_ignores_our_line(self):
        plain, ours = mount_bytes(), mount_bytes(ours=True)
        self.assertEqual(native_launch.fingerprint(plain), native_launch.fingerprint(ours))
        self.assertEqual(native_launch.fingerprint(ours), VERIFIED)

    def test_with_entry_preserves_crlf(self):
        entry = f'{native_launch.MOUNT_STEM},900,{OURS_SHA1},,,'
        once = native_launch.with_entry(mount_bytes('\r\n'), entry)
        self.assertEqual(once, mount_bytes('\r\n', ours=True))
        self.assertEqual(native_launch.with_entry(once, entry), once)

    def test_with_entry_without_trailing_newline_roundtrip(self):
        entry = f'{native_launch.MOUNT_STEM},900,{OURS_SHA1},,,'
        raw = b'::Mount::\n' + VERIFIED[0].encode() + b'\n' + VERIFIED[1].encode()
        out = native_launch.with_entry(raw, entry)
        self.assertEqual(native_launch.fingerprint(out), VERIFIED)
        self.assertEqual(native_launch.without_entry(out), raw)

    def test_without_entry_restores_original_bytes(self):
        for eol in ('\n', '\r\n'):
            self.assertEqual(native_launch.without_entry(mount_bytes(eol, ours=True)), mount_bytes(eol))
            self.assertEqual(native_launch.without_entry(mount_bytes(eol)), mount_bytes(eol))

    def test_no_mount_section_is_error(self):
        with self.assertRaises(ValueError):
            native_launch.fingerprint(b'::Del::\n')

    def test_inspect_phases(self):
        self.assertEqual(native_launch.inspect(None, self.native, 'k1').phase, Phase.NO_GAME)
        state = self.inspect(None, input_error='нет шрифта')
        self.assertEqual(state.phase, Phase.NO_INPUTS)
        self.assertIn('нет шрифта', state.reasons)
        self.assertEqual(self.inspect(running=lambda g: [{'Name': 'Client-Win64-Shipping.exe'}]).phase,
                         Phase.GAME_RUNNING)

        def boom(game):
            raise ValueError('updating')
        self.assertEqual(self.inspect(guard=boom).phase, Phase.STEAM_UPDATING)
        state = self.inspect()
        self.assertEqual(state.phase, Phase.READY)
        self.assertEqual(state.fingerprint, VERIFIED)
        self.assertFalse(state.ours_in_mount)
        self.assertEqual(self.inspect(key='other').phase, Phase.NEEDS_REBUILD)
        self.pak.write_bytes(b'tampered')
        self.assertEqual(self.inspect().phase, Phase.NEEDS_INSTALL)
        self.pak.unlink()
        self.assertEqual(self.inspect().phase, Phase.NEEDS_INSTALL)
        (self.native / 'package.json').unlink()
        self.assertEqual(self.inspect().phase, Phase.NEEDS_REBUILD)

    def test_missing_staging_and_game_copy_needs_rebuild(self):
        self.pak.unlink()
        (self.native / 'staging' / 'k1.pak').unlink()
        state = self.inspect()
        self.assertEqual(state.phase, Phase.NEEDS_REBUILD)
        self.assertIn('Пакет нужно собрать заново', state.reasons)
        with self.assertRaises(ValueError):
            native_launch.plan_play(state, self.native)
        self.pak.write_bytes(b'synthetic pak')  # the installed copy alone is enough: READY
        self.assertEqual(self.inspect().phase, Phase.READY)

    def test_our_stem_under_del_is_not_mounted(self):
        entry = f'{native_launch.MOUNT_STEM},900,{OURS_SHA1},,,'
        self.mount.write_bytes(('::Mount::\n' + '\n'.join(VERIFIED) + '\n::Del::\n' + entry + '\n').encode())
        state = self.inspect()
        self.assertFalse(state.ours_in_mount)
        self.assertFalse(state.mount_applied)
        self.mount.write_bytes(mount_bytes(ours=True))
        self.assertTrue(self.inspect().ours_in_mount)

    def test_mount_applied_only_for_the_exact_line_when_ready(self):
        self.assertFalse(self.inspect().mount_applied)
        self.mount.write_bytes(mount_bytes(ours=True))
        state = self.inspect()
        self.assertEqual(state.phase, Phase.READY)
        self.assertTrue(state.mount_applied)
        stale = native_launch.with_entry(mount_bytes(), f'{native_launch.MOUNT_STEM},900,{"1" * 40},,,')
        self.mount.write_bytes(stale)
        self.assertFalse(self.inspect().mount_applied)
        self.mount.write_bytes(mount_bytes(ours=True))
        self.pak.write_bytes(b'tampered')
        self.assertFalse(self.inspect().mount_applied)  # NEEDS_INSTALL never skips the helper

    def test_game_processes_ignore_foreign_launchers(self):
        foreign = {'ProcessId': 1, 'Name': 'EpicGamesLauncher.exe',
                   'ExecutablePath': r'C:\Program Files (x86)\Epic Games\Launcher\EpicGamesLauncher.exe'}
        ours = {'ProcessId': 2, 'Name': 'Wuthering Waves.exe', 'ExecutablePath': str(self.game / 'Wuthering Waves.exe')}
        under_root = {'ProcessId': 3, 'Name': 'helper.exe', 'ExecutablePath': str(self.game / 'Client/helper.exe')}
        client = {'ProcessId': 4, 'Name': 'Client-Win64-Shipping.exe', 'ExecutablePath': None}
        with mock.patch.object(native_launch, 'consumers', return_value=[foreign]):
            self.assertEqual(native_launch.game_processes(self.game), [])
            self.assertEqual(native_launch.inspect(self.game, self.native, 'k1', guard=lambda g: None).phase,
                             Phase.READY)
        with mock.patch.object(native_launch, 'consumers', return_value=[foreign, ours, under_root, client]):
            self.assertEqual(native_launch.game_processes(self.game), [ours, under_root, client])
            state = native_launch.inspect(self.game, self.native, 'k1', guard=lambda g: None)
        self.assertEqual(state.phase, Phase.GAME_RUNNING)
        self.assertIn('Запущено: Client-Win64-Shipping.exe, Wuthering Waves.exe, helper.exe', state.reasons)
        self.assertFalse(any(str(self.game) in reason for reason in state.reasons))

    def test_game_processes_fail_closed(self):
        with mock.patch.object(native_launch, 'consumers', side_effect=RuntimeError('inventory down')):
            with self.assertRaises(RuntimeError):
                native_launch.game_processes(self.game)
            state = native_launch.inspect(self.game, self.native, 'k1', guard=lambda g: None)
        self.assertEqual(state.phase, Phase.GAME_RUNNING)

    def test_version_change_and_accept(self):
        self.mount.write_bytes(mount_bytes(first=CHANGED))
        self.assertEqual(self.inspect().phase, Phase.VERSION_CHANGED)
        fp = self.inspect().fingerprint
        native_launch.accept_fingerprint(self.native, fp)
        self.assertIn(fp, native_launch.accepted_fingerprints(self.native))
        self.assertIn(VERIFIED, native_launch.accepted_fingerprints(self.native))
        self.assertNotEqual(self.inspect().phase, Phase.VERSION_CHANGED)
        journal = [json.loads(line) for line in (self.native / 'journal.jsonl').read_text().splitlines()]
        self.assertEqual(journal[-1]['event'], 'accept_fingerprint')
        self.assertEqual(journal[-1]['fingerprint'], list(fp))

    def test_plan_play_install_then_mount(self):
        self.pak.write_bytes(b'old')
        plan = native_launch.plan_play(self.inspect(), self.native)
        self.assertEqual(plan[0], {'op': 'install_package', 'staging': str(self.native / 'staging' / 'k1.pak'),
                                   'sha256': self.package['pak_sha256'], 'sha1': OURS_SHA1})
        self.assertEqual(plan[1], {'op': 'apply_mount', 'expected_sha256': sha256(self.mount),
                                   'entry': f'{native_launch.MOUNT_STEM},900,{OURS_SHA1},,,'})
        self.assertEqual(len(plan), 2)
        self.pak.write_bytes(b'synthetic pak')
        self.assertEqual([o['op'] for o in native_launch.plan_play(self.inspect(), self.native)],
                         ['apply_mount'])

    def test_plan_play_refuses_version_changed(self):
        self.mount.write_bytes(mount_bytes(first=CHANGED))
        with self.assertRaises(ValueError):
            native_launch.plan_play(self.inspect(), self.native)

    def test_plan_remove(self):
        self.mount.write_bytes(mount_bytes(first=CHANGED, ours=True))
        state = self.inspect()
        self.assertEqual(state.phase, Phase.VERSION_CHANGED)
        self.assertTrue(state.ours_in_mount)
        self.assertEqual(native_launch.plan_remove(state),
                         [{'op': 'remove', 'pak_sha256': self.package['pak_sha256'],
                           'expected_sha256': sha256(self.mount)}])
        (self.native / 'package.json').unlink()
        self.assertIsNone(native_launch.plan_remove(self.inspect())[0]['pak_sha256'])
        self.pak.unlink()
        self.assertIsNone(native_launch.plan_remove(self.inspect())[0]['pak_sha256'])
        for state in (self.inspect(running=lambda g: [{'Name': 'x'}]),
                      native_launch.inspect(None, self.native, 'k1')):
            with self.assertRaises(ValueError):
                native_launch.plan_remove(state)

    def test_installed_record_names_the_pak_after_a_rebuild(self):
        op = {'op': 'install_package', 'staging': str(self.native / 'staging' / 'k1.pak'),
              'sha256': self.package['pak_sha256'], 'sha1': OURS_SHA1}
        native_launch.record_installed(self.native, op)
        record = {'schema_version': 1, 'key': 'k1', 'pak_sha256': self.package['pak_sha256'], 'pak_sha1': OURS_SHA1}
        self.assertEqual(json.loads((self.native / 'installed.json').read_text('utf-8')), record)
        self.assertEqual([p.name for p in self.native.iterdir() if p.name.startswith('installed')],
                         ['installed.json'])
        # A new build is in package.json, the old PAK is still in the game: the record names it.
        (self.native / 'package.json').write_text(json.dumps(dict(self.package, key='k2', pak_sha256='e' * 64)),
                                                  encoding='utf-8')
        (self.native / 'staging' / 'k2.pak').write_bytes(b'new build')
        self.mount.write_bytes(mount_bytes(ours=True))
        state = self.inspect(key='k2')
        self.assertEqual(state.phase, Phase.NEEDS_INSTALL)
        self.assertEqual(state.installed_record, record)
        self.assertEqual(native_launch.plan_remove(state)[0]['pak_sha256'], self.package['pak_sha256'])
        # The record does not match the file in the game: package.json, never the bare file hash.
        self.pak.write_bytes(b'other')
        self.assertEqual(native_launch.plan_remove(self.inspect(key='k2'))[0]['pak_sha256'], 'e' * 64)
        # No package.json and no PAK: the record still names what may be deleted.
        (self.native / 'package.json').unlink()
        self.pak.unlink()
        self.assertEqual(native_launch.plan_remove(self.inspect())[0]['pak_sha256'], self.package['pak_sha256'])

    def test_running_game_beats_missing_inputs_and_blocks_remove(self):
        state = self.inspect(key=None, running=lambda g: [{'Name': 'x'}])
        self.assertEqual(state.phase, Phase.GAME_RUNNING)
        with self.assertRaises(ValueError):
            native_launch.plan_remove(state)

        def boom(game):
            raise ValueError('updating')
        state = self.inspect(key=None, guard=boom)
        self.assertEqual(state.phase, Phase.STEAM_UPDATING)
        with self.assertRaises(ValueError):
            native_launch.plan_remove(state)

    def test_guard_oserror_fails_closed(self):
        def missing(game):
            raise FileNotFoundError('appmanifest')
        state = self.inspect(guard=missing)
        self.assertEqual(state.phase, Phase.STEAM_UPDATING)
        self.assertTrue(any('appmanifest' in r for r in state.reasons))

    def test_plans_use_inspected_mount_hash(self):
        state = self.inspect()
        inspected = sha256(self.mount)
        self.mount.write_bytes(mount_bytes(first=CHANGED))
        self.assertEqual(native_launch.plan_play(state, self.native)[-1]['expected_sha256'], inspected)
        self.assertEqual(native_launch.plan_remove(state)[0]['expected_sha256'], inspected)

    def test_with_entry_moves_line_last_and_dedupes(self):
        entry = f'{native_launch.MOUNT_STEM},900,{OURS_SHA1},,,'
        middle = ('::Mount::\n' + VERIFIED[0] + '\n' + entry + '\n' + VERIFIED[1] + '\n::Del::\n').encode()
        self.assertEqual(native_launch.with_entry(middle, entry), mount_bytes(ours=True))
        stale = f'{native_launch.MOUNT_STEM},900,{"1" * 40},,,'
        twice = ('::Mount::\n' + stale + '\n' + VERIFIED[0] + '\n' + VERIFIED[1] + '\n' + entry
                 + '\n::Del::\n').encode()
        out = native_launch.with_entry(twice, entry)
        self.assertEqual(out, mount_bytes(ours=True))
        self.assertEqual(native_launch.with_entry(out, entry), out)

    def test_replace_atomic_writes_and_verifies(self):
        landed = []
        before = self.mount.read_bytes()
        after = mount_bytes(ours=True)
        native_launch.replace_atomic(self.mount, before, after, landed=lambda: landed.append(True))
        self.assertEqual(self.mount.read_bytes(), after)
        self.assertEqual(landed, [True])
        self.assertEqual(sorted(p.name for p in self.mount.parent.iterdir()),
                         ['MountLang_en.txt', 'MountResource.txt'])

    def test_replace_atomic_refuses_change_before_replace(self):
        before = self.mount.read_bytes()
        with mock.patch.object(native_launch, 'read_bounded', return_value=b'::Mount::\nchanged\n'):
            with self.assertRaises(ValueError):
                native_launch.replace_atomic(self.mount, before, mount_bytes(ours=True))
        self.assertEqual(self.mount.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.mount.parent.iterdir()),
                         ['MountLang_en.txt', 'MountResource.txt'])

    def test_inspect_rejects_junction_mount(self):
        real = self.game / 'RealMount'
        mount_dir = self.mount.parent
        mount_dir.rename(real)
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(mount_dir), str(real)],
                              capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        with self.assertRaises(ValueError):
            self.inspect()


if __name__ == '__main__':
    unittest.main()
