"""Bounded resource diagnostics. No index decryption, keys or native codecs.

Footer layout: hash-pinned CUE4Parse FPakInfo / WuWaRu writer; see source-index.
Footer observations describe a candidate layout, never a validated archive.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import threading
from pathlib import Path

from .discovery import Installation, MAX_METADATA, read_metadata
from .manifest import check_cancel, create_manifest, fingerprint, mount_entries
from .paths import inside, reject_links

FOOTER_SIZE = 221
LANG_MOUNT = re.compile(r'MountLang_([a-z]{2,3}(?:_[A-Za-z]{2,4})?)\.txt')
RES_MANIFEST = re.compile(
    r'Manifest(?:Aggregated|Lang_[a-z]{2,3}|Launcher|Resource(?:_HD)?)_'
    r'(\d+(?:\.\d+){1,3})\.txt')


def _manifest_names(folder: Path) -> list[str]:
    reject_links(folder)
    names = []
    if folder.is_dir():
        for count, path in enumerate(folder.iterdir()):
            if count >= 256:
                raise ValueError('Слишком много ResManifest файлов')
            if RES_MANIFEST.fullmatch(path.name):
                names.append(path.name)
    return sorted(names)


def decode_footer(raw: bytes, length: int) -> dict:
    """Interpret only the known 221-byte layout; no scanning or guessing offsets."""
    info = {'status': 'UNKNOWN_FORMAT', 'format': 'UNKNOWN',
            'index_content_status': 'NOT_READ'}
    if len(raw) != FOOTER_SIZE:
        return info
    magic, version, offset, size = struct.unpack_from('<IIQQ', raw, 17)
    if magic != 0x5a6f12e1 or version != 12:
        return info
    info.update(format='PAK_V12_FOOTER_CANDIDATE', magic='0x5a6f12e1',
                version_field=version, encrypted_index=bool(raw[16]),
                key_guid_present=any(raw[:16]), index_offset=offset, index_size=size,
                footer_sha256=hashlib.sha256(raw).hexdigest())
    methods = []
    for pos in range(61, FOOTER_SIZE, 32):
        name, separator, padding = raw[pos:pos + 32].partition(b'\0')
        if not separator or any(padding) or any(c < 33 or c > 126 for c in name):
            info['status'] = 'INVALID_COMPRESSION_TABLE'
            return info
        methods.append(name.decode('ascii'))
    info['compression_methods'] = [m for m in methods if m]
    info['compression_slots'] = methods
    if raw[16] not in (0, 1):
        status = 'INVALID_ENCRYPTION_FLAG'
    elif not (0 < size and offset + size <= length - FOOTER_SIZE):
        status = 'INVALID_INDEX_BOUNDS'
    elif raw[16]:
        status = 'ENCRYPTED_INDEX'
    elif any(raw[:16]):
        status = 'KEY_GUID_PRESENT'
    elif any(methods):
        status = 'DECLARED_COMPRESSION'
    else:
        status = 'PLAIN_FOOTER_CANDIDATE'
    info['status'] = status
    return info


def inspect_footer(path: Path) -> dict:
    """Read at most 221 bytes, with path and concurrent-change checks."""
    path = Path(path)
    reject_links(path)
    before = path.stat()
    raw = b''
    if before.st_size >= FOOTER_SIZE:
        with path.open('rb') as stream:
            stream.seek(-FOOTER_SIZE, 2)
            raw = stream.read(FOOTER_SIZE)
        if len(raw) != FOOTER_SIZE:
            raise ValueError('PAK footer обрезан во время чтения')
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError('PAK изменился во время диагностики')
    return decode_footer(raw, before.st_size)


def probe_resources(installation: Installation, *, resource_version: str | None = None,
                    hash_language_packages: bool = False,
                    cancelled: threading.Event | None = None) -> dict:
    """Report local evidence; semantic client/active version remain UNKNOWN.

    Full byte identity is optional and covers only MountLang-referenced packages,
    not an inferred complete corpus. Opaque ResManifest payloads are never interpreted.
    """
    snapshot = create_manifest(installation, resource_version=resource_version, cancelled=cancelled)
    if len(snapshot['packages']) > 4096:
        raise ValueError('Слишком много PAK для диагностики')
    root = installation.root
    packages = []
    language_hashes = []
    for item in snapshot['packages']:
        check_cancel(cancelled)
        path = inside(root, item['path'])
        info = inspect_footer(path)
        digest = None
        if hash_language_packages and any(LANG_MOUNT.fullmatch(o) for o in item['origins']):
            digest = fingerprint(path, True, cancelled)['sha256']
            language_hashes.append((item['path'], digest))
        packages.append({**item, 'sha256': digest, 'footer': info})
    languages = []
    evidence = []
    selected = snapshot['resource_version']
    if selected is not None:
        resources = inside(root, f'Client/Saved/Resources/{selected}')
        for item in snapshot['mounts']:
            match = LANG_MOUNT.fullmatch(Path(item['path']).name)
            if match:
                entries = mount_entries(read_metadata(inside(root, item['path'])))
                languages.append({'language': match[1], 'mount': item['path'],
                                  'packages': [p.relative_to(root).as_posix()
                                               for p in (inside(resources, e) for e in entries)],
                                  'role': 'UNKNOWN'})
        folder = inside(resources, 'ResManifest')
        manifest_names = _manifest_names(folder)
        if manifest_names:
            for name in manifest_names:
                check_cancel(cancelled)
                match = RES_MANIFEST.fullmatch(name)
                path = inside(folder, name)
                if path.stat().st_size > MAX_METADATA:
                    raise ValueError('Слишком большой ResManifest')
                before = fingerprint(path, True, cancelled)
                text = read_metadata(path)
                content_format = 'OPAQUE_TEXT'
                try:
                    base64.b64decode(text, validate=True)
                    content_format = 'OPAQUE_BASE64'
                except ValueError:
                    pass
                if before != fingerprint(path, True, cancelled):
                    raise RuntimeError('ResManifest изменился во время чтения')
                evidence.append({'path': path.relative_to(root).as_posix(), **before,
                                 'version_claim': match[1], 'claim_source': 'FILENAME_ONLY',
                                 'content_format': content_format,
                                 'client_version_verified': False})
    # Recheck content hashes as well as inventory; never publish stale evidence.
    for relative, digest in language_hashes:
        if fingerprint(inside(root, relative), True, cancelled)['sha256'] != digest:
            raise RuntimeError('Языковой PAK изменился во время диагностики')
    for item in evidence:
        if fingerprint(inside(root, item['path']), True, cancelled) != {
                k: item[k] for k in ('size', 'mtime_ns', 'sha256')}:
            raise RuntimeError('ResManifest изменился во время диагностики')
    if selected is not None and manifest_names != _manifest_names(folder):
        raise RuntimeError('Состав ResManifest изменился во время диагностики')
    if snapshot != create_manifest(installation, resource_version=resource_version, cancelled=cancelled):
        raise RuntimeError('Набор ресурсов изменился во время диагностики')
    return {
        'schema_version': 1, 'mode': 'READ_ONLY', 'game_root': str(root),
        'client_version': None, 'client_version_status': 'UNKNOWN',
        'client_version_reason': 'Нет подтверждённого источника версии клиента; '
                                 'Steam build и имена ResManifest/Mount не являются версией клиента.',
        'steam_build_id': installation.steam_build_id,
        'resource_version': selected, 'active_resource_version_status': 'UNKNOWN',
        'inventory': snapshot, 'packages': packages, 'language_mounts': languages,
        'version_evidence': evidence,
        'language_packages_sha256': hashlib.sha256(json.dumps(
            language_hashes, separators=(',', ':')).encode()).hexdigest() if language_hashes else None,
        'language_identity_scope': 'EXACT_MOUNTLANG_REFERENCED_BYTES' if language_hashes else 'NOT_HASHED',
        'complete_game_language_inventory': False,
        'coverage': {category: 'UNKNOWN' for category in
                     ('interface', 'quests', 'dialogues', 'abilities', 'items')},
        'extraction': {'databases': 0, 'rows': 0, 'status': 'NOT_ATTEMPTED_BY_DIAGNOSTIC'},
        'compatibility': 'UNKNOWN', 'installation_allowed': False,
    }
