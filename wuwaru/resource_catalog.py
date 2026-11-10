"""Read-only inventory of locally installed PAKs using the SELF ArchiveReader."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections import Counter
from pathlib import Path

from .core_contracts import CoreError, VersionIdentity
from .discovery import inspect_installation
from .manifest import check_cancel
from .pak_reader import V12ArchiveReader
from .paths import inside, reject_links
from .resource_probe import inspect_footer

MAX_PACKAGES = 4096
MAX_DIRECTORIES = 20_000
MAX_DIRECTORY_DEPTH = 16
RESOURCE_VERSION = re.compile(r'\d+(?:\.\d+){1,3}')


class LocalResourceCatalog:
    """Inventory PAK paths and verified indexes; never claims client load order."""

    def __init__(self, root: Path, *, reader: V12ArchiveReader | None = None,
                 cancelled: threading.Event | None = None):
        self.installation = inspect_installation(root)
        self.root = self.installation.root
        self.reader = reader or V12ArchiveReader(max_files=400_000,
                                                 max_index_bytes=128 * 1024 * 1024,
                                                 max_metadata_bytes=512 * 1024 * 1024)
        self.cancelled = cancelled
        self.priority_basis = (
            'HEURISTIC: resource patch paths precede base paths, then casefolded filename; '
            'actual client load order UNKNOWN')

    def discover_resources(self) -> tuple[Path, ...]:
        check_cancel(self.cancelled)
        found: set[Path] = set()
        base = inside(self.root, 'Client/Content/Paks')
        if base.is_dir():
            reject_links(base)
            for package in base.glob('*.pak'):
                reject_links(package)
                if package.is_file():
                    found.add(package)

        resources = inside(self.root, 'Client/Saved/Resources')
        if resources.is_dir():
            reject_links(resources)
            pending = [(resources, 0)]
            visited = 0
            while pending:
                folder, depth = pending.pop()
                check_cancel(self.cancelled)
                visited += 1
                if visited > MAX_DIRECTORIES or depth > MAX_DIRECTORY_DEPTH:
                    raise ValueError('Превышен лимит обхода каталога ресурсов')
                reject_links(folder)
                for child in folder.iterdir():
                    reject_links(child)
                    if child.is_dir():
                        pending.append((child, depth + 1))
                    elif child.is_file() and child.suffix.casefold() == '.pak':
                        found.add(child)
        if len(found) > MAX_PACKAGES:
            raise ValueError('Слишком много PAK в каталоге установки')
        return tuple(sorted(found, key=lambda p: p.relative_to(self.root).as_posix().casefold()))

    def _priority_key(self, path: Path) -> tuple[int, str, str]:
        rel = path.relative_to(self.root).as_posix()
        patch_rank = 0 if '/Saved/Resources/' in rel else 1
        return patch_rank, path.stem.casefold(), rel.casefold()

    def resolve_resource_priority(self, resources: tuple[Path, ...]) -> tuple[Path, ...]:
        return tuple(sorted(resources, key=self._priority_key))

    def search_entries(self, *, path_contains: str | None = None,
                       extensions: set[str] | None = None,
                       limit: int = 100) -> tuple[dict, ...]:
        """Search available indexes; encrypted and unsupported packages stay opaque."""
        if not 1 <= limit <= 10_000:
            raise ValueError('Лимит поиска должен быть от 1 до 10000')
        found = []
        ordered = self.resolve_resource_priority(self.discover_resources())
        for priority, path in enumerate(ordered, start=1):
            check_cancel(self.cancelled)
            footer = inspect_footer(path)
            if footer['status'] not in ('PLAIN_FOOTER_CANDIDATE', 'DECLARED_COMPRESSION'):
                continue
            metadata = self.reader.read_metadata(
                path, path_contains=path_contains, extensions=extensions,
                limit=limit - len(found))
            relative = path.relative_to(self.root).as_posix()
            kind = 'PATCH_RESOURCE' if '/Saved/Resources/' in relative else 'BASE_RESOURCE'
            found.extend({'archive': relative, 'kind': kind, 'priority_hint': priority,
                          'path': entry.path, 'size': entry.size,
                          'sha1_stored': entry.sha1,
                          'index_sha256': entry.index_sha256,
                          'compression': entry.compression}
                         for entry in metadata.entries)
            if len(found) == limit:
                break
        return tuple(found)

    def identify_version(self) -> VersionIdentity:
        resources = inside(self.root, 'Client/Saved/Resources')
        candidates = []
        if resources.is_dir():
            for path in resources.iterdir():
                reject_links(path)
                if path.is_dir() and RESOURCE_VERSION.fullmatch(path.name):
                    candidates.append(path.name)
        candidates.sort(key=lambda v: tuple(map(int, v.split('.'))))
        resource_version = candidates[0] if len(candidates) == 1 else None
        return VersionIdentity(None, resource_version, 'UNKNOWN')

    def _inspect_package(self, path: Path) -> tuple[dict, object | None]:
        check_cancel(self.cancelled)
        relative = path.relative_to(self.root).as_posix()
        kind = 'PATCH_RESOURCE' if '/Saved/Resources/' in relative else 'BASE_RESOURCE'
        footer = inspect_footer(path)
        record = {'archive': relative, 'kind': kind, 'archive_size': path.stat().st_size,
                  'mount_point': None, 'footer_status': footer['status'],
                  'footer_sha256': footer.get('footer_sha256'),
                  'declared_compression_methods': footer.get('compression_methods', []),
                  'index_status': 'NOT_READ', 'active_entries': None,
                  'pruned_entries': None, 'pruned_paths': [], 'entry_compression_counts': {},
                  'index_sha256': None, 'reason': None}
        if footer['status'] not in ('PLAIN_FOOTER_CANDIDATE', 'DECLARED_COMPRESSION'):
            record['reason'] = footer['status']
            return record, None
        try:
            metadata = self.reader.read_metadata(path)
        except CoreError as exc:
            record['index_status'] = exc.code.value
            record['reason'] = str(exc)
            return record, None
        methods = Counter(entry.compression for entry in metadata.entries)
        record.update(index_status='VERIFIED_SUPPORTED_SUBSET',
                      mount_point=metadata.mount_point,
                      active_entries=metadata.active_entries,
                      pruned_entries=metadata.pruned_entries,
                      pruned_paths=list(metadata.pruned_paths),
                      entry_compression_counts=dict(sorted(methods.items())),
                      index_sha256=metadata.index_sha256)
        return record, metadata

    def build_manifest(self) -> dict:
        packages = [self._inspect_package(path)[0] for path in self.discover_resources()]
        fingerprint = hashlib.sha256(json.dumps(packages, sort_keys=True,
                                                separators=(',', ':')).encode()).hexdigest()
        return {'schema_version': 1, 'mode': 'READ_ONLY',
                'inventory_fingerprint_sha256': fingerprint,
                'resource_version': self.identify_version().resource_version,
                'inventory_complete': all(p['index_status'] == 'VERIFIED_SUPPORTED_SUBSET'
                                          for p in packages),
                'client_version': None, 'client_version_status': 'UNKNOWN',
                'priority_basis': self.priority_basis, 'packages': packages}

    def write_entry_manifest(self, destination: Path) -> dict:
        """Write a deterministic JSONL manifest without keeping all packages in RAM."""
        target = Path(destination).absolute()
        if target.is_relative_to(self.root):
            raise ValueError('Manifest нельзя записывать внутрь каталога игры')
        reject_links(target)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        package_count = entry_count = 0
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                             dir=target.parent, suffix='.tmp', delete=False) as out:
                temporary = Path(out.name)
                out.write(json.dumps({'type': 'catalog', 'schema_version': 1,
                                      'mode': 'READ_ONLY', 'priority_basis': self.priority_basis},
                                     ensure_ascii=False, sort_keys=True) + '\n')
                resources = self.discover_resources()
                priority_order = {path: index for index, path in
                                  enumerate(self.resolve_resource_priority(resources), start=1)}
                seen_entries: dict[str, str] = {}
                for path in self.resolve_resource_priority(resources):
                    check_cancel(self.cancelled)
                    package, metadata = self._inspect_package(path)
                    package_count += 1
                    out.write(json.dumps({'type': 'package', **package}, ensure_ascii=False,
                                         sort_keys=True) + '\n')
                    if metadata is None:
                        continue
                    for entry in metadata.entries:
                        check_cancel(self.cancelled)
                        entry_count += 1
                        key = entry.path.casefold()
                        previous = seen_entries.get(key)
                        out.write(json.dumps({'type': 'entry',
                                              'archive': package['archive'],
                                              'kind': package['kind'],
                                              'priority_hint': priority_order[path],
                                              'overlap_candidate': previous is not None,
                                              'overlaps_with': previous,
                                              'overlap_resolution': 'HEURISTIC_ONLY',
                                              'path': entry.path.replace('\\', '/'),
                                              'offset': entry.offset,
                                              'header_size': entry.header_size,
                                              'size': entry.size,
                                              'stored_size': entry.stored_size,
                                              'sha1_stored': entry.sha1,
                                              'index_sha256': entry.index_sha256,
                                              'compression': entry.compression},
                                             ensure_ascii=False, sort_keys=True) + '\n')
                        seen_entries.setdefault(key, package['archive'])
                    for pruned in metadata.pruned_paths:
                        key = pruned.casefold()
                        previous = seen_entries.get(key)
                        out.write(json.dumps({'type': 'pruned_entry',
                                              'archive': package['archive'],
                                              'kind': package['kind'],
                                              'priority_hint': priority_order[path],
                                              'path': pruned.replace('\\', '/'),
                                              'overlap_candidate': previous is not None,
                                              'overlaps_with': previous,
                                              'resolution': 'UNKNOWN'},
                                             ensure_ascii=False, sort_keys=True) + '\n')
                        seen_entries.setdefault(key, package['archive'])
                summary = {'type': 'complete', 'package_count': package_count,
                           'available_entries': entry_count,
                           'inventory_complete': False,
                           'note': 'Encrypted or unsupported indexes and active client set remain UNKNOWN.'}
                out.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + '\n')
                out.flush()
                os.fsync(out.fileno())
            os.rename(temporary, target)
            return {'path': target, 'package_count': package_count,
                    'available_entries': entry_count}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
