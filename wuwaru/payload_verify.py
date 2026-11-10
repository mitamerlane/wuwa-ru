"""Independent sequential payload walk; does not use directory/encoded indexes."""
import hashlib
import struct
from collections import Counter
from pathlib import Path
from threading import Event

from .manifest import check_cancel
from .paths import reject_links
from .sqlite_source import MAX_DATABASE


def verify_payloads(path: Path, expected: dict[str, bytes], *,
                    cancelled: Event | None = None) -> None:
    reject_links(path)
    wanted = Counter((len(data), hashlib.sha256(data).hexdigest()) for data in expected.values())
    found = Counter()
    with path.open('rb') as stream:
        stream.seek(-221, 2)
        footer = stream.read(221)
        if len(footer) != 221:
            raise ValueError('PAK footer обрезан при независимом чтении')
        if footer[:17] != bytes(17) or struct.unpack_from('<II', footer, 17) != (0x5a6f12e1, 12):
            raise ValueError('Неподдерживаемый footer при независимой проверке')
        end = struct.unpack_from('<Q', footer, 25)[0]
        if not 0 < end <= path.stat().st_size - 221:
            raise ValueError('Неверная граница payload')
        stream.seek(0)
        while stream.tell() < end:
            check_cancel(cancelled)
            header = stream.read(53)
            if len(header) != 53:
                raise ValueError('Обрезанный независимый FPakEntry')
            offset, size, original, method = struct.unpack_from('<QQQI', header)
            if (offset or size != original or method or header[48:] != bytes(5)
                    or not 0 < size <= MAX_DATABASE or stream.tell() + size > end):
                raise ValueError('Неверный независимый FPakEntry')
            sha1, sha256 = hashlib.sha1(), hashlib.sha256()
            remaining = size
            while remaining:
                check_cancel(cancelled)
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError('Обрезанный независимый payload')
                sha1.update(block)
                sha256.update(block)
                remaining -= len(block)
            if sha1.digest() != header[28:48]:
                raise ValueError('SHA-1 независимого payload не совпадает')
            found[(size, sha256.hexdigest())] += 1
            if sum(found.values()) > len(expected):
                raise ValueError('Лишние независимые payload')
        if stream.tell() != end or found != wanted:
            raise ValueError('Независимое чтение payload не совпало с extractor')
