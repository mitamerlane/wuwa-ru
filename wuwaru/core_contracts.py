"""SELF-CORE contracts. Non-reader protocols describe future adapters, not services.

No TranslationDB migration or replacement of existing public functions is implied.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypedDict


class ErrorCode(StrEnum):
    UNSUPPORTED_FORMAT = 'UNSUPPORTED_FORMAT'
    ENCRYPTED_INDEX = 'ENCRYPTED_INDEX'
    KEY_UNAVAILABLE = 'KEY_UNAVAILABLE'
    DECOMPRESSION_UNAVAILABLE = 'DECOMPRESSION_UNAVAILABLE'
    CORRUPTED_ARCHIVE = 'CORRUPTED_ARCHIVE'
    VERSION_UNKNOWN = 'VERSION_UNKNOWN'
    SCHEMA_MISMATCH = 'SCHEMA_MISMATCH'
    RESOURCE_MISMATCH = 'RESOURCE_MISMATCH'


class ResourceSourceType(StrEnum):
    SQLITE_STAGING = 'SQLITE_STAGING'
    REAL_LOCAL_RESOURCE = 'REAL_LOCAL_RESOURCE'
    PUBLIC_TEXTMAP = 'PUBLIC_TEXTMAP'
    SYNTHETIC_FIXTURE = 'SYNTHETIC_FIXTURE'


class CoreError(ValueError):
    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        super().__init__(f'{code.value}: {message}')


@dataclass(frozen=True)
class ProbeResult:
    supported: bool
    code: ErrorCode | None
    detail: str


@dataclass(frozen=True)
class ArchiveEntry:
    archive: Path
    path: str
    offset: int  # serialized header; payload starts at offset + header_size
    size: int
    sha1: str
    index_sha256: str
    identity: tuple[int, int, int]  # size, mtime_ns, inode
    stored_size: int | None = None  # None preserves old plain-entry construction
    header_size: int = 53
    compression: str = 'None'
    blocks: tuple[tuple[int, int], ...] = ()  # absolute (start, end)
    block_size: int = 0


@dataclass(frozen=True)
class ArchiveMetadata:
    archive: Path
    size: int
    version: int
    mount_point: str
    entries: tuple[ArchiveEntry, ...]
    total_size: int
    index_sha256: str
    pruned_entries: int = 0
    active_entries: int | None = None
    pruned_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntryVerification:
    path: str
    size: int
    sha1: str
    sha256: str
    destination: Path | None = None


@dataclass(frozen=True)
class ArchiveVerification:
    archive_sha256: str
    entries: tuple[EntryVerification, ...]


class ArchiveReader(Protocol):
    def probe(self, path: Path) -> ProbeResult: ...
    def read_metadata(self, path: Path) -> ArchiveMetadata: ...
    def list_entries(self, path: Path) -> tuple[ArchiveEntry, ...]: ...
    def extract_entry(self, entry: ArchiveEntry, destination: Path) -> EntryVerification: ...
    def verify_archive(self, path: Path) -> ArchiveVerification: ...


@dataclass(frozen=True)
class VersionIdentity:
    client_version: str | None
    resource_version: str | None
    status: str  # UNKNOWN until independent evidence exists


class ResourceManifest(TypedDict):
    schema_version: int
    inventory_fingerprint_sha256: str
    resource_version: str | None
    inventory_complete: bool


class ResourceCatalog(Protocol):
    def discover_resources(self) -> tuple[Path, ...]: ...
    def build_manifest(self) -> ResourceManifest: ...
    def identify_version(self) -> VersionIdentity: ...
    def resolve_resource_priority(self, resources: tuple[Path, ...]) -> tuple[Path, ...]: ...


@dataclass(frozen=True)
class SourceRecord:
    table: str
    identifier: int | str
    content: str


@dataclass(frozen=True)
class DatabaseValidation:
    sha256: str
    rows: int
    schema_sha256: str


class SQLiteSourceAdapter(Protocol):
    def inspect_schema(self, path: Path) -> Mapping[str, tuple[str, ...]]: ...
    def validate_database(self, path: Path) -> DatabaseValidation: ...
    def import_records(self, path: Path) -> Iterable[SourceRecord]: ...
    def compare_source_records(self, left: Path, right: Path) -> bool: ...


@dataclass(frozen=True)
class PackageManifest:
    archive: Path
    sha256: str
    resources: tuple[EntryVerification, ...]
    installation_allowed: bool = False


class PackageBuilder(Protocol):
    def build_package(self, source: Path, destination: Path) -> PackageManifest: ...
    def verify_package(self, archive: Path, expected: tuple[EntryVerification, ...]) -> PackageManifest: ...
    def build_manifest(self, archive: Path) -> PackageManifest: ...


@dataclass(frozen=True)
class CompatibilityResult:
    status: str = 'UNKNOWN'
    installation_allowed: bool = False
    reasons: tuple[str, ...] = ()


class CompatibilityChecker(Protocol):
    def inspect_target(self, path: Path) -> VersionIdentity: ...
    def compare_fingerprints(self, expected: str, actual: str) -> bool: ...
    def evaluate_compatibility(self, target: VersionIdentity,
                               package: PackageManifest) -> CompatibilityResult: ...
