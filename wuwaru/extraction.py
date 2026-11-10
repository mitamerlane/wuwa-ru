"""Read-only extraction/import with byte provenance and exclusive staging publication."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .core_contracts import (ArchiveEntry, ArchiveReader, DatabaseValidation,
                             EntryVerification, ResourceCatalog, ResourceSourceType,
                             SQLiteSourceAdapter)
from .discovery import Installation
from .manifest import check_cancel, create_manifest, fingerprint
from .pak_reader import MAX_FILES, MAX_TOTAL, read_databases
from .paths import UnsafePath, inside, reject_links
from .payload_verify import verify_payloads
from .sqlite_source import MAX_DATABASE, inspect_database

_LANGUAGE_ENTRY = re.compile(r'Client/Content/Aki/ConfigDB/en/lang_[A-Za-z0-9_]+\.db')


@dataclass(frozen=True)
class ArchiveDatabaseInspection:
    archive: Path
    entry: ArchiveEntry
    source_type: ResourceSourceType
    verification: EntryVerification
    validation: DatabaseValidation
    source_metadata: dict[str, str]


def inspect_catalog_databases(catalog: ResourceCatalog, reader: ArchiveReader,
                              sqlite_source: SQLiteSourceAdapter, staging: Path, *,
                              source_type: ResourceSourceType = ResourceSourceType.REAL_LOCAL_RESOURCE,
                              cancelled: Event | None = None) -> tuple[ArchiveDatabaseInspection, ...]:
    """Connect catalog packages, the typed archive reader and SQLite validation.

    Only the established English ConfigDB/lang_*.db route is exposed to the
    Translation Core. PUBLIC_TEXTMAP keeps its separate JSON adapter.
    """
    if source_type == ResourceSourceType.PUBLIC_TEXTMAP:
        raise ValueError('PUBLIC_TEXTMAP не является записью PAK и импортируется отдельно')
    if not isinstance(source_type, ResourceSourceType):
        raise TypeError('source_type должен быть ResourceSourceType')
    if source_type not in (ResourceSourceType.REAL_LOCAL_RESOURCE,
                           ResourceSourceType.SYNTHETIC_FIXTURE):
        raise ValueError('Для чтения PAK укажи REAL_LOCAL_RESOURCE или SYNTHETIC_FIXTURE')
    resources = tuple(catalog.discover_resources())
    ordered = tuple(catalog.resolve_resource_priority(resources))
    if len(ordered) != len(resources) or set(ordered) != set(resources):
        raise ValueError('ResourceCatalog вернул изменённый или дублирующийся список PAK')
    destination = Path(staging).absolute()
    reject_links(destination)
    output = []
    for archive in ordered:
        check_cancel(cancelled)
        metadata = reader.read_metadata(archive)
        entries = tuple(entry for entry in metadata.entries
                        if _LANGUAGE_ENTRY.fullmatch(entry.path))
        if not entries:
            continue
        archive_stage = destination / f'{archive.stem}-{metadata.index_sha256[:16]}'
        if archive_stage.exists():
            raise FileExistsError(archive_stage)
        for entry in entries:
            check_cancel(cancelled)
            verification = reader.extract_entry(entry, archive_stage)
            validation = sqlite_source.validate_database(verification.destination)
            if validation.sha256 != verification.sha256:
                raise ValueError('SHA-256 SQLite не совпадает с проверенным PAK entry')
            if validation.rows <= 0:
                raise ValueError('PAK entry не содержит импортируемых строк SQLite')
            source_metadata = {
                'archive': str(Path(archive).absolute()),
                'entry_path': entry.path,
                'archive_index_sha256': entry.index_sha256,
            }
            output.append(ArchiveDatabaseInspection(
                Path(archive), entry, source_type, verification, validation, source_metadata))
    return tuple(output)


def extract(installation: Installation, destination: Path, *, packages: list[str],
            resource_version: str | None = None, expected_inventory: str | None = None,
            cancelled: Event | None = None) -> dict:
    return _stage(installation, destination, packages=packages,
                  resource_version=resource_version, expected_inventory=expected_inventory,
                  cancelled=cancelled, source=None)


def import_databases(installation: Installation, source: Path, destination: Path, *,
                     packages: list[str], resource_version: str | None = None,
                     expected_inventory: str | None = None,
                     cancelled: Event | None = None) -> dict:
    """Import only when standalone DB bytes exactly match the selected game PAKs."""
    return _stage(installation, destination, packages=packages,
                  resource_version=resource_version, expected_inventory=expected_inventory,
                  cancelled=cancelled, source=Path(source).absolute())


def _stage(installation: Installation, destination: Path, *, packages: list[str],
           resource_version: str | None, expected_inventory: str | None,
           cancelled: Event | None, source: Path | None) -> dict:
    check_cancel(cancelled)
    root = installation.root.resolve()
    destination = Path(destination).absolute()
    reject_links(destination)
    resolved = destination.resolve()
    if resolved.is_relative_to(root) or root.is_relative_to(resolved):
        raise UnsafePath('Staging должен находиться вне установки и её предков')
    if destination.exists():
        raise FileExistsError(destination)
    if source is not None:
        reject_links(source)
        if resolved.is_relative_to(source.resolve()) or source.resolve().is_relative_to(resolved):
            raise UnsafePath('Staging и импортируемый каталог не должны пересекаться')
    manifest = create_manifest(installation, resource_version=resource_version, cancelled=cancelled)
    if manifest['resource_version'] is None or not manifest['inventory_complete']:
        raise ValueError('Нужен однозначный полный манифест выбранного каталога ресурсов')
    if expected_inventory is not None and expected_inventory != manifest['inventory_fingerprint_sha256']:
        raise ValueError('Выбранная установка/версия изменилась относительно ожидаемого манифеста')
    listed = {item['path'] for item in manifest['packages']}
    if not packages or len(packages) > MAX_FILES or len(set(packages)) != len(packages):
        raise ValueError('Нужен непустой список уникальных пакетов')
    if any(relative not in listed for relative in packages):
        raise ValueError('Пакет отсутствует в манифесте выбранных ресурсов')
    payloads = {}
    origins = {}
    evidence = []
    total = 0
    for relative in sorted(packages):
        path = inside(root, relative)
        before = fingerprint(path, True, cancelled)
        contents = read_databases(path, max_total=MAX_TOTAL - total, cancelled=cancelled)
        verify_payloads(path, contents, cancelled=cancelled)
        if fingerprint(path, True, cancelled) != before:
            raise RuntimeError('Пакет изменился между независимыми чтениями')
        for name, data in contents.items():
            if name.casefold() in origins:
                raise ValueError('Перекрывающиеся БД: приоритет пакетов не подтверждён')
            total += len(data)
            if len(payloads) >= MAX_FILES or total > MAX_TOTAL:
                raise ValueError('Превышен лимит staging')
            origins[name.casefold()] = relative
            payloads[name] = data
        evidence.append({'path': relative, **before})
    if not payloads:
        raise ValueError('Нет английских БД для извлечения')
    if source is not None:
        imported = {}
        for path in source.iterdir():
            reject_links(path)
            if not path.is_file() or path.suffix != '.db' or not 0 < path.stat().st_size <= MAX_DATABASE:
                raise ValueError('Импорт принимает только самостоятельные .db в одном каталоге')
            if len(imported) >= MAX_FILES:
                raise ValueError('Слишком много импортируемых SQLite')
            inspect_database(path, cancelled=cancelled)
            imported[path.name] = path.read_bytes()
            if sum(map(len, imported.values())) > MAX_TOTAL:
                raise ValueError('Превышен лимит импорта')
        if imported != payloads:
            raise ValueError('Импортированные SQLite не совпадают с выбранными игровыми пакетами')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent)[2] < total + 2 * 1024 * 1024:
        raise OSError('Недостаточно места для staging')
    with tempfile.TemporaryDirectory(prefix='.extract-', dir=destination.parent) as folder:
        stage = Path(folder) / 'source'
        stage.mkdir()
        databases = []
        for name, data in sorted(payloads.items()):
            check_cancel(cancelled)
            target = inside(stage, name)
            with target.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            first = inspect_database(target, cancelled=cancelled)
            if inspect_database(target, independent=True, cancelled=cancelled) != first:
                raise ValueError('Повторная SQLite-валидация дала другой результат')
            databases.append({'name': name, 'package': origins[name.casefold()], 'validation': first})
        fresh = create_manifest(installation, resource_version=resource_version, cancelled=cancelled)
        if fresh != manifest:
            raise RuntimeError('Установка/манифест изменились во время извлечения')
        for item in evidence:
            current = fingerprint(inside(root, item['path']), True, cancelled)
            if current != {key: item[key] for key in ('size', 'mtime_ns', 'sha256')}:
                raise RuntimeError('Исходный пакет изменился перед публикацией')
        report = {
            'schema_version': 1, 'mode': 'READ_ONLY', 'reader': 'wuwaru-v12-plain-subset-v1',
            'operation': 'import' if source is not None else 'extract',
            'client_version': None, 'client_version_status': 'UNKNOWN',
            'client_version_evidence': None,
            'client_version_reason': 'Нет проверенного источника семантической версии клиента; '
                                     'Steam build, UE version и имена каталогов не используются.',
            'resource_version': manifest['resource_version'],
            'resource_version_selection': manifest['resource_version_selection'],
            'active_resource_version_status': 'UNKNOWN',
            'language_resource_identity_sha256': hashlib.sha256(json.dumps(
                [(d['name'], d['validation']['sha256']) for d in databases],
                separators=(',', ':')).encode('utf-8')).hexdigest(),
            'source_binding': 'SELECTED_PACKAGE_BYTES_VERIFIED',
            'complete_game_language_inventory': False,
            'inventory': manifest, 'selected_packages': evidence, 'databases': databases,
            'verification': {'payloads': 'independent_serialized_scan',
                             'sqlite': 'fresh_connection_reverse_order',
                             'independent_sqlite_engine': False},
            'compatibility': 'UNKNOWN', 'installation_allowed': False,
        }
        with (stage / 'source-manifest.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        check_cancel(cancelled)
        reject_links(destination)
        # Windows rename is exclusive. On POSIX an existing empty directory could
        # be replaced: refuse that platform until exclusive dir publication exists.
        if os.name != 'nt':
            raise OSError('Атомарная публикация каталогов поддержана только на Windows')
        os.rename(stage, destination)
    return report
