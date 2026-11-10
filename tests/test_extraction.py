import json
import subprocess
import sys
import threading
from unittest.mock import patch

from tests.test_discovery import FixtureCase, fixture
from tests.test_sqlite_source import database
from tests.pak_fixture import pak
from wuwaru.discovery import inspect_installation
from wuwaru.extraction import extract, import_databases
from wuwaru.manifest import Cancelled
from wuwaru.paths import UnsafePath


class ExtractionTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.game = fixture(self.home / 'game')
        self.installation = inspect_installation(self.game)
        self.source = self.home / 'db'
        self.source.mkdir()
        self.db = database(self.source / 'lang_test.db')
        self.package = 'Client/Content/Paks/base.pak'
        pak(self.game / self.package, {'lang_test.db': self.db.read_bytes()})
        self.output = self.home / 'staged'

    def test_extract_repeat_verify_and_source_unchanged(self):
        before = {str(p): p.read_bytes() for p in self.game.rglob('*') if p.is_file()}
        report = extract(self.installation, self.output, packages=[self.package])
        self.assertEqual((self.output / 'lang_test.db').read_bytes(), self.db.read_bytes())
        self.assertEqual(report['databases'][0]['validation']['rows'], 3)
        self.assertEqual(report['verification']['payloads'], 'independent_serialized_scan')
        self.assertEqual(report['source_binding'], 'SELECTED_PACKAGE_BYTES_VERIFIED')
        self.assertEqual(report['client_version_status'], 'UNKNOWN')
        self.assertFalse(report['complete_game_language_inventory'])
        self.assertFalse(report['installation_allowed'])
        self.assertEqual(json.loads((self.output / 'source-manifest.json').read_text('utf-8')),
                         json.loads(json.dumps(report)))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.game.rglob('*') if p.is_file()})

    def test_import_requires_exact_package_bytes(self):
        report = import_databases(self.installation, self.source, self.output,
                                  packages=[self.package])
        self.assertEqual(report['databases'][0]['name'], 'lang_test.db')
        self.assertTrue((self.output / 'lang_test.db').exists())
        self.db.unlink()
        database(self.db, ((99, 'different version'),))
        with self.assertRaises(ValueError):
            import_databases(self.installation, self.source, self.home / 'mismatch',
                             packages=[self.package])
        self.assertFalse((self.home / 'mismatch').exists())

    def test_bad_db_and_unlisted_package_leave_no_output(self):
        pak(self.game / self.package, {'lang_test.db': b'not sqlite'})
        with self.assertRaises(ValueError):
            extract(self.installation, self.output, packages=[self.package])
        with self.assertRaises(ValueError):
            extract(self.installation, self.output, packages=['../outside.pak'])
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.home.glob('.extract-*')))

    def test_destination_game_ancestor_existing_and_links(self):
        for output in (self.game / 'data', self.home, self.source):
            with self.subTest(output=output), self.assertRaises((UnsafePath, FileExistsError)):
                extract(self.installation, output, packages=[self.package])

    def test_cancel_disk_full_publish_failure_and_stale_snapshot(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            extract(self.installation, self.output, packages=[self.package], cancelled=event)
        with patch('wuwaru.extraction.shutil.disk_usage', return_value=(100, 100, 0)):
            with self.assertRaises(OSError):
                extract(self.installation, self.output, packages=[self.package])
        with patch('wuwaru.extraction.os.rename', side_effect=OSError('publish failure')):
            with self.assertRaises(OSError):
                extract(self.installation, self.output, packages=[self.package])
        with self.assertRaises(ValueError):
            extract(self.installation, self.output, packages=[self.package],
                    expected_inventory='0' * 64)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.home.glob('.extract-*')))

    def test_ambiguous_overlay_is_rejected(self):
        second = 'Client/Content/Paks/second.pak'
        pak(self.game / second, {'lang_test.db': self.db.read_bytes()})
        with self.assertRaises(ValueError):
            extract(self.installation, self.output, packages=[self.package, second])

    def test_source_mutation_during_second_read_is_rejected(self):
        from wuwaru.extraction import verify_payloads

        def mutate(*args, **kwargs):
            result = verify_payloads(*args, **kwargs)
            (self.game / self.package).write_bytes(b'changed')
            return result

        with patch('wuwaru.extraction.verify_payloads', side_effect=mutate):
            with self.assertRaises(RuntimeError):
                extract(self.installation, self.output, packages=[self.package])
        self.assertFalse(self.output.exists())

    def test_cli_extract(self):
        result = subprocess.run([sys.executable, '-m', 'wuwaru', 'extract', '--game',
                                 str(self.game), '--output', str(self.output), '--package',
                                 self.package], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_expected_scan_fingerprint_keeps_steam_build_evidence(self):
        game = fixture(self.home / 'Steam/steamapps/common/Wuthering Waves')
        pak(game / self.package, {'lang_test.db': self.db.read_bytes()})
        (game.parent.parent / 'appmanifest_3513350.acf').write_text(
            '"AppState" { "appid" "3513350" "installdir" "Wuthering Waves" "buildid" "123" }')
        scan = self.home / 'scan.json'
        result = subprocess.run([sys.executable, '-m', 'wuwaru', 'scan', '--game',
                                 str(game), '--output', str(scan)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        fingerprint = json.loads(scan.read_text('utf-8'))['inventory_fingerprint_sha256']
        result = subprocess.run([sys.executable, '-m', 'wuwaru', 'extract', '--game',
                                 str(game), '--output', str(self.output), '--package', self.package,
                                 '--expected-inventory', fingerprint], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_empty_extra_and_missing_databases_fail(self):
        for variant in ('empty', 'extra', 'missing'):
            with self.subTest(variant=variant):
                source = self.home / variant
                source.mkdir()
                if variant == 'extra':
                    (source / self.db.name).write_bytes(self.db.read_bytes())
                    (source / 'note.txt').write_text('not a database')
                if variant == 'missing':
                    database(source / 'lang_other.db')
                with self.assertRaises(ValueError):
                    import_databases(self.installation, source, self.output, packages=[self.package])
                self.assertFalse(self.output.exists())

    def test_junction_staging_and_import_are_rejected(self):
        outside = self.home / 'outside'
        outside.mkdir()
        link = self.home / 'link'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaises(UnsafePath):
                extract(self.installation, link / 'staged', packages=[self.package])
            with self.assertRaises(UnsafePath):
                import_databases(self.installation, link, self.output, packages=[self.package])
        finally:
            link.rmdir()
