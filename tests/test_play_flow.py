"""Play / remove end to end on the core, without mocks of our own code.

Real inspect, build_package, plan_play / plan_remove, write_request, the helper's execute,
record_installed and the staging drop. Only the process inventory, the Steam update guard and
installation discovery are replaced, and the "game" is a synthetic folder in `.test-tmp/`.
No UAC, no helper process, no Steam.
"""
import unittest

from tests.test_discovery import fixture
from tests.test_native_launch import CHANGED, mount_bytes
from tests.test_native_package import PackageCase
from wuwaru import native_helper, native_launch, native_package as package
from wuwaru.native_launch import MOUNT, PAK_RELATIVE, Phase

KINDS = ('ui',)


class PlayFlowTests(PackageCase):
    def setUp(self):
        super().setUp()
        self.game = fixture(self.home / 'game')
        self.mount = self.game / MOUNT
        self.original = mount_bytes()
        self.mount.write_bytes(self.original)
        self.pak = self.game / PAK_RELATIVE
        self.pak.parent.mkdir(parents=True)
        self.requests = 0

    # -- the same steps the Play page takes, without Qt ------------------------------------

    def inspect(self):
        key = package.current_key(self.project, self.native, KINDS, pins=self.pins)
        return native_launch.inspect(self.game, self.native, key, running=lambda game: [],
                                     guard=lambda game: None)

    def helper(self, plan):
        mount = native_launch.read_bounded(self.mount)
        request = native_helper.write_request(self.native, self.game, plan, mount)
        self.requests += 1
        result = native_helper.execute(request, discover_roots=lambda: [self.game.resolve()],
                                       running=lambda game: [], guard=lambda game: None)
        self.assertTrue(result['ok'], result)
        return result

    def play(self):
        state = self.inspect()
        if state.phase is Phase.NEEDS_REBUILD:
            package.build_package(self.project, self.native, KINDS, pins=self.pins, installed=self.pak)
            state = self.inspect()
        self.assertIn(state.phase, (Phase.READY, Phase.NEEDS_INSTALL), state.reasons)
        if state.mount_applied:  # the exact line is already mounted: Steam starts without the helper
            return []
        plan = native_launch.plan_play(state, self.native)
        self.helper(plan)
        for op in plan:
            if op['op'] == 'install_package':
                native_launch.record_installed(self.native, op)
        native_launch.drop_staging(self.native, plan)
        return [op['op'] for op in plan]

    def remove(self):
        state = self.inspect()
        return self.helper(native_launch.plan_remove(state))['operations']

    def entry(self):
        return f"{native_launch.MOUNT_STEM},900,{self.report()['pak_sha1']},,,"

    def report(self):
        import json
        return json.loads((self.native / 'package.json').read_text('utf-8'))

    def staged(self):
        return sorted(path.name for path in (self.native / 'staging').glob('*.pak'))

    def assert_installed(self):
        self.assertEqual(package.sha256(self.pak), self.report()['pak_sha256'])
        self.assertEqual(self.mount.read_bytes(), native_launch.with_entry(self.original, self.entry()))
        self.assertEqual(self.staged(), [])

    # -- flows -------------------------------------------------------------------------------

    def test_play_remove_play_again(self):
        self.assertEqual(self.play(), ['install_package', 'apply_mount'])
        self.assert_installed()
        state = self.inspect()
        self.assertIs(state.phase, Phase.READY)
        self.assertTrue(state.mount_applied)

        operations = self.remove()
        self.assertEqual([(op['write'], op.get('pak')) for op in operations], [('mount', None), ('pak', 'removed')])
        self.assertEqual(self.mount.read_bytes(), self.original)
        self.assertFalse(self.pak.exists())

        state = self.inspect()  # staging went away after the first install, the PAK with remove
        self.assertIs(state.phase, Phase.NEEDS_REBUILD)
        self.assertIn('Пакет нужно собрать заново', state.reasons)
        self.assertEqual(self.play(), ['install_package', 'apply_mount'])
        self.assert_installed()
        self.assertIs(self.inspect().phase, Phase.READY)

    def test_play_twice_in_a_row(self):
        self.assertEqual(self.play(), ['install_package', 'apply_mount'])
        mounted = self.mount.read_bytes()
        self.assertEqual(self.play(), [])  # READY with our exact line: no request, no UAC
        self.assertEqual(self.requests, 1)
        self.assertEqual(self.mount.read_bytes(), mounted)
        # The client resets Mount ~40–60 s after start; the next click writes only the line.
        self.mount.write_bytes(self.original)
        self.assertEqual(self.play(), ['apply_mount'])
        self.assert_installed()
        self.assertEqual(self.requests, 2)

    def test_version_changed_with_our_line_removes(self):
        self.play()
        updated = native_launch.with_entry(mount_bytes(first=CHANGED), self.entry())
        self.mount.write_bytes(updated)  # the game updated its English packages, our line stayed
        state = self.inspect()
        self.assertIs(state.phase, Phase.VERSION_CHANGED)
        self.assertTrue(state.ours_in_mount)
        with self.assertRaises(ValueError):
            native_launch.plan_play(state, self.native)
        operations = self.remove()
        self.assertEqual([(op['write'], op.get('pak')) for op in operations], [('mount', None), ('pak', 'removed')])
        self.assertEqual(self.mount.read_bytes(), mount_bytes(first=CHANGED))
        self.assertFalse(self.pak.exists())
        state = self.inspect()
        self.assertIs(state.phase, Phase.VERSION_CHANGED)
        self.assertFalse(state.ours_in_mount)
        self.assertIsNone(state.installed_sha256)


if __name__ == '__main__':
    unittest.main()
