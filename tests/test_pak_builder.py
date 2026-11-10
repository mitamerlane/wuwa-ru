import hashlib
import sqlite3
from threading import Event
from unittest.mock import patch

from tests.test_discovery import FixtureCase, fixture
from tests.test_sqlite_source import database
from tests.pak_fixture import pak
from wuwaru.pak_builder import build_package, verify_package
from wuwaru.pak_reader import PREFIX
from wuwaru.manifest import Cancelled


class PackageTests(FixtureCase):
    def test_directory_publication_retries_transient_windows_lock_exclusively(self):
        from wuwaru.pak_builder import _publish_directory
        import os
        source, target = self.home / 'verified', self.home / 'published'
        source.mkdir()
        (source / 'marker').write_bytes(b'keep')
        transient = PermissionError('temporary Windows denial')
        transient.winerror = 5
        real_rename = os.rename
        calls = []

        def locked_once(left, right):
            calls.append((left, right))
            if len(calls) == 1:
                raise transient
            real_rename(left, right)

        with patch('wuwaru.pak_builder.os.rename', side_effect=locked_once), \
                patch('wuwaru.pak_builder.time.sleep'):
            _publish_directory(source, target, None)
        self.assertEqual(len(calls), 2)
        self.assertEqual((target / 'marker').read_bytes(), b'keep')
        source.mkdir()
        with self.assertRaises(FileExistsError):
            _publish_directory(source, target, None)
        target = self.home / 'denied'
        with patch('wuwaru.pak_builder.os.rename', side_effect=transient) as rename, \
                patch('wuwaru.pak_builder.time.sleep'):
            with self.assertRaises(PermissionError):
                _publish_directory(source, target, None)
        self.assertEqual(rename.call_count, 4)
        event = Event()
        with patch('wuwaru.pak_builder.os.rename', side_effect=transient), \
                patch('wuwaru.pak_builder.time.sleep', side_effect=lambda _: event.set()):
            with self.assertRaises(Cancelled):
                _publish_directory(source, target, event)
        self.assertTrue(source.exists())
        self.assertFalse(target.exists())

    def prepare(self):
        source = self.home / 'source'
        folder = source / PREFIX
        folder.mkdir(parents=True)
        database(folder / 'lang_test.db')
        return source

    def test_roundtrip_names_schema_cyrillic_hashes_and_reproducibility(self):
        source = self.prepare()
        original = source / PREFIX / 'lang_test.db'
        with sqlite3.connect(original) as db:
            db.execute("UPDATE Text SET Content='Ёжик — русский текст' WHERE Id=2")
        db.close()
        result = build_package(source, self.home / 'first')
        other = build_package(source, self.home / 'second')
        self.assertEqual(result['pak_sha256'], other['pak_sha256'])
        extracted = self.home / 'first/verified' / PREFIX / 'lang_test.db'
        self.assertEqual(original.read_bytes(), extracted.read_bytes())
        self.assertEqual(result['databases'][0]['sha256'], hashlib.sha256(extracted.read_bytes()).hexdigest())
        self.assertFalse(result['installation_allowed'])
        with self.assertRaises(FileExistsError):
            build_package(source, self.home / 'first')

    def test_corruption_encryption_and_mismatch_fail_before_extract(self):
        source = self.prepare()
        build_package(source, self.home / 'good')
        data = (self.home / 'good/synthetic.pak').read_bytes()
        expected = {'lang_test.db': (source / PREFIX / 'lang_test.db').read_bytes()}
        for index, blob in enumerate((data[:100], data[:60] + bytes([data[60] ^ 1]) + data[61:],
                                      data[:-1], data[:-205] + b'\x01' + data[-204:])):
            path = self.home / f'bad{index}.pak'
            path.write_bytes(blob)
            target = self.home / f'extract{index}'
            with self.assertRaises((ValueError, OSError)):
                verify_package(path, expected, target)
            self.assertFalse(target.exists())
        with self.assertRaises(ValueError):
            verify_package(self.home / 'good/synthetic.pak', {'lang_other.db': expected['lang_test.db']}, self.home / 'wrong')
        self.assertFalse((self.home / 'wrong').exists())

    def test_bad_input_cancel_and_failure_do_not_publish(self):
        source = self.prepare()
        bad = source / PREFIX / 'bad.db'
        bad.write_bytes(b'not sqlite')
        with self.assertRaises(ValueError):
            build_package(source, self.home / 'bad')
        bad.unlink()
        event = Event(); event.set()
        with self.assertRaises(Cancelled):
            build_package(source, self.home / 'cancel', cancelled=event)
        with patch('wuwaru.pak_builder.verify_payloads', side_effect=ValueError('verification failed')):
            with self.assertRaises(ValueError):
                build_package(source, self.home / 'failed')
        for name in ('bad', 'cancel', 'failed'):
            self.assertFalse((self.home / name).exists())
        self.assertFalse(list(self.home.glob('.pak-*')))
        game = fixture(self.home / 'game')
        with self.assertRaises(ValueError):
            build_package(source, game / 'package')

    def test_valid_archive_with_invalid_sqlite_does_not_extract(self):
        expected = {'lang_broken.db': b'not a SQLite database'}
        archive = pak(self.home / 'invalid-sqlite.pak', expected)
        with self.assertRaises(ValueError):
            verify_package(archive, expected, self.home / 'invalid-sqlite')
        self.assertFalse((self.home / 'invalid-sqlite').exists())
