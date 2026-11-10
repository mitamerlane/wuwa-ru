from tests.test_discovery import FixtureCase
from tests.pak_fixture import large_index_pak, pak
from wuwaru.pak_reader import read_databases, UnsupportedPak, V12ArchiveReader
from wuwaru.payload_verify import verify_payloads
from wuwaru.manifest import Cancelled
import random
import struct
import threading
from unittest.mock import mock_open, patch


class PakTests(FixtureCase):
    def test_indexes_with_over_400000_synthetic_entries(self):
        count = 400_001
        source = large_index_pak(self.home / 'large-index.pak', count)

        metadata = V12ArchiveReader(max_files=400_010,
                                    max_index_bytes=32 * 1024 * 1024,
                                    max_metadata_bytes=512 * 1024 * 1024).read_metadata(
                                        source, extensions={'.bin'}, offset=399_998, limit=3)

        self.assertEqual(metadata.active_entries, count)
        self.assertEqual(len(metadata.entries), 3)
        self.assertEqual([entry.path.rsplit('/', 1)[-1] for entry in metadata.entries],
                         ['e399998.bin', 'e399999.bin', 'e400000.bin'])

        with source.open('r+b') as stream:
            stream.seek(-221, 2)
            footer = stream.read(221)
            index_offset, primary_size = struct.unpack_from('<QQ', footer, 25)
            stream.seek(index_offset + primary_size)
            byte = stream.read(1)
            stream.seek(index_offset + primary_size)
            stream.write(bytes([byte[0] ^ 1]))
        with self.assertRaises(ValueError):
            V12ArchiveReader(max_files=400_010, max_index_bytes=32 * 1024 * 1024,
                             max_metadata_bytes=512 * 1024 * 1024).read_metadata(source)

    def test_reader_limits_can_be_raised_explicitly_and_kept_bounded(self):
        source = pak(self.home / 'configured-limit.pak',
                     {'one.bin': b'1', 'two.bin': b'2'})

        with patch('wuwaru.pak_reader.MAX_FILES', 1):
            with self.assertRaises(UnsupportedPak):
                V12ArchiveReader().read_metadata(source)

        metadata = V12ArchiveReader(max_files=2).read_metadata(source)
        self.assertEqual(len(metadata.entries), 2)

        with self.assertRaisesRegex(ValueError, 'budget'):
            V12ArchiveReader(max_files=2, max_metadata_bytes=1).read_metadata(source)
        with self.assertRaises(UnsupportedPak):
            V12ArchiveReader(max_files=2, max_index_bytes=1).read_metadata(source)

    def test_reader_filters_and_pages_without_changing_total_counts(self):
        source = pak(self.home / 'filtered.pak', {
            'one.bytes': b'1', 'two.db': b'2', 'three.bytes': b'3',
        })

        metadata = V12ArchiveReader(max_files=3).read_metadata(
            source, path_contains='one', extensions={'.bytes'}, offset=0, limit=1)

        self.assertEqual([entry.path.rsplit('/', 1)[-1] for entry in metadata.entries],
                         ['one.bytes'])
        self.assertEqual(metadata.active_entries, 3)

    def test_reader_cancellation_is_checked_before_index_work(self):
        source = pak(self.home / 'cancelled.pak', {'one.bin': b'1'})
        cancelled = threading.Event()
        cancelled.set()

        with self.assertRaisesRegex(Exception, 'отменена'):
            V12ArchiveReader(cancelled=cancelled).read_metadata(source)

    def test_reader_can_be_reused_after_cancelled_read(self):
        source = pak(self.home / 'cancel-then-retry.pak', {'one.bin': b'fixture'})
        cancelled = threading.Event()
        reader = V12ArchiveReader(cancelled=cancelled)
        cancelled.set()

        with self.assertRaises(Cancelled):
            reader.read_metadata(source)

        cancelled.clear()
        metadata = reader.read_metadata(source)
        self.assertEqual([entry.path.rsplit('/', 1)[-1] for entry in metadata.entries], ['one.bin'])

    def test_reader_cancellation_is_checked_during_index_walk(self):
        source = pak(self.home / 'cancelled-mid-read.pak',
                     {f'file-{i}.bin': b'x' for i in range(8)})
        cancelled = threading.Event()
        calls = 0

        def cancel_after_checks(event):
            nonlocal calls
            calls += 1
            if calls == 7:
                event.set()
            from wuwaru.manifest import check_cancel
            check_cancel(event)

        with patch('wuwaru.pak_reader.check_cancel', side_effect=cancel_after_checks):
            with self.assertRaises(Cancelled):
                V12ArchiveReader(cancelled=cancelled).read_metadata(source)

    def test_reads_realistic_universe_editor_mount_point(self):
        mount = '../../../Client/Content/Aki/Config/UniverseEditorConfig/'
        source = pak(self.home / 'universe.pak', {'table.bin': b'fixture'},
                     mount_point=mount)

        metadata = V12ArchiveReader().read_metadata(source)

        self.assertEqual(metadata.mount_point, mount)
        self.assertEqual([entry.path for entry in metadata.entries],
                         ['Client/Content/Aki/ConfigDB/en/table.bin'])

    def test_omits_pruned_paths_and_counts_them(self):
        source = pak(self.home / 'pruned.pak', {'active.bin': b'active'},
                     pruned_entries=('deleted.bin',))

        metadata = V12ArchiveReader().read_metadata(source)

        self.assertEqual([entry.path for entry in metadata.entries],
                         ['Client/Content/Aki/ConfigDB/en/active.bin'])
        self.assertEqual(metadata.pruned_entries, 1)
        self.assertEqual(metadata.pruned_paths,
                         ('Client/Content/Aki/ConfigDB/en/deleted.bin',))

    def test_accepts_only_zero_2048_byte_payload_alignment(self):
        source = pak(self.home / 'aligned.pak',
                     {'one.bin': b'one', 'two.bin': b'two'}, alignment=2048)
        metadata = V12ArchiveReader().read_metadata(source)
        self.assertEqual([entry.path.rsplit('/', 1)[-1] for entry in metadata.entries],
                         ['one.bin', 'two.bin'])

        damaged = bytearray(source.read_bytes())
        damaged[2048 - 1] = 1
        source.write_bytes(damaged)
        with self.assertRaisesRegex(ValueError, 'padding'):
            V12ArchiveReader().read_metadata(source)

    def test_archive_file_quota_is_reported_as_unsupported_not_corruption(self):
        source = pak(self.home / 'over-limit.pak', {'one.bin': b'1', 'two.bin': b'2'})
        with patch('wuwaru.pak_reader.MAX_FILES', 1):
            with self.assertRaises(UnsupportedPak) as error:
                V12ArchiveReader().read_metadata(source)

        self.assertEqual(error.exception.code.value, 'UNSUPPORTED_FORMAT')
        self.assertIn('2', str(error.exception))

    def test_repeat_read_matches_exact_bytes(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'SQLite fixture'})
        a = read_databases(source)
        self.assertEqual(a, read_databases(source))
        self.assertEqual(a['lang_a.db'], b'SQLite fixture')

    def test_corrupt_payload_index_footer_and_encryption_fail(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'fixture'})
        original = source.read_bytes()
        for position in (53, 65, -221, -205):
            data = bytearray(original)
            data[position] ^= 1
            source.write_bytes(data)
            with self.subTest(position=position), self.assertRaises(ValueError):
                read_databases(source)
        source.write_bytes(b'unknown')
        with self.assertRaises(UnsupportedPak):
            read_databases(source)

    def test_paths_and_case_collisions_fail(self):
        for files in ({'../lang_a.db': b'a'}, {'lang_a.db:ads': b'a'},
                      {'lang_a.db': b'a', 'LANG_A.db': b'b'}, {'other.txt': b'a'}):
            with self.subTest(files=files):
                source = pak(self.home / 'a.pak', files)
                with self.assertRaises(ValueError):
                    read_databases(source)

    def test_limits(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'long fixture'})
        with self.assertRaises(ValueError):
            read_databases(source, max_total=1)

    def test_short_footer_read_is_a_validation_error(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'fixture'})
        with patch('pathlib.Path.open', mock_open(read_data=b'x')):
            with self.assertRaises(ValueError):
                read_databases(source)
            with self.assertRaises(ValueError):
                verify_payloads(source, {'lang_a.db': b'fixture'})

    def test_independent_walk_rejects_missing_extra_and_changed_bytes(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'one', 'lang_b.db': b'two'})
        verify_payloads(source, {'lang_a.db': b'one', 'lang_b.db': b'two'})
        for expected in ({'lang_a.db': b'one'}, {'lang_a.db': b'one', 'lang_b.db': b'bad'}, {}):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                verify_payloads(source, expected)

    def test_deterministic_mutated_archives_fail_closed(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'fixture'})
        original = source.read_bytes()
        rng = random.Random(20261024)
        for _ in range(100):
            data = bytearray(original)
            position = rng.randrange(len(data))
            data[position] ^= 1 << rng.randrange(8)
            source.write_bytes(data)
            with self.subTest(position=position), self.assertRaises(ValueError):
                read_databases(source)
