"""SELF-1: public behavior on synthetic bytes; no upstream executable or data."""
import hashlib
import struct
import os
import subprocess
import tracemalloc
from dataclasses import replace
from threading import Event
from unittest.mock import patch

from tests.pak_fixture import pak, streamed_pak, PREFIX
from tests.test_discovery import FixtureCase, fixture
from wuwaru.core_contracts import CoreError, ErrorCode
from wuwaru.manifest import Cancelled
from wuwaru.pak_reader import V12ArchiveReader, read_databases


class SelfCoreTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.reader = V12ArchiveReader()

    def test_metadata_extract_verify_unicode_and_source_unchanged(self):
        content = 'Ёжик {Player}\n日本語'.encode()
        source = pak(self.home / 'a.pak', {'текст_日本.txt': content})
        original = source.read_bytes()
        self.assertTrue(self.reader.probe(source).supported)
        meta = self.reader.read_metadata(source)
        self.assertEqual((meta.version, meta.mount_point, meta.total_size),
                         (12, '../../../', len(content)))
        entry, = self.reader.list_entries(source)
        self.assertEqual(entry.path, PREFIX + 'текст_日本.txt')
        for folder in ('first', 'second'):
            result = self.reader.extract_entry(entry, self.home / folder)
            self.assertEqual(result.destination.read_bytes(), content)
            self.assertEqual(result.sha256, hashlib.sha256(content).hexdigest())
        verified = self.reader.verify_archive(source)
        self.assertEqual(verified.archive_sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(verified.entries[0].sha1, hashlib.sha1(content).hexdigest())
        self.assertEqual(source.read_bytes(), original)
        with self.assertRaises(ValueError):
            read_databases(source)  # legacy SQLite adapter remains deliberately strict

    def test_existing_target_game_destination_and_forged_entry_fail(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'payload'})
        entry, = self.reader.list_entries(source)
        root = self.home / 'out'
        target = root / entry.path
        target.parent.mkdir(parents=True)
        target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            self.reader.extract_entry(entry, root)
        self.assertEqual(target.read_bytes(), b'keep')
        with self.assertRaises(ValueError):
            self.reader.extract_entry(entry, fixture(self.home / 'game'))
        with self.assertRaises(CoreError) as cm:
            self.reader.extract_entry(replace(entry, offset=entry.offset + 1), self.home / 'bad')
        self.assertEqual(cm.exception.code, ErrorCode.RESOURCE_MISMATCH)
        self.assertFalse((self.home / 'bad').exists())

    def test_structured_footer_encryption_codec_and_bounds_errors(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'x'})
        original = source.read_bytes()
        cases = [(17, b'XXXX', ErrorCode.UNSUPPORTED_FORMAT),
                 (16, b'\x01', ErrorCode.KEY_UNAVAILABLE),
                 (25, struct.pack('<Q', len(original) * 2), ErrorCode.CORRUPTED_ARCHIVE),
                 (16, b'\x02', ErrorCode.CORRUPTED_ARCHIVE)]
        for offset, data, code in cases:
            changed = bytearray(original)
            changed[len(original) - 221 + offset:len(original) - 221 + offset + len(data)] = data
            source.write_bytes(changed)
            with self.subTest(code=code):
                probe = self.reader.probe(source)
                self.assertFalse(probe.supported)
                self.assertEqual(probe.code, code)
                with self.assertRaises(CoreError) as cm:
                    self.reader.verify_archive(source)
                self.assertEqual(cm.exception.code, code)
        source.write_bytes(b'x' * 100)
        self.assertEqual(self.reader.probe(source).code, ErrorCode.UNSUPPORTED_FORMAT)

    def test_corrupted_payload_leaves_no_file_and_preserves_archive(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'payload'})
        changed = bytearray(source.read_bytes())
        changed[53] ^= 1
        source.write_bytes(changed)
        entry, = self.reader.list_entries(source)  # metadata does not assert payload validity
        root = self.home / 'out'
        with self.assertRaises(CoreError) as cm:
            self.reader.extract_entry(entry, root)
        self.assertEqual(cm.exception.code, ErrorCode.CORRUPTED_ARCHIVE)
        self.assertFalse((root / entry.path).exists())
        self.assertEqual(list(root.rglob('*.tmp')), [])
        with self.assertRaises(CoreError):
            self.reader.verify_archive(source)
        self.assertEqual(source.read_bytes(), changed)

    def test_unsafe_names_and_collisions(self):
        for files in ({'../escape': b'x'}, {'a:ads': b'x'}, {'/absolute': b'x'},
                      {'A': b'x', 'a': b'y'}, {'CON.txt': b'x'}, {'a/b': b'x'}):
            source = pak(self.home / 'a.pak', files)
            with self.subTest(files=files), self.assertRaises(ValueError):
                self.reader.list_entries(source)

    def test_limits_and_stale_metadata(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'payload'})
        with self.assertRaises(CoreError):
            V12ArchiveReader(max_entry_size=3).read_metadata(source)
        with self.assertRaises(CoreError):
            V12ArchiveReader(max_total=3).read_metadata(source)
        entry, = self.reader.list_entries(source)
        pak(source, {'lang_a.db': b'different bytes'})
        with self.assertRaises(CoreError) as cm:
            self.reader.extract_entry(entry, self.home / 'out')
        self.assertEqual(cm.exception.code, ErrorCode.RESOURCE_MISMATCH)
        self.assertFalse((self.home / 'out').exists())

    def test_cancellation_publishes_nothing(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'x'})
        entry, = self.reader.list_entries(source)
        event = Event()
        event.set()
        with self.assertRaises(Cancelled):
            V12ArchiveReader(cancelled=event).extract_entry(entry, self.home / 'out')
        self.assertFalse((self.home / 'out').exists())

    def test_utf16_strings_and_empty_payload(self):
        source = pak(self.home / 'a.pak', {'текст.txt': b'', 'b.txt': b'b'}, utf16=True)
        entries = self.reader.list_entries(source)
        self.assertEqual(entries[0].size, 0)
        result = self.reader.extract_entry(entries[0], self.home / 'out')
        self.assertEqual(result.destination.read_bytes(), b'')
        self.assertEqual(len(self.reader.verify_archive(source).entries), 2)

    def test_junction_destination_is_rejected(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'x'})
        entry, = self.reader.list_entries(source)
        outside = self.home / 'outside'
        outside.mkdir()
        link = self.home / 'link'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: os.rmdir(link))
        with self.assertRaises(ValueError):
            self.reader.extract_entry(entry, link)
        self.assertEqual(list(outside.iterdir()), [])

    def test_streaming_32_mib_payload_has_bounded_python_memory(self):
        size = 32 * 1024 * 1024
        source = streamed_pak(self.home / 'large.pak', size)
        tracemalloc.start()
        try:
            entry, = self.reader.list_entries(source)
            result = self.reader.extract_entry(entry, self.home / 'out')
            verified = self.reader.verify_archive(source)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result.destination.stat().st_size, size)
        self.assertEqual(verified.entries[0].sha256, result.sha256)
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_sparse_over_4_gib_metadata_without_reading_payload(self):
        size = 0xffffffff
        source = streamed_pak(self.home / 'sparse.pak', size, sparse=True)
        reader = V12ArchiveReader(max_entry_size=size, max_total=size)
        tracemalloc.start()
        try:
            metadata = reader.read_metadata(source)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertGreater(source.stat().st_size, 2 ** 32)
        self.assertEqual(metadata.entries[0].size, size)
        self.assertLess(peak, 2 * 1024 * 1024)
        with self.assertRaises(CoreError):
            self.reader.read_metadata(source)  # conservative default quotas

    def test_cancel_during_stream_and_publication_failure_leave_no_file(self):
        source = streamed_pak(self.home / 'a.pak', 3 * 1024 * 1024)
        entry, = self.reader.list_entries(source)
        from wuwaru.manifest import check_cancel
        event = Event()
        calls = 0

        def cancel_after_chunk(flag):
            nonlocal calls
            calls += 1
            if calls == 2:
                event.set()
            check_cancel(event)

        real_copy = self.reader._copy_entry

        def cancelling_copy(*args):
            with patch('wuwaru.pak_reader.check_cancel', side_effect=cancel_after_chunk):
                return real_copy(*args)

        root = self.home / 'out'
        with patch.object(self.reader, '_copy_entry', side_effect=cancelling_copy):
            with self.assertRaises(Cancelled):
                self.reader.extract_entry(entry, root)
        self.assertFalse((root / entry.path).exists())
        self.assertEqual(list(root.rglob('*.tmp')), [])
        with patch('wuwaru.pak_reader.os.rename', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.reader.extract_entry(entry, root)
        self.assertFalse((root / entry.path).exists())
        self.assertEqual(list(root.rglob('*.tmp')), [])

    def test_declared_entry_codec_reports_decompression_unavailable(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'x'})
        raw = bytearray(source.read_bytes())
        index = struct.unpack_from('<Q', raw, len(raw) - 221 + 25)[0]
        size = struct.unpack_from('<Q', raw, len(raw) - 221 + 33)[0]
        # Known compression-method bits in the encoded u32, method slot 1.
        struct.pack_into('<I', raw, index + size - 17, 0xe0800000)
        raw[-180:-160] = hashlib.sha1(raw[index:index + size]).digest()
        source.write_bytes(raw)
        with self.assertRaises(CoreError) as cm:
            self.reader.read_metadata(source)
        self.assertEqual(cm.exception.code, ErrorCode.DECOMPRESSION_UNAVAILABLE)

    def test_frozen_known_bytes_without_calling_a_writer(self):
        # Frozen 486-byte synthetic vector; payload abc, SHA-1 is the standard abc vector.
        raw = bytes.fromhex(
            '00000000000000000300000000000000030000000000000000000000a9993e364706816aba3e25717850c26c9cd0d89d'
            '00000000006162630a0000002e2e2f2e2e2f2e2e2f0001000000000000000000000001000000b7000000000000001400'
            '000000000000aeed878894f89f954c676fe9288a7cd829596da801000000cb000000000000003e000000000000003286'
            '4a723542da59761ee57b1d733777b731cc000d000000000000e000030000000000000000000000010000008402f810be'
            'f8901800000000000000000100000020000000436c69656e742f436f6e74656e742f416b692f436f6e66696744422f65'
            '6e2f00010000000a0000006c616e675f612e646200000000000000000000000000000000000000000000e1126f5a0c00'
            '000038000000000000007f000000000000001a8960b28472602cd099a760f62c54886a7f3298'
        ) + bytes(160)
        self.assertEqual(len(raw), 486)
        source = self.home / 'golden.pak'
        source.write_bytes(raw)
        entry, = self.reader.list_entries(source)
        self.assertEqual((entry.offset, entry.size, entry.sha1),
                         (0, 3, 'a9993e364706816aba3e25717850c26c9cd0d89d'))
        result = self.reader.extract_entry(entry, self.home / 'out')
        self.assertEqual(result.destination.read_bytes(), b'abc')

    def test_valid_index_hash_cannot_hide_invalid_entry_offset(self):
        source = pak(self.home / 'a.pak', {'lang_a.db': b'x'})
        raw = bytearray(source.read_bytes())
        index = struct.unpack_from('<Q', raw, len(raw) - 221 + 25)[0]
        size = struct.unpack_from('<Q', raw, len(raw) - 221 + 33)[0]
        struct.pack_into('<I', raw, index + size - 8, 1)
        raw[-180:-160] = hashlib.sha1(raw[index:index + size]).digest()
        source.write_bytes(raw)
        with self.assertRaises(CoreError) as cm:
            self.reader.read_metadata(source)
        self.assertEqual(cm.exception.code, ErrorCode.CORRUPTED_ARCHIVE)
