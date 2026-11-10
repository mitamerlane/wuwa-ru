"""Inventory only: no extraction, package loading, installation or game launch."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

from .discovery import Installation, read_metadata
from .paths import UnsafePath, inside, reject_links

VERSION = re.compile(r'\d+(?:\.\d+){1,3}')
MOUNTS = ('MountResource.txt', 'MountResource_HD.txt', 'MountLauncher.txt', 'MountLang_en.txt')


class Cancelled(RuntimeError):
    pass


def check_cancel(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise Cancelled('Операция отменена; игровые файлы не изменены.')


def fingerprint(path: Path, digest: bool, event: threading.Event | None) -> dict:
    check_cancel(event)
    reject_links(path)
    before = path.stat()
    sha256 = None
    if digest:
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                check_cancel(event)
                hasher.update(block)
        sha256 = hasher.hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError(f'Файл изменился во время чтения: {path.name}')
    return {'size': after.st_size, 'mtime_ns': after.st_mtime_ns, 'sha256': sha256}


def mount_records(text: str) -> list[dict]:
    """Preserve declared order and opaque metadata, without inferring runtime priority.

    Only ::Mount:: records are interpreted; ::Del:: syntax remains unknown.
    The trailing fields are not assumed to be hashes or digital signatures.
    """
    section = None
    seen_mount = False
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('::'):
            if line not in ('::Mount::', '::Del::'):
                raise ValueError(f'Неизвестная секция Mount: {line}')
            section = line
            seen_mount |= line == '::Mount::'
            continue
        if section is None:
            raise ValueError('Данные вне секции Mount')
        if section == '::Mount::':
            fields = line.split(',')
            if len(fields) < 3 or not fields[0] or not re.fullmatch(r'[0-9]+', fields[1]):
                raise ValueError('Повреждённая запись Mount')
            entries.append({'path': fields[0] + '.pak',
                            'declared_order': int(fields[1]),
                            'opaque_fields': tuple(fields[2:])})
    if not seen_mount:
        raise ValueError('Отсутствует секция ::Mount::')
    return entries


def mount_entries(text: str) -> list[str]:
    return [record['path'] for record in mount_records(text)]


def create_manifest(installation: Installation, *, resource_version: str | None = None,
                    hash_packages: bool = False,
                    cancelled: threading.Event | None = None) -> dict:
    """Stable schema v1. Metadata mode never claims package integrity verification.

    Exactly one resource folder is a candidate, not proof it is mounted by a client.
    Multiple folders require an explicit selection. Unsupported layouts fail closed.
    """
    check_cancel(cancelled)
    root = installation.root
    warnings = ['Нативная установка и совместимость с античитом не проверены.']
    resource_root = inside(root, 'Client/Saved/Resources')
    candidates = []
    if resource_root.is_dir():
        for directory in resource_root.iterdir():
            if VERSION.fullmatch(directory.name):
                checked = inside(root, directory.relative_to(root).as_posix())
                if checked.is_dir() and inside(checked, 'Mount/MountResource.txt').is_file():
                    candidates.append(directory.name)
    candidates.sort(key=lambda value: tuple(map(int, value.split('.'))))
    selection = 'unknown'
    if resource_version is not None:
        if resource_version not in candidates:
            raise ValueError('Запрошенная версия ресурсов не найдена')
        selection = 'explicit'
    elif len(candidates) == 1:
        resource_version = candidates[0]
        selection = 'single_candidate'
    else:
        warnings.append('Версия ресурсов не определена; укажи её явно при нескольких каталогах.')
    packages: dict[str, set[str]] = {}
    base = inside(root, 'Client/Content/Paks')
    for path in sorted(base.glob('*.pak')):
        relative = path.relative_to(root).as_posix()
        inside(root, relative)
        packages.setdefault(relative, set()).add('base_directory_unverified')
    mounts = []
    patch_versions = set()
    inventory_complete = bool(packages) and resource_version is not None
    if resource_version is not None:
        resources = inside(root, f'Client/Saved/Resources/{resource_version}')
        mount_folder = inside(resources, 'Mount')
        language_mounts = []
        for count, path in enumerate(mount_folder.iterdir()):
            if count >= 256:
                raise ValueError('Слишком много Mount файлов')
            if re.fullmatch(r'MountLang_[a-z]{2,3}(?:_[A-Za-z]{2,4})?\.txt', path.name):
                language_mounts.append(path.name)
        for name in sorted(set(MOUNTS) | set(language_mounts)):
            check_cancel(cancelled)
            mount = inside(resources, f'Mount/{name}')
            if not mount.is_file():
                if name in ('MountResource.txt', 'MountLang_en.txt'):
                    warnings.append(f'Нет обязательного для инвентаризации {name}')
                    inventory_complete = False
                continue
            evidence = fingerprint(mount, True, cancelled)
            entries = mount_entries(read_metadata(mount))
            # Bind parsed bytes to the hash; detect ordinary concurrent updates.
            if evidence != fingerprint(mount, True, cancelled):
                raise RuntimeError(f'Манифест изменился во время чтения: {name}')
            mounts.append({'path': mount.relative_to(root).as_posix(), **evidence,
                           'entries': len(entries)})
            for entry in entries:
                pak = inside(resources, entry)
                if not pak.is_file():
                    raise FileNotFoundError(f'Нет указанного пакета: {pak.relative_to(root)}')
                relative = pak.relative_to(root).as_posix()
                packages.setdefault(relative, set()).add(name)
                patch_versions.update(p for p in Path(entry).parts if VERSION.fullmatch(p))
    if not packages:
        warnings.append('Не найдены PAK; установка неполная или формат не поддержан.')
    items = []
    for relative, origins in sorted(packages.items()):
        pak = inside(root, relative)
        item = {'path': relative, 'origins': sorted(origins),
                **fingerprint(pak, hash_packages, cancelled)}
        signature = pak.with_suffix('.sig')
        item['signature_present'] = inside(root, signature.relative_to(root).as_posix()).is_file()
        items.append(item)
    if not hash_packages:
        warnings.append('Хеши PAK не вычислялись; размер и mtime не доказывают целостность.')
    data = {
        'schema_version': 1, 'mode': 'READ_ONLY', 'game_root': str(root),
        'client_version': installation.client_version,
        'client_version_evidence': None,
        'steam_build_id': installation.steam_build_id,
        'discovery_origins': sorted(installation.origins),
        'resource_version': resource_version,
        'resource_version_candidates': candidates,
        'resource_version_selection': selection,
        'referenced_patch_versions': sorted(patch_versions, key=lambda v: tuple(map(int, v.split('.')))),
        'hash_scope': 'full' if hash_packages else 'metadata',
        'inventory_complete': inventory_complete,
        'mounts': mounts, 'packages': items, 'warnings': warnings,
        'compatibility': 'UNKNOWN', 'installation_allowed': False,
        'package_format': 'UNKNOWN', 'cyrillic_fonts': 'UNKNOWN',
    }
    data['inventory_fingerprint_sha256'] = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    check_cancel(cancelled)
    return data


def write_manifest(manifest: dict, destination: Path) -> None:
    """Publish a new report atomically, without replacing any existing file.

    Windows rename fails if occupied; POSIX uses exclusive hard-link publication.
    Unsupported filesystems produce an error rather than a partial report.
    """
    destination = Path(destination).absolute()
    reject_links(destination)
    root = Path(manifest['game_root']).resolve()
    if destination.resolve().is_relative_to(root):
        raise UnsafePath('Отчёт нельзя записывать внутрь игровой установки')
    if destination.exists():
        raise FileExistsError(destination)
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent)[2] < len(payload) + 1024 * 1024:
        raise OSError('Недостаточно свободного места для отчёта')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.manifest-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        reject_links(destination)
        if os.name == 'nt':
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
