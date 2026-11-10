import hashlib
import struct
import contextlib
import io
import json
from unittest.mock import patch

from tests.test_discovery import FixtureCase, fixture
from tests.pak_fixture import pak
from wuwaru.discovery import inspect_installation
from wuwaru.extraction import extract
from wuwaru.pak_reader import UnsupportedPak, read_databases
from wuwaru.resource_probe import inspect_footer, probe_resources


class ResourceProbeTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.game = fixture(self.home / 'game')
        self.installation = inspect_installation(self.game)
        self.package = self.game / 'Client/Content/Paks/base.pak'
        pak(self.package, {'lang_test.db': b'fixture'})

    def encrypted(self):
        data = bytearray(self.package.read_bytes())
        data[-221 + 16] = 1
        data[-160:-128] = b'Oodle' + bytes(27)
        self.package.write_bytes(data)

    def test_encryption_and_declared_compression_are_distinct(self):
        self.encrypted()
        info = inspect_footer(self.package)
        self.assertEqual(info['status'], 'ENCRYPTED_INDEX')
        self.assertEqual(info['version_field'], 12)
        self.assertEqual(info['compression_methods'], ['Oodle'])
        self.assertTrue(info['encrypted_index'])
        self.assertEqual(info['index_content_status'], 'NOT_READ')
        with self.assertRaisesRegex(UnsupportedPak, 'ENCRYPTED_INDEX'):
            read_databases(self.package)

    def test_no_key_and_compression_does_not_prove_encryption(self):
        data = bytearray(self.package.read_bytes())
        data[-160:-128] = b'Oodle' + bytes(27)
        self.package.write_bytes(data)
        self.assertEqual(inspect_footer(self.package)['status'], 'DECLARED_COMPRESSION')
        data[-221] = 1
        self.package.write_bytes(data)
        self.assertEqual(inspect_footer(self.package)['status'], 'KEY_GUID_PRESENT')

    def test_unknown_malformed_and_out_of_bounds_are_not_supported(self):
        for raw in (b'not a pak', bytes(221)):
            self.package.write_bytes(raw)
            self.assertEqual(inspect_footer(self.package)['status'], 'UNKNOWN_FORMAT')
        pak(self.package, {'lang_test.db': b'fixture'})
        data = bytearray(self.package.read_bytes())
        struct.pack_into('<Q', data, len(data) - 221 + 25, len(data) + 1)
        self.package.write_bytes(data)
        self.assertEqual(inspect_footer(self.package)['status'], 'INVALID_INDEX_BOUNDS')

    def test_all_mount_languages_versions_and_full_byte_binding(self):
        mounts = self.game / 'Client/Saved/Resources/3.7.0/Mount'
        (mounts / 'MountLang_ja.txt').write_text(
            '::Mount::\nResource/3.7.16/patch,20,A,B,,\n::Del::\n', encoding='utf-8')
        manifests = mounts.parent / 'ResManifest'
        manifests.mkdir()
        (manifests / 'ManifestLang_en_3.7.6.txt').write_text('QUJDRA==', encoding='utf-8')
        first = probe_resources(self.installation, hash_language_packages=True)
        self.assertEqual(first, probe_resources(self.installation, hash_language_packages=True))
        self.assertIsNone(first['client_version'])
        self.assertEqual(first['active_resource_version_status'], 'UNKNOWN')
        self.assertEqual(first['language_mounts'][0]['language'], 'en')
        self.assertEqual(first['language_mounts'][1]['language'], 'ja')
        self.assertEqual(first['version_evidence'][0]['version_claim'], '3.7.6')
        self.assertEqual(first['version_evidence'][0]['content_format'], 'OPAQUE_BASE64')
        self.assertFalse(first['complete_game_language_inventory'])
        self.assertEqual(first['extraction']['databases'], 0)
        self.assertTrue(all(v == 'UNKNOWN' for v in first['coverage'].values()))
        identity = first['language_packages_sha256']
        patch_pak = mounts.parent / 'Resource/3.7.16/patch.pak'
        patch_pak.write_bytes(b'changed patch bytes')
        second = probe_resources(self.installation, hash_language_packages=True)
        self.assertNotEqual(identity, second['language_packages_sha256'])
        self.assertIsNone(probe_resources(self.installation)['language_packages_sha256'])

    def test_protected_extraction_repeats_without_publication(self):
        self.encrypted()
        digest = hashlib.sha256(self.package.read_bytes()).hexdigest()
        for _ in range(2):
            with self.assertRaisesRegex(UnsupportedPak, 'ENCRYPTED_INDEX'):
                extract(self.installation, self.home / 'staging',
                        packages=['Client/Content/Paks/base.pak'])
        self.assertFalse((self.home / 'staging').exists())
        self.assertEqual(digest, hashlib.sha256(self.package.read_bytes()).hexdigest())

    def test_changed_snapshot_and_traversal_fail_closed(self):
        from wuwaru.resource_probe import create_manifest
        calls = []

        def changed(*args, **kwargs):
            result = create_manifest(*args, **kwargs)
            calls.append(None)
            if len(calls) > 1:
                result['inventory_fingerprint_sha256'] = 'changed'
            return result

        with patch('wuwaru.resource_probe.create_manifest', side_effect=changed):
            with self.assertRaises(RuntimeError):
                probe_resources(self.installation)
        mount = self.game / 'Client/Saved/Resources/3.7.0/Mount/MountLang_en.txt'
        mount.write_text('::Mount::\n../escape,20,A,B,,\n::Del::\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            probe_resources(self.installation)

    def test_probe_cli_new_report_and_m2_original_import(self):
        from wuwaru.__main__ import main
        from wuwaru.translation_db import TranslationDB
        from tests.test_sqlite_source import database
        source = database(self.home / 'lang_test.db', ((7, 'Hello {Player} <b>%s</b>'),))
        pak(self.package, {'lang_test.db': source.read_bytes()})
        destination = self.home / 'probe.json'
        args = ['probe-resources', '--game', str(self.game), '--output', str(destination)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(destination.read_text('utf-8'))['client_version_status'], 'UNKNOWN')
        before = destination.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 1)
        self.assertEqual(before, destination.read_bytes())
        stage = self.home / 'stage'
        report = extract(self.installation, stage, packages=['Client/Content/Paks/base.pak'])
        self.assertEqual(report['databases'][0]['validation']['rows'], 1)
        core = TranslationDB(self.home / 'core')
        self.addCleanup(core.close)
        self.assertEqual(core.import_source('lang_test.db', stage / 'lang_test.db')['new'], 1)
        self.assertEqual(core.record(1)['source'], 'Hello {Player} <b>%s</b>')
        self.assertEqual(core.record(1)['id_value'], '7')

    def test_encrypted_reader_never_reads_index(self):
        self.encrypted()
        original = self.package.open
        reads = []

        class Tracked:
            def __enter__(self):
                self.stream = original('rb')
                return self

            def __exit__(self, *args):
                self.stream.close()

            def seek(self, *args):
                return self.stream.seek(*args)

            def read(self, size):
                reads.append((self.stream.tell(), size))
                return self.stream.read(size)

        with patch('pathlib.Path.open', return_value=Tracked()):
            with self.assertRaisesRegex(UnsupportedPak, 'ENCRYPTED_INDEX'):
                read_databases(self.package)
        self.assertEqual(reads, [(self.package.stat().st_size - 221, 221)])

    def test_new_version_manifest_during_probe_is_rejected(self):
        from wuwaru.resource_probe import read_metadata
        folder = self.game / 'Client/Saved/Resources/3.7.0/ResManifest'
        folder.mkdir()
        (folder / 'ManifestLang_en_3.7.6.txt').write_text('QUJDRA==', encoding='utf-8')

        def add_version(path):
            result = read_metadata(path)
            if path.name == 'ManifestLang_en_3.7.6.txt':
                (folder / 'ManifestLang_en_3.7.7.txt').write_text('RUZHSA==', encoding='utf-8')
            return result

        with patch('wuwaru.resource_probe.read_metadata', side_effect=add_version):
            with self.assertRaisesRegex(RuntimeError, 'ResManifest'):
                probe_resources(self.installation)
