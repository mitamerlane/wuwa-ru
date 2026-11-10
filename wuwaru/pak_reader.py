"""Bounded reader for the documented WuWa V12 subset.

Format studied in GishReloaded/WuWaRu@ee6c16e/wuwa_pak.py (GPL-3.0-or-later).
Encoded bit layout also studied in CUE4Parse@db808145/FPakEntry.cs (Apache-2.0).
The project owns this reader; it is not clean-room. Synthetic AES-256 indexes
and Zlib/Gzip blocks are tested; original game compatibility is unverified.
The supported layout is documented in docs/SELF_CORE_FORMAT.md.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import struct
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO

from .core_contracts import (ArchiveEntry, ArchiveMetadata, ArchiveVerification,
                             CoreError, EntryVerification, ErrorCode, ProbeResult)

from .manifest import check_cancel
from .paths import relative_parts, reject_links
from .sqlite_source import MAX_DATABASE
from .resource_probe import decode_footer
from .index_crypto import KeyProvider, authorized_key, decrypt_index
from .compression import CompressionProvider, MAX_BLOCK_INPUT, MAX_BLOCK_OUTPUT

FOOTER = 221
MAX_INDEX = 16 * 1024 * 1024
MAX_FILES = 4096
MAX_INDEX_LIMIT = 128 * 1024 * 1024
DEFAULT_METADATA_BUDGET = 512 * 1024 * 1024
MAX_METADATA_BUDGET = 1024 * 1024 * 1024
MAX_BLOCKS = 8192  # aggregate metadata quota, independent of compressed sizes
MAX_TOTAL = 512 * 1024 * 1024
PREFIX = 'Client/Content/Aki/ConfigDB/en/'


class UnsupportedPak(CoreError):
    def __init__(self, message: str, code: ErrorCode = ErrorCode.UNSUPPORTED_FORMAT):
        super().__init__(code, message)


class Cursor:
    def __init__(self, data: bytes, *, aligned: bool = False):
        self.stream = io.BytesIO(data)
        self.aligned = aligned

    def take(self, size: int) -> bytes:
        data = self.stream.read(size)
        if len(data) != size:
            raise ValueError('Обрезанный индекс PAK')
        return data

    def number(self, fmt: str = 'I') -> int:
        return struct.unpack('<' + fmt, self.take(struct.calcsize('<' + fmt)))[0]

    def string(self) -> str:
        size = self.number('i')
        if not 1 <= abs(size) <= 4096:
            raise ValueError('Недопустимая длина строки PAK')
        if size < 0:
            raw = self.take(-size * 2)
            if raw[-2:] != b'\0\0':
                raise ValueError('Некорректная UTF-16 строка PAK')
            value = raw[:-2].decode('utf-16le')
        else:
            raw = self.take(size)
            if raw[-1:] != b'\0':
                raise ValueError('Некорректная строка PAK')
            value = raw[:-1].decode('utf-8')
        if '\0' in value:
            raise ValueError('NUL внутри строки PAK')
        return value

    def end(self):
        remaining = self.stream.read(16 if self.aligned else 1)
        if remaining and not (self.aligned and len(remaining) < 16 and not any(remaining)):
            raise ValueError('Лишние данные индекса PAK')


def _identity(stat: os.stat_result) -> tuple[int, int, int]:
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _unchanged(path: Path, stream: BinaryIO, identity: tuple[int, int, int]) -> None:
    reject_links(path)
    if _identity(path.stat()) != identity or _identity(os.fstat(stream.fileno())) != identity:
        raise CoreError(ErrorCode.RESOURCE_MISMATCH, 'PAK изменился во время чтения')


class V12ArchiveReader:
    """Streaming payloads, bounded metadata; only the documented V12 subset.

    probe/list_entries do not verify payloads. extract_entry accepts an offline
    root and publishes one verified file exclusively; existing files survive.
    """
    def __init__(self, *, max_entry_size: int = MAX_DATABASE, max_total: int = MAX_TOTAL,
                 cancelled: threading.Event | None = None,
                 key_provider: KeyProvider | None = None,
                 compression_provider: CompressionProvider | None = None,
                 max_files: int | None = None, max_index_bytes: int = MAX_INDEX,
                 max_metadata_bytes: int = DEFAULT_METADATA_BUDGET):
        max_files = MAX_FILES if max_files is None else max_files
        if (max_entry_size <= 0 or max_total <= 0 or not 0 < max_files <= 1_000_000
                or not 0 < max_index_bytes <= MAX_INDEX_LIMIT
                or not 0 < max_metadata_bytes <= MAX_METADATA_BUDGET):
            raise ValueError('Лимиты должны быть положительными')
        self.max_entry_size, self.max_total, self.cancelled = max_entry_size, max_total, cancelled
        self.max_files, self.max_index_bytes = max_files, max_index_bytes
        self.max_metadata_bytes = max_metadata_bytes
        self.key_provider = key_provider
        self.compression_provider = compression_provider or CompressionProvider()

    def probe(self, path: Path) -> ProbeResult:
        try:
            self.read_metadata(path)
            return ProbeResult(True, None, 'V12_SUBSET_METADATA_ONLY')
        except CoreError as exc:
            return ProbeResult(False, exc.code, str(exc))

    def read_metadata(self, path: Path, *, path_contains: str | None = None,
                      extensions: set[str] | None = None, offset: int = 0,
                      limit: int | None = None) -> ArchiveMetadata:
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError('Неверная страница индекса')
        suffixes = None if extensions is None else {ext.casefold() for ext in extensions}
        if suffixes is not None and any(not ext.startswith('.') for ext in suffixes):
            raise ValueError('Расширения должны начинаться с точки')
        try:
            return _read_metadata(path, max_entry_size=self.max_entry_size,
                                  max_total=self.max_total, cancelled=self.cancelled,
                                  key_provider=self.key_provider,
                                  compression_provider=self.compression_provider,
                                  max_files=self.max_files, max_index_bytes=self.max_index_bytes,
                                  max_metadata_bytes=self.max_metadata_bytes,
                                  path_contains=path_contains.casefold() if path_contains else None,
                                  extensions=suffixes, page_offset=offset, page_limit=limit)
        except CoreError:
            raise
        except (ValueError, struct.error) as exc:
            raise CoreError(ErrorCode.CORRUPTED_ARCHIVE, str(exc)) from exc

    def list_entries(self, path: Path, **filters) -> tuple[ArchiveEntry, ...]:
        return self.read_metadata(path, **filters).entries

    def _copy_entry(self, entry: ArchiveEntry, stream: BinaryIO,
                    destination: BinaryIO | None = None) -> EntryVerification:
        _unchanged(entry.archive, stream, entry.identity)
        stream.seek(entry.offset + entry.header_size)
        remaining = entry.size
        stored_sha1, sha1, sha256 = hashlib.sha1(), hashlib.sha1(), hashlib.sha256()

        def consume(block: bytes) -> None:
            nonlocal remaining
            remaining -= len(block)
            if remaining < 0:
                raise CoreError(ErrorCode.CORRUPTED_ARCHIVE, 'Превышен decoded size')
            sha1.update(block)
            sha256.update(block)
            if destination is not None:
                destination.write(block)

        if entry.compression != 'None':
            for start, end in entry.blocks:
                check_cancel(self.cancelled)
                stream.seek(start)
                packed = stream.read(end - start)
                if len(packed) != end - start:
                    raise CoreError(ErrorCode.CORRUPTED_ARCHIVE, 'Обрезанный compressed block')
                stored_sha1.update(packed)
                block = self.compression_provider.decode_block(
                    entry.compression, packed, min(entry.block_size, remaining))
                check_cancel(self.cancelled)
                consume(block)
        while remaining and entry.compression == 'None':
            check_cancel(self.cancelled)
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise CoreError(ErrorCode.CORRUPTED_ARCHIVE, 'Обрезанные данные PAK')
            stored_sha1.update(block)
            consume(block)
        check_cancel(self.cancelled)
        if remaining or stored_sha1.hexdigest() != entry.sha1:
            raise CoreError(ErrorCode.CORRUPTED_ARCHIVE, 'SHA-1 содержимого PAK не совпадает')
        _unchanged(entry.archive, stream, entry.identity)
        return EntryVerification(entry.path, entry.size, sha1.hexdigest(), sha256.hexdigest())

    def extract_entry(self, entry: ArchiveEntry, destination: Path) -> EntryVerification:
        from .translation_db import offline_path
        from .paths import inside

        check_cancel(self.cancelled)
        if entry not in self.list_entries(entry.archive):
            raise CoreError(ErrorCode.RESOURCE_MISMATCH, 'Запись устарела или подменена')
        root = offline_path(destination)
        target = inside(root, entry.path)
        if target == entry.archive:
            raise CoreError(ErrorCode.RESOURCE_MISMATCH, 'Output совпал с исходным архивом')
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode='w+b', suffix='.tmp', dir=target.parent,
                                             delete=False) as out:
                temporary = Path(out.name)
                with entry.archive.open('rb') as stream:
                    result = self._copy_entry(entry, stream, out)
                out.flush()
                os.fsync(out.fileno())
            check_cancel(self.cancelled)
            reject_links(target)
            if os.name == 'nt':
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
            return EntryVerification(result.path, result.size, result.sha1, result.sha256, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def verify_archive(self, path: Path) -> ArchiveVerification:
        metadata = self.read_metadata(path)
        with metadata.archive.open('rb') as stream:
            verified = tuple(self._copy_entry(entry, stream) for entry in metadata.entries)
            stream.seek(0)
            digest = hashlib.sha256()
            while True:
                check_cancel(self.cancelled)
                block = stream.read(min(1024 * 1024, metadata.size - stream.tell()))
                if not block:
                    break
                digest.update(block)
            _unchanged(metadata.archive, stream, metadata.entries[0].identity)
        return ArchiveVerification(digest.hexdigest(), verified)


def read_databases(path: Path, *, max_total: int = MAX_TOTAL,
                   cancelled: threading.Event | None = None,
                   key_provider: KeyProvider | None = None,
                   compression_provider: CompressionProvider | None = None) -> dict[str, bytes]:
    """Compatible in-memory SQLite adapter; the general reader streams to disk."""
    reader = V12ArchiveReader(max_total=max_total, cancelled=cancelled, key_provider=key_provider,
                              compression_provider=compression_provider)
    metadata = reader.read_metadata(path)
    result = {}
    with metadata.archive.open('rb') as stream:
        for entry in metadata.entries:
            name = entry.path.removeprefix(PREFIX)
            if (not entry.path.startswith(PREFIX) or not re.fullmatch(r'lang_[A-Za-z0-9_]+\.db', name)
                    or entry.size == 0):
                raise CoreError(ErrorCode.SCHEMA_MISMATCH, 'Нужны непустые ConfigDB/en/lang_*.db')
            buffer = io.BytesIO()
            reader._copy_entry(entry, stream, buffer)
            result[name] = buffer.getvalue()
    return result


def _read_metadata(path: Path, *, max_total: int, max_entry_size: int,
                   cancelled: threading.Event | None = None,
                   key_provider: KeyProvider | None = None,
                   compression_provider: CompressionProvider,
                   max_files: int = MAX_FILES, max_index_bytes: int = MAX_INDEX,
                   max_metadata_bytes: int = DEFAULT_METADATA_BUDGET,
                   path_contains: str | None = None, extensions: set[str] | None = None,
                   page_offset: int = 0, page_limit: int | None = None) -> ArchiveMetadata:
    """Read bounded indexes and headers, without loading any payload."""
    check_cancel(cancelled)
    path = Path(path).absolute()
    reject_links(path)
    identity = _identity(path.stat())
    length = identity[0]
    if length < FOOTER:
        raise UnsupportedPak('UNKNOWN: PAK footer отсутствует')
    with path.open('rb') as stream:
        stream.seek(-FOOTER, 2)
        footer = stream.read(FOOTER)
        if len(footer) != FOOTER:
            raise ValueError('PAK footer обрезан во время чтения')
        info = decode_footer(footer, length)
        status = info['status']
        encrypted = status == 'ENCRYPTED_INDEX'
        if status in ('UNKNOWN_FORMAT', 'KEY_GUID_PRESENT'):
            raise UnsupportedPak(f'Неизвестный layout: {status}')
        if status not in ('PLAIN_FOOTER_CANDIDATE', 'ENCRYPTED_INDEX', 'DECLARED_COMPRESSION'):
            raise ValueError(f'Повреждённый footer: {status}')
        try:
            key = authorized_key(key_provider, path, footer[:16]) if encrypted else None
        except CoreError as exc:
            raise UnsupportedPak('ENCRYPTED_INDEX: разрешённый ключ недоступен', exc.code) from None
        index_hash = hashlib.sha256(footer)
        index_offset, index_size = info['index_offset'], info['index_size']

        def region(offset: int, size: int, expected: bytes) -> bytes:
            check_cancel(cancelled)
            if not 0 < size or offset < index_offset or offset + size > length - FOOTER:
                raise ValueError('Индекс PAK выходит за границы')
            if size > max_index_bytes:
                raise UnsupportedPak(f'Размер индекса PAK {size} превышает лимит {max_index_bytes}')
            stream.seek(offset)
            data = stream.read(size)
            if len(data) != size:
                raise ValueError('Индекс PAK обрезан')
            index_hash.update(data)
            if encrypted:
                data = decrypt_index(data, key)
                check_cancel(cancelled)
            if hashlib.sha1(data).digest() != expected:
                raise ValueError('SHA-1 индекса PAK не совпадает')
            return data

        primary = Cursor(region(index_offset, index_size, footer[41:61]), aligned=encrypted)
        mount_point = primary.string()
        # Preserve the mount for metadata while keeping it separate from staging
        # paths. Current WuWa resources use this virtual root plus a subdirectory.
        if not re.fullmatch(r'\.\./\.\./\.\./(?:[A-Za-z0-9_.-]+/)*', mount_point):
            raise UnsupportedPak('Неподдерживаемый mount point PAK')
        count = primary.number()
        if not 0 < count <= max_files:
            raise UnsupportedPak(
                f'Число файлов PAK {count} превышает лимит Reader {max_files}')
        seed = primary.number('Q')
        descriptors = []
        for _ in range(2):
            if primary.number() != 1:
                raise UnsupportedPak('Нужны hash и directory indexes')
            descriptors.append((primary.number('Q'), primary.number('Q'), primary.take(20)))
        encoded_size = primary.number()
        if not 0 < encoded_size:
            raise ValueError('Недопустимый размер encoded entries')
        if encoded_size > max_index_bytes:
            raise UnsupportedPak(f'Encoded index PAK превышает лимит {max_index_bytes} bytes')
        encoded = Cursor(primary.take(encoded_size))
        if primary.number() != 0:
            raise UnsupportedPak('Non-encoded entries не поддержаны')
        primary.end()
        (hash_offset, hash_size, hash_digest), (dir_offset, dir_size, dir_digest) = descriptors
        if (hash_offset != index_offset + index_size or dir_offset != hash_offset + hash_size
                or dir_offset + dir_size != length - FOOTER):
            raise ValueError('Перекрывающиеся/неполные индексы PAK')
        estimate = count * 1024 + index_size + hash_size + dir_size
        if estimate > max_metadata_bytes:
            raise UnsupportedPak(
                f'Оценка метаданных PAK {estimate} bytes превышает budget {max_metadata_bytes}')
        if hash_size > max_index_bytes or dir_size > max_index_bytes:
            raise UnsupportedPak(f'Индекс PAK превышает лимит {max_index_bytes} bytes')
        hashes = Cursor(region(hash_offset, hash_size, hash_digest), aligned=encrypted)
        directory = Cursor(region(dir_offset, dir_size, dir_digest), aligned=encrypted)
        if hashes.number() != count:
            raise ValueError('Количество path hashes не совпадает')
        dirs = directory.number()
        if not 1 <= dirs <= max_files * 16:
            raise ValueError('Превышен лимит каталогов PAK')
        estimate += dirs * 256
        if estimate > max_metadata_bytes:
            raise UnsupportedPak(
                f'Оценка метаданных PAK {estimate} bytes превышает budget {max_metadata_bytes}')
        names = {}
        pruned_names = []
        folded = set()
        seen_dirs = set()
        for _ in range(dirs):
            parent = directory.string()
            if parent.casefold() in seen_dirs:
                raise ValueError('Повторяющийся каталог PAK')
            seen_dirs.add(parent.casefold())
            if parent != '/':
                if not parent.endswith('/') or '\\' in parent:
                    raise ValueError('Некорректный каталог PAK')
                relative_parts(parent[:-1])
            files = directory.number()
            if files > count:
                raise ValueError('Превышен лимит файлов каталога')
            for _ in range(files):
                check_cancel(cancelled)
                name, entry = directory.string(), directory.number()
                if len(relative_parts(name)) != 1 or '/' in name or '\\' in name:
                    raise ValueError('Имя файла должно быть одним компонентом')
                full = ('' if parent == '/' else parent) + name
                relative_parts(full)
                if full.casefold() in folded:
                    raise ValueError('Недопустимая/дублирующаяся запись PAK')
                folded.add(full.casefold())
                if entry == 0x80000000:
                    pruned_names.append(full)
                elif entry >= encoded_size or entry in names:
                    raise ValueError('Недопустимая/дублирующаяся запись PAK')
                else:
                    names[entry] = full
        directory.end()
        if len(names) + len(pruned_names) != count:
            raise ValueError('Неполный directory index')
        if not len(names) * 13 <= encoded_size <= max_index_bytes:
            raise ValueError('Размер encoded entries не соответствует активным файлам')
        expected_hashes = set()

        def add_expected_hash(entry: int, name: str) -> None:
            check_cancel(cancelled)
            value = (0xCBF29CE484222325 + seed) & 0xffffffffffffffff
            for byte in name.lower().encode('utf-16le'):
                value = ((value ^ byte) * 0x100000001B3) & 0xffffffffffffffff
            expected_hashes.add((value, entry))

        for entry, name in names.items():
            add_expected_hash(entry, name)
        for name in pruned_names:
            add_expected_hash(0x80000000, name)
        if len(expected_hashes) != count:
            raise ValueError('Path hash не соответствует directory index')
        for _ in range(count):
            check_cancel(cancelled)
            item = (hashes.number('Q'), hashes.number())
            if item not in expected_hashes:
                raise ValueError('Path hash не соответствует directory index')
            expected_hashes.remove(item)
        if expected_hashes or hashes.number() != 0:
            raise ValueError('Path hash не соответствует directory index')
        hashes.end()
        entries = []
        spans = []
        total = 0
        total_blocks = 0
        active_count = 0
        selected_count = 0
        for _ in range(len(names)):
            check_cancel(cancelled)
            entry = encoded.stream.tell()
            if entry not in names:
                raise ValueError('Directory offset не указывает на начало encoded entry')
            raw = encoded.number()
            flags = ((raw >> 16) & 0x3f | (raw & 0xffff) << 6 | (raw & (1 << 28)) >> 6
                     | (raw & 0x0fc00000) << 1 | (raw & 0xc0000000) >> 1
                     | (raw & 0x20000000) << 2)
            if encoded.take(1) != b'\0' or flags & (1 << 22):
                raise UnsupportedPak('Custom/encrypted payload entries не поддержаны')
            block_size = encoded.number() if (flags & 63) == 63 else (flags & 63) << 11
            method = (flags >> 23) & 63
            if method:
                slots = info['compression_slots']
                codec = slots[method - 1] if method <= len(slots) else ''
                compression_provider.require(codec)
            else:
                codec = 'None'
            # WuWa V12 stores size before offset after its bit permutation.
            size = encoded.number('I' if flags & (1 << 31) else 'Q')
            offset = encoded.number('I' if flags & (1 << 30) else 'Q')
            stored = encoded.number('I' if flags & (1 << 29) else 'Q') if method else size
            block_count = (flags >> 6) & 0xffff
            total_blocks += block_count
            if total_blocks > MAX_BLOCKS:
                raise ValueError('Превышен лимит метаданных compressed blocks')
            if not method and (block_count or block_size):
                raise UnsupportedPak('Plain entry с compression block metadata')
            if method and (not size or not 0 < block_size <= MAX_BLOCK_OUTPUT
                           or block_count != (size + block_size - 1) // block_size):
                raise ValueError('Некорректное число/размер compressed blocks')
            lengths = ([stored] if block_count == 1 else
                       [encoded.number() for _ in range(block_count)]) if method else []
            if method and (any(not 0 < n <= MAX_BLOCK_INPUT for n in lengths) or sum(lengths) != stored):
                raise ValueError('Некорректный размер compressed block')
            header_size = 53 + (4 + block_count * 16 if method else 0)
            total += size
            if not 0 <= size <= max_entry_size or total > max_total or offset + header_size + stored > index_offset:
                raise ValueError('Размер/границы данных PAK недопустимы')
            spans.append((offset, offset + header_size + stored))
            stream.seek(offset)
            header = stream.read(header_size)
            if (len(header) != header_size or header[:28] != struct.pack('<QQQI', 0, stored, size, method)
                    or header[-5:] != b'\0' + struct.pack('<I', block_size)):
                raise ValueError('Serialized FPakEntry не совпадает с encoded entry')
            blocks = []
            start = offset + header_size
            if method:
                if struct.unpack_from('<I', header, 48)[0] != block_count:
                    raise ValueError('Serialized block count не совпадает')
                for i, packed_size in enumerate(lengths):
                    left, right = struct.unpack_from('<QQ', header, 52 + i * 16)
                    if (left, right) != (start - offset, start + packed_size - offset):
                        raise ValueError('Serialized block bounds не совпадают')
                    blocks.append((start, start + packed_size))
                    start += packed_size
            entry_path = names[entry]
            active_count += 1
            matches = ((path_contains is None or path_contains in entry_path.casefold())
                       and (extensions is None or Path(entry_path).suffix.casefold() in extensions))
            if matches:
                if selected_count >= page_offset and (page_limit is None
                                                       or len(entries) < page_limit):
                    entries.append(ArchiveEntry(path, entry_path, offset, size,
                                                header[28:48].hex(), index_hash.hexdigest(),
                                                identity, stored, header_size, codec,
                                                tuple(blocks), block_size))
                selected_count += 1
        encoded.end()
        spans.sort()
        if spans[0][0] != 0 or spans[-1][1] != index_offset:
            raise ValueError('PAK payload имеет незанятые начальные/конечные байты')
        for left, right in zip(spans, spans[1:]):
            check_cancel(cancelled)
            if left[1] > right[0]:
                raise ValueError('PAK payload-записи перекрываются')
            if left[1] < right[0]:
                padding_size = right[0] - left[1]
                if padding_size > 2047 or right[0] % 2048:
                    raise ValueError('Неподдерживаемое выравнивание PAK payload')
                stream.seek(left[1])
                if stream.read(padding_size) != bytes(padding_size):
                    raise ValueError('Ненулевой padding между PAK payload-записями')
        _unchanged(path, stream, identity)
        return ArchiveMetadata(path, length, 12, mount_point, tuple(entries), total,
                               index_hash.hexdigest(), len(pruned_names), active_count,
                               tuple(pruned_names))
