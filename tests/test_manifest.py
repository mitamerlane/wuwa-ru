import hashlib
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_discovery import FixtureCase, fixture
from wuwaru.discovery import inspect_installation
from wuwaru.manifest import Cancelled, create_manifest, write_manifest, mount_entries, mount_records
from wuwaru.paths import UnsafePath


class ManifestTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.game = fixture(self.home / 'Игра')
        self.installation = inspect_installation(self.game)

    def test_mount_records_preserve_declared_order_and_opaque_fields(self):
        text = ('::Mount::\nLang_en/Base/text,4,AAA,BBB,,\n'
                'Lang_en/1.2/text_P,10,CCC,DDD,extra,\n'
                '::Del::\nunknown deletion syntax\n')
        records = mount_records(text)
        self.assertEqual(records, [
            {'path': 'Lang_en/Base/text.pak', 'declared_order': 4,
             'opaque_fields': ('AAA', 'BBB', '', '')},
            {'path': 'Lang_en/1.2/text_P.pak', 'declared_order': 10,
             'opaque_fields': ('CCC', 'DDD', 'extra', '')},
        ])
        self.assertEqual(mount_entries(text), [r['path'] for r in records])
        self.assertEqual(mount_records('::Mount::\n::Del::\n'), [])

    def test_mount_record_invalid_structure_and_order_fail_closed(self):
        for text in ('x,1,A', '::Unknown::\nx,1,A', '::Del::\n',
                     '::Mount::\nx,-1,A', '::Mount::\nx,1.5,A',
                     '::Mount::\nx,²,A', '::Mount::\n,1,A'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                mount_records(text)

    def test_inventory_hashes_and_repeatability(self):
        before = {p.relative_to(self.game): p.read_bytes()
                  for p in self.game.rglob('*') if p.is_file()}
        a = create_manifest(self.installation)
        b = create_manifest(self.installation)
        self.assertEqual(a, b)
        self.assertEqual(a['resource_version'], '3.7.0')
        self.assertEqual(a['referenced_patch_versions'], ['3.7.16'])
        self.assertEqual(a['compatibility'], 'UNKNOWN')
        self.assertFalse(a['installation_allowed'])
        self.assertIsNone(a['client_version'])
        self.assertEqual(len(a['packages']), 2)
        self.assertTrue(all(p['sha256'] is None for p in a['packages']))
        full = create_manifest(self.installation, hash_packages=True)
        self.assertEqual(full['hash_scope'], 'full')
        for item in full['packages']:
            self.assertEqual(item['sha256'], hashlib.sha256(
                (self.game / item['path']).read_bytes()).hexdigest())
        after = {p.relative_to(self.game): p.read_bytes()
                 for p in self.game.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_unknown_resource_version_and_no_paks(self):
        empty = self.home / 'empty'
        empty.mkdir()
        (empty / 'Wuthering Waves.exe').write_bytes(b'fixture')
        (empty / 'Client/Content/Paks').mkdir(parents=True)
        result = create_manifest(inspect_installation(empty))
        self.assertIsNone(result['resource_version'])
        self.assertFalse(result['inventory_complete'])
        self.assertTrue(result['warnings'])

    def test_ambiguous_resources_are_not_guessed(self):
        other = self.game / 'Client/Saved/Resources/9.0.0/Mount'
        other.mkdir(parents=True)
        (other / 'MountResource.txt').write_text('::Mount::\n::Del::\n')
        result = create_manifest(self.installation)
        self.assertIsNone(result['resource_version'])
        selected = create_manifest(self.installation, resource_version='3.7.0')
        self.assertEqual(selected['resource_version_selection'], 'explicit')
        with self.assertRaises(ValueError):
            create_manifest(self.installation, resource_version='8.0.0')

    def test_unsafe_mount_paths_fail_closed(self):
        mount = self.game / 'Client/Saved/Resources/3.7.0/Mount/MountResource.txt'
        for path in ('../escape', '/absolute', 'C:/outside', 'Resource/../../escape',
                     'Resource/file:stream', 'Resource/CON', 'Resource/a. /b'):
            with self.subTest(path=path):
                mount.write_text(f'::Mount::\n{path},1,A,B,,\n::Del::\n')
                with self.assertRaises(UnsafePath):
                    create_manifest(self.installation)

    def test_missing_package_and_malformed_mount_fail(self):
        mount = self.game / 'Client/Saved/Resources/3.7.0/Mount/MountResource.txt'
        mount.write_text('::Mount::\nResource/missing,1,A,B,,\n::Del::\n')
        with self.assertRaises(FileNotFoundError):
            create_manifest(self.installation)
        mount.write_text('::Mount::\nbroken record\n::Del::\n')
        with self.assertRaises(ValueError):
            create_manifest(self.installation)

    def test_output_is_atomic_outside_game_and_never_overwrites(self):
        manifest = create_manifest(self.installation)
        output = self.home / 'reports/манифест.json'
        write_manifest(manifest, output)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8')), manifest)
        with self.assertRaises(FileExistsError):
            write_manifest(manifest, output)
        with self.assertRaises(UnsafePath):
            write_manifest(manifest, self.game / 'report.json')
        with self.assertRaises(UnsafePath):
            write_manifest(manifest, self.game / 'Client/report.json')

    def test_cancelled_scan_writes_nothing(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            create_manifest(self.installation, cancelled=event)

    def test_invalid_utf8_is_rejected(self):
        mount = self.game / 'Client/Saved/Resources/3.7.0/Mount/MountResource.txt'
        mount.write_bytes(b'::Mount::\n\xff')
        with self.assertRaises(UnicodeDecodeError):
            create_manifest(self.installation)

    def test_file_changed_while_hashing_is_rejected(self):
        from wuwaru.manifest import fingerprint
        pak = self.game / 'Client/Content/Paks/base.pak'
        digest = hashlib.sha256()

        class MutatingHash:
            def update(self, data):
                digest.update(data)
                pak.write_bytes(b'changed during read, different size')

            def hexdigest(self):
                return digest.hexdigest()

        with patch('wuwaru.manifest.hashlib.sha256', return_value=MutatingHash()):
            with self.assertRaises(RuntimeError):
                fingerprint(pak, True, None)

    def test_cancellation_during_hashing(self):
        from wuwaru.manifest import fingerprint
        event = threading.Event()
        pak = self.game / 'Client/Content/Paks/base.pak'
        pak.write_bytes(b'F' * (2 * 1024 * 1024))
        digest = hashlib.sha256()

        class CancellingHash:
            def update(self, data):
                digest.update(data)
                event.set()

        with patch('wuwaru.manifest.hashlib.sha256', return_value=CancellingHash()):
            with self.assertRaises(Cancelled):
                fingerprint(pak, True, event)

    def test_publish_failure_cleans_temporary_file(self):
        manifest = create_manifest(self.installation)
        output = self.home / 'failed.json'
        target = 'os.rename' if sys.platform == 'win32' else 'os.link'
        with patch('wuwaru.manifest.' + target, side_effect=OSError('publish failed')):
            with self.assertRaises(OSError):
                write_manifest(manifest, output)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.home.glob('.manifest-*')))

    def test_disk_full_leaves_no_output(self):
        manifest = create_manifest(self.installation)
        output = self.home / 'report.json'
        with patch('wuwaru.manifest.shutil.disk_usage', return_value=(100, 100, 0)):
            with self.assertRaises(OSError):
                write_manifest(manifest, output)
        self.assertFalse(output.exists())

    def test_junction_escape_rejected(self):
        outside = self.home / 'outside'
        outside.mkdir()
        link = self.game / 'escape'
        if sys.platform == 'win32':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(UnsafePath):
                write_manifest(create_manifest(self.installation), link / 'report.json')
        finally:
            if sys.platform == 'win32':
                link.rmdir()
            else:
                link.unlink()

    def test_cli_end_to_end_and_no_install_command(self):
        output = self.home / 'cli-report.json'
        result = subprocess.run([sys.executable, '-m', 'wuwaru', 'scan', '--game',
                                 str(self.game), '--output', str(output)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['mode'], 'READ_ONLY')
        rejected = subprocess.run([sys.executable, '-m', 'wuwaru', 'install'], capture_output=True)
        self.assertEqual(rejected.returncode, 2)
