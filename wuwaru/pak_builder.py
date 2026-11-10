"""Offline-only, plain V12 writer and independent verification.

SPDX-License-Identifier: GPL-3.0-or-later
Layout adapted from GishReloaded/WuWaRu@ee6c16ed53e274cff1aa1127ee1c1d85c5a12158,
wuwa_pak.py; earlier references: CUE4Parse and WuwaIDLauncher (see NOTICES).
No upstream module is imported. No encryption, signing or installation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
import time
from pathlib import Path
from threading import Event

from .manifest import check_cancel
from .pak_reader import MAX_FILES, MAX_TOTAL, PREFIX, read_databases
from .paths import inside, reject_links
from .payload_verify import verify_payloads
from .sqlite_source import MAX_DATABASE, inspect_database
from .translation_db import offline_path


def _publish_directory(source: Path, target: Path, cancelled: Event | None) -> None:
    """Exclusive Windows rename; bounded retry of transient filesystem denials."""
    for attempt in range(4):
        check_cancel(cancelled)
        reject_links(target)
        if target.exists():
            raise FileExistsError(target)
        try:
            os.rename(source, target)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32) or attempt == 3:
                raise
            time.sleep(0.05 * 2 ** attempt)


def _plain_v12(files: dict[str, bytes], destination: Path, cancelled: Event | None, *, prefix: str = PREFIX) -> None:
    """The existing WuWaRu writer layout, with bounded, prevalidated inputs."""
    def u32(value):
        return struct.pack('<I', value)

    def u64(value):
        return struct.pack('<Q', value)

    def sha1(data):
        return hashlib.sha1(data).digest()

    def string(value):
        data = value.encode('utf-8') + b'\0'
        return u32(len(data)) + data

    encoded = bytearray()
    hashes = bytearray(u32(len(files)))
    directories = {'/': {}}
    with destination.open('xb') as out:
        for name, data in sorted(files.items()):
            check_cancel(cancelled)
            relative = prefix + name
            offset, size = out.tell(), len(data)
            out.write(struct.pack('<QQQI', 0, size, size, 0) + sha1(data) + bytes(5) + data)
            entry = len(encoded)
            encoded += u32(0xe0000000) + b'\0' + u32(size) + u32(offset)
            value = 0xCBF29CE484222325
            for byte in relative.lower().encode('utf-16le'):
                value = ((value ^ byte) * 0x100000001B3) & 0xffffffffffffffff
            hashes += u64(value) + u32(entry)
            parent, filename = relative.rsplit('/', 1)
            directories.setdefault(parent + '/', {})[filename] = entry
            while '/' in parent:
                parent = parent.rsplit('/', 1)[0]
                directories.setdefault(parent + '/', {})
        hashes += u32(0)
        directory = bytearray(u32(len(directories)))
        for parent, children in sorted(directories.items()):
            directory += string(parent) + u32(len(children))
            for name, entry in sorted(children.items()):
                directory += string(name) + u32(entry)
        index = out.tell()
        mount = string('../../../')
        size = len(mount) + 4 + 8 + 80 + 4 + len(encoded) + 4
        primary = (mount + u32(len(files)) + u64(0)
                   + u32(1) + u64(index + size) + u64(len(hashes)) + sha1(hashes)
                   + u32(1) + u64(index + size + len(hashes)) + u64(len(directory)) + sha1(directory)
                   + u32(len(encoded)) + encoded + u32(0))
        out.write(primary + hashes + directory)
        out.write(bytes(17) + u32(0x5a6f12e1) + u32(12) + u64(index) + u64(size) + sha1(primary) + bytes(160))
        out.flush()
        os.fsync(out.fileno())


def write_archive(files: dict[str, bytes], destination: Path, *, cancelled: Event | None = None) -> None:
    """Explicit full entry paths; the caller verifies its payload. SQLite rules stay in build_package."""
    for name in files:
        if '\\' in name or ':' in name or any(part in ('', '.', '..') for part in name.split('/')):
            raise ValueError('Unsafe entry path: ' + name)
    _plain_v12(files, destination, cancelled, prefix='')


def verify_package(archive: Path, expected: dict[str, bytes], destination: Path, *,
                   cancelled: Event | None = None) -> list[dict]:
    """Separate indexed parser + sequential payload scan, then SQLite reread.

    Both readers are independent of the writer, not independent specifications.
    Extraction uses a fresh folder; no untrusted path is ever written directly.
    """
    archive, destination = offline_path(archive), offline_path(destination)
    check_cancel(cancelled)
    actual = read_databases(archive, cancelled=cancelled)
    if not expected or actual != expected:
        raise ValueError('Имена или байты PAK не совпали с исходными SQLite')
    verify_payloads(archive, expected, cancelled=cancelled)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = []
    with tempfile.TemporaryDirectory(prefix='.pak-', dir=destination.parent) as temporary:
        folder = Path(temporary) / 'verified'
        folder.mkdir()
        for name, data in sorted(actual.items()):
            check_cancel(cancelled)
            target = inside(folder, PREFIX + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
            inspection = inspect_database(target, independent=True, cancelled=cancelled)
            result.append(dict(name=PREFIX + name, sha256=inspection['sha256'], rows=inspection['rows'],
                               integrity=inspection['integrity']))
        check_cancel(cancelled)
        reject_links(destination)
        if os.name != 'nt':
            raise OSError('Exclusive publication поддержана только на Windows')
        _publish_directory(folder, destination, cancelled)
    return result


def build_package(source: Path, destination: Path, *, cancelled: Event | None = None) -> dict:
    """Build only a new offline output directory; publish after verification.

    Source must contain exactly Client/Content/Aki/ConfigDB/en/lang_*.db.
    Bounded in-memory prototype: at most 512 MiB total, 4096 SQLite files.
    """
    source, destination = offline_path(source), offline_path(destination)
    check_cancel(cancelled)
    if destination.exists():
        raise FileExistsError(destination)
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError('Output должен быть вне исходного дерева')
    files = {}
    total = 0
    for path in source.rglob('*'):
        check_cancel(cancelled)
        reject_links(path)
        if path.is_dir():
            continue
        relative = path.relative_to(source).as_posix()
        if not re.fullmatch(re.escape(PREFIX) + r'lang_[A-Za-z0-9_]+\.db', relative):
            raise ValueError('Допускаются только ConfigDB/en/lang_*.db с исходными именами')
        if len(files) >= MAX_FILES or path.name.casefold() in {n.casefold() for n in files}:
            raise ValueError('Лимит или коллизия имён SQLite')
        total += path.stat().st_size
        if total > MAX_TOTAL:
            raise ValueError('Лимит размера пакета')
        inspection = inspect_database(path, cancelled=cancelled)
        with path.open('rb') as stream:
            data = stream.read(MAX_DATABASE + 1)
        if len(data) != inspection['size'] or hashlib.sha256(data).hexdigest() != inspection['sha256']:
            raise ValueError('Исходная SQLite изменилась')
        files[path.name] = data
    if not files:
        raise ValueError('Нет исходных SQLite')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.pak-', dir=destination.parent) as temporary:
        folder = Path(temporary) / 'package'
        folder.mkdir()
        archive = folder / 'synthetic.pak'
        _plain_v12(files, archive, cancelled)
        databases = verify_package(archive, files, folder / 'verified', cancelled=cancelled)
        report = dict(schema_version=1, mode='BUILD_OFFLINE', evidence='SYNTHETIC_ONLY',
                      compatibility='UNKNOWN', installation_allowed=False, databases=databases,
                      pak_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        (folder / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        check_cancel(cancelled)
        reject_links(destination)
        if os.name != 'nt':
            raise OSError('Exclusive publication поддержана только на Windows')
        _publish_directory(folder, destination, cancelled)
    return report
