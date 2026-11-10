"""SELF-2 behavior on independent synthetic fixtures, no installed game access."""
import hashlib
import random
import struct
import tracemalloc
import zlib
from threading import Event
from unittest.mock import patch

from tests.encrypted_pak_fixture import make_pak, TEST_KEY, TEST_GUID
from tests.test_discovery import FixtureCase
from wuwaru.core_contracts import CoreError, ErrorCode
from wuwaru.pak_reader import V12ArchiveReader
from wuwaru.manifest import Cancelled


class TestKeys:
    """Caller authorizes only our public synthetic key for a specific fixture."""
    def __init__(self, key=TEST_KEY):
        self.key = key
        self.requests = []

    def get_key(self, archive, key_guid):
        self.requests.append((archive, key_guid))
        return self.key


class EncryptedIndexTests(FixtureCase):
    def test_nist_aes256_ecb_known_answer_and_alignment(self):
        # NIST SP 800-38A F.1.6, public test vector (not a game key).
        from wuwaru.index_crypto import decrypt_index
        key = bytes.fromhex('603deb1015ca71be2b73aef0857d7781'
                            '1f352c073b6108d72d9810a30914dff4')
        cipher = bytes.fromhex('f3eed1bdb5d2a03c064b5a7e3db181f8')
        self.assertEqual(decrypt_index(cipher, key),
                         bytes.fromhex('6bc1bee22e409f96e93d7e117393172a'))
        for data in (b'', cipher[:-1]):
            with self.assertRaises(CoreError) as cm:
                decrypt_index(data, key)
            self.assertEqual(cm.exception.code, ErrorCode.CORRUPTED_ARCHIVE)

    def test_missing_crypto_dependency_is_structured_and_plain_still_works(self):
        import builtins
        original_import = builtins.__import__

        def no_crypto(name, *args, **kwargs):
            if name.startswith('cryptography'):
                raise ImportError
            return original_import(name, *args, **kwargs)

        source = make_pak(self.home / 'a.pak')
        plain = make_pak(self.home / 'plain.pak', encrypted=False)
        with patch('builtins.__import__', side_effect=no_crypto):
            reader = V12ArchiveReader(key_provider=TestKeys())
            self.assertEqual(reader.probe(source).code, ErrorCode.ENCRYPTED_INDEX)
            self.assertTrue(reader.probe(plain).supported)

    def test_encrypted_paths_index_limits_and_cancel_before_payload(self):
        for name in ('../escape', 'a:ads', '/absolute', 'CON.txt'):
            source = make_pak(self.home / 'a.pak', name=name)
            self.assertEqual(V12ArchiveReader(key_provider=TestKeys()).probe(source).code,
                             ErrorCode.CORRUPTED_ARCHIVE)
        source = make_pak(self.home / 'a.pak', primary_edit=lambda p: struct.pack_into('<Q', p, 38, 1 << 40))
        self.assertEqual(V12ArchiveReader(key_provider=TestKeys()).probe(source).code,
                         ErrorCode.CORRUPTED_ARCHIVE)
        event = Event()

        class CancellingKeys(TestKeys):
            def get_key(self, archive, key_guid):
                event.set()
                return super().get_key(archive, key_guid)

        make_pak(source)
        with self.assertRaises(Cancelled):
            V12ArchiveReader(key_provider=CancellingKeys(), cancelled=event).read_metadata(source)

    def test_plain_and_aes_extract_exact_bytes_and_hashes(self):
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                source = make_pak(self.home / 'a.pak', encrypted=encrypted)
                original = source.read_bytes()
                keys = TestKeys()
                reader = V12ArchiveReader(key_provider=keys)
                entry, = reader.list_entries(source)
                result = reader.extract_entry(entry, self.home / str(encrypted))
                self.assertEqual(result.destination.read_bytes(), b'abc')
                self.assertEqual(result.sha256, hashlib.sha256(b'abc').hexdigest())
                self.assertEqual(reader.verify_archive(source).archive_sha256,
                                 hashlib.sha256(original).hexdigest())
                self.assertEqual(source.read_bytes(), original)
                if encrypted:
                    self.assertEqual(keys.requests[0], (source.absolute(), TEST_GUID))
                else:
                    self.assertEqual(keys.requests, [])

    def test_missing_invalid_and_wrong_key_fail_without_key_disclosure(self):
        source = make_pak(self.home / 'a.pak')
        for key, code in ((None, ErrorCode.KEY_UNAVAILABLE),
                          (b'short', ErrorCode.KEY_UNAVAILABLE),
                          (bytes(reversed(TEST_KEY)), ErrorCode.CORRUPTED_ARCHIVE)):
            with self.subTest(code=code):
                reader = V12ArchiveReader(key_provider=TestKeys(key))
                self.assertEqual(reader.probe(source).code, code)
                with self.assertRaises(CoreError) as cm:
                    reader.read_metadata(source)
                self.assertNotIn(str(key), str(cm.exception))
        self.assertEqual(V12ArchiveReader().probe(source).code, ErrorCode.KEY_UNAVAILABLE)

    def test_corrupted_ciphertext_and_valid_hash_invalid_structure_are_rejected(self):
        source = make_pak(self.home / 'a.pak')
        raw = bytearray(source.read_bytes())
        raw[56] ^= 128
        source.write_bytes(raw)
        reader = V12ArchiveReader(key_provider=TestKeys())
        self.assertEqual(reader.probe(source).code, ErrorCode.CORRUPTED_ARCHIVE)
        # Valid AES and SHA-1 do not turn arbitrary plaintext into an index.
        make_pak(source, primary_edit=lambda p: p.__setitem__(slice(None), bytes(len(p))))
        self.assertFalse(reader.probe(source).supported)
        make_pak(source, padding_edit=True)
        self.assertEqual(reader.probe(source).code, ErrorCode.CORRUPTED_ARCHIVE)

    def test_provider_exception_is_sanitized(self):
        source = make_pak(self.home / 'a.pak')
        keys = TestKeys()
        with patch.object(keys, 'get_key', side_effect=RuntimeError('sensitive provider value')):
            with self.assertRaises(CoreError) as cm:
                V12ArchiveReader(key_provider=keys).read_metadata(source)
        self.assertNotIn('sensitive', str(cm.exception))
        self.assertTrue(cm.exception.__suppress_context__)


class CompressionTests(FixtureCase):
    def test_incompressible_one_mib_blocks(self):
        data = random.Random(20261025).randbytes(2 * 1024 * 1024 + 1)
        source = make_pak(self.home / 'a.pak', data, codec='Zlib', block_size=1024 * 1024)
        reader = V12ArchiveReader(key_provider=TestKeys())
        entry, = reader.list_entries(source)
        self.assertGreater(entry.blocks[0][1] - entry.blocks[0][0], 1024 * 1024)
        result = reader.extract_entry(entry, self.home / 'out')
        self.assertEqual(result.destination.read_bytes(), data)

    def test_compression_block_metadata_has_an_archive_budget(self):
        packed = [zlib.compress(b'x')] * 8193
        source = make_pak(self.home / 'a.pak', b'x' * 8193, codec='Zlib',
                          block_size=1, packed_blocks=packed)
        self.assertEqual(V12ArchiveReader(key_provider=TestKeys()).probe(source).code,
                         ErrorCode.CORRUPTED_ARCHIVE)

    def test_frozen_zlib_bytes_and_encrypted_sqlite_adapter(self):
        source = make_pak(self.home / 'a.pak', codec='Zlib',
                          packed_blocks=[bytes.fromhex('789c4b4c4a0600024d0127')])
        self.assertEqual(V12ArchiveReader(key_provider=TestKeys()).verify_archive(source).entries[0].sha1,
                         'a9993e364706816aba3e25717850c26c9cd0d89d')
        from tests.test_sqlite_source import database
        from wuwaru.pak_reader import read_databases
        from wuwaru.translation_db import TranslationDB
        db = database(self.home / 'lang_test.db', ((7, 'Hello {Player}'),))
        source = make_pak(source, db.read_bytes(), codec='Gzip',
                          name='Client/Content/Aki/ConfigDB/en/lang_test.db')
        contents = read_databases(source, key_provider=TestKeys())
        self.assertEqual(contents['lang_test.db'], db.read_bytes())
        copy = self.home / 'copy.db'
        copy.write_bytes(contents['lang_test.db'])
        translations = TranslationDB(self.home / 'workspace')
        self.addCleanup(translations.close)
        result = translations.import_source('synthetic-encrypted.db', copy)
        self.assertEqual(result['new'], 1)

    def test_unused_codec_slots_and_plain_64bit_are_accepted(self):
        source = make_pak(self.home / 'a.pak', encrypted=False, wide=True)
        raw = bytearray(source.read_bytes())
        raw[-160:-128] = b'Oodle' + bytes(27)
        source.write_bytes(raw)
        self.assertTrue(V12ArchiveReader().probe(source).supported)
        self.assertEqual(V12ArchiveReader().verify_archive(source).entries[0].size, 3)

    def test_zlib_gzip_single_multi_explicit_and_64_bit_extract(self):
        for codec, size, block_size, wide, encrypted in (
                ('Zlib', 3000, 65536, False, False),
                ('Gzip', 170000, 65536, False, True),
                ('Zlib', 3001, 1000, True, True),
                ('Gzip', 3000, 65536, True, False)):
            with self.subTest(codec=codec, wide=wide, block_size=block_size):
                data = bytes(range(251)) * (size // 251) + b'x' * (size % 251)
                source = make_pak(self.home / 'a.pak', data, codec=codec,
                                  block_size=block_size, wide=wide, encrypted=encrypted)
                archive_bytes = source.read_bytes()
                reader = V12ArchiveReader(key_provider=TestKeys())
                self.assertEqual(reader.read_metadata(source).size, len(archive_bytes))
                entry, = reader.list_entries(source)
                self.assertEqual((entry.compression, entry.size), (codec, len(data)))
                out = reader.extract_entry(entry, self.home / f'{codec}-{wide}-{block_size}')
                self.assertEqual(out.destination.read_bytes(), data)
                self.assertEqual(out.sha1, hashlib.sha1(data).hexdigest())
                verified = reader.verify_archive(source)
                self.assertEqual(verified.entries[0].sha256, hashlib.sha256(data).hexdigest())
                self.assertEqual(verified.archive_sha256, hashlib.sha256(archive_bytes).hexdigest())

    def test_oodle_unknown_codec_and_missing_slot_fail_structurally(self):
        for codec in ('Oodle', 'UnknownCodec'):
            source = make_pak(self.home / 'a.pak', codec=codec)
            self.assertEqual(V12ArchiveReader(key_provider=TestKeys()).probe(source).code,
                             ErrorCode.DECOMPRESSION_UNAVAILABLE)

    def test_bad_stream_bomb_trailing_and_truncation_publish_nothing(self):
        # Valid metadata + stored SHA-1, but invalid compressed streams/decoded sizes.
        for blocks in ([b'bad'], [zlib.compress(b'x' * (2 * 1024 * 1024))],
                       [zlib.compress(b'abc') + b'extra'], [zlib.compress(b'abc')[:-1]],
                       [zlib.compress(b'a')]):
            source = make_pak(self.home / 'a.pak', codec='Zlib', packed_blocks=blocks)
            reader = V12ArchiveReader(key_provider=TestKeys())
            entry, = reader.list_entries(source)
            root = self.home / 'out'
            tracemalloc.start()
            with self.assertRaises(CoreError) as cm:
                reader.extract_entry(entry, root)
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            self.assertEqual(cm.exception.code, ErrorCode.CORRUPTED_ARCHIVE)
            self.assertLess(peak, 5 * 1024 * 1024)
            self.assertFalse((root / entry.path).exists())
            self.assertEqual(list(root.rglob('*.tmp')), [])

    def test_bad_compression_bounds_and_uncompressed_quota_rejected_before_output(self):
        reader = V12ArchiveReader(key_provider=TestKeys(), max_entry_size=100000)
        for edit in (lambda h: struct.pack_into('<Q', h, 52, 0),
                     lambda h: struct.pack_into('<Q', h, 60, 1 << 62),
                     lambda h: struct.pack_into('<I', h, 48, 65536)):
            source = make_pak(self.home / 'a.pak', b'x' * 90000, codec='Zlib', header_edit=edit)
            with self.subTest(edit=edit):
                self.assertEqual(reader.probe(source).code, ErrorCode.CORRUPTED_ARCHIVE)
        source = make_pak(self.home / 'a.pak', b'x' * 100001, codec='Zlib')
        self.assertEqual(reader.probe(source).code, ErrorCode.CORRUPTED_ARCHIVE)

    def test_large_compressed_payload_memory_and_cancellation(self):
        size = 32 * 1024 * 1024
        source = make_pak(self.home / 'a.pak', bytes(size), codec='Zlib')
        event = Event()
        reader = V12ArchiveReader(key_provider=TestKeys(), cancelled=event)
        tracemalloc.start()
        entry, = reader.list_entries(source)
        result = reader.extract_entry(entry, self.home / 'out')
        verified = reader.verify_archive(source)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertEqual(result.size, size)
        self.assertEqual(result.destination.stat().st_size, size)
        self.assertEqual(verified.entries[0].sha256, result.sha256)
        with result.destination.open('rb') as stream:
            self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), result.sha256)
        self.assertLess(peak, 6 * 1024 * 1024)
        from wuwaru.compression import CompressionProvider
        provider = CompressionProvider()
        original = provider.decode_block

        def cancelling(*args):
            value = original(*args)
            event.set()
            return value

        with patch.object(provider, 'decode_block', side_effect=cancelling):
            reader = V12ArchiveReader(key_provider=TestKeys(), cancelled=event,
                                      compression_provider=provider)
            with self.assertRaises(Cancelled):
                reader.extract_entry(entry, self.home / 'cancel')
        self.assertFalse((self.home / 'cancel' / entry.path).exists())
        self.assertEqual(list((self.home / 'cancel').rglob('*.tmp')), [])
