"""Portable application paths and bounded discovery of existing translation workspaces."""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .flow_context import WAVES
from .paths import inside, reject_links

# Waves checked on screens in NATIVE-TEXT-WAVES; any other text_kind is opt-in and unverified in game.
DEFAULT_NATIVE_KINDS = ('speaker', 'ui')
# Every kind text_kind can return, in WAVES order; 'scene' labels flow imports and is never returned.
NATIVE_KINDS = tuple(dict.fromkeys(kind for kinds in WAVES.values() for kind in kinds if kind != 'scene'))


class PathManager:
    """Own all application-managed paths under one explicit portable root."""

    def __init__(self, application_root: Path):
        self.root = Path(application_root).expanduser().absolute()
        self.data = self.root / 'Data'
        self.projects = self.data / 'Projects'
        self.sources = self.data / 'Sources'
        self.native = self.data / 'Native'
        self.imports = self.root / 'Imports'
        self.exports = self.root / 'Exports'
        self.backups = self.root / 'Backups'
        self.logs = self.root / 'Logs'
        self.settings_path = self.data / 'settings.json'

    def initialize(self) -> None:
        reject_links(self.root)
        for path in (self.projects, self.sources, self.native, self.imports, self.exports, self.backups,
                     self.logs):
            path.mkdir(parents=True, exist_ok=True)

    def is_writable(self) -> bool:
        try:
            self.data.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix='.write-check-', dir=self.data, delete=True):
                pass
            return True
        except OSError:
            return False

    def last_project(self) -> Path | None:
        try:
            reject_links(self.settings_path)
            raw = self.settings_path.read_text(encoding='utf-8')
            if len(raw) > 64 * 1024:
                return None
            payload = json.loads(raw)
            value = payload.get('last_project')
            if not isinstance(value, str) or not value:
                return None
            path = Path(value).expanduser()
            if payload.get('last_project_relative') is True:
                if path.is_absolute() or '..' in path.parts:
                    return None
                return (self.root / path).absolute()
            return path.absolute()
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _settings(self) -> dict:
        try:
            reject_links(self.settings_path)
            raw = self.settings_path.read_text(encoding='utf-8')
            if len(raw) > 64 * 1024:
                return {}
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def game_path(self) -> Path | None:
        value = self._settings().get('game_path')
        return Path(value).expanduser().absolute() if isinstance(value, str) and value else None

    def save_game_path(self, game_path: Path) -> None:
        self._save_settings(game_path=str(Path(game_path).expanduser().absolute()))

    def native_kinds(self) -> tuple[str, ...]:
        value = self._settings().get('native_kinds')
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return DEFAULT_NATIVE_KINDS
        known = tuple(dict.fromkeys(item for item in value if item in NATIVE_KINDS))
        return known or DEFAULT_NATIVE_KINDS  # an empty set would build a package with nothing in it

    def save_native_kinds(self, kinds) -> None:
        self._save_settings(native_kinds=list(kinds))

    def timing(self, name: str, default: float) -> float:
        """Seconds the step took last time; the Play page's progress bar is paced by it."""
        value = (self._settings().get('timings') or {}).get(name)
        return float(value) if isinstance(value, (int, float)) and 0.2 <= value <= 3600 else default

    def save_timing(self, name: str, seconds: float) -> None:
        timings = self._settings().get('timings')
        timings = dict(timings) if isinstance(timings, dict) else {}
        timings[name] = round(min(max(seconds, 0.2), 3600.0), 2)
        self._save_settings(timings=timings)

    def save_public_source(self, name: str, lock: Path, source_root: Path, workspace: Path) -> None:
        sources = self._settings().get('public_sources', {})
        sources = sources if isinstance(sources, dict) else {}
        binding = {}
        for key, path in dict(lock=lock, source_root=source_root, workspace=workspace).items():
            path = Path(path).absolute()
            relative = path.is_relative_to(self.root)
            binding[key] = dict(path=str(path.relative_to(self.root) if relative else path), relative=relative)
        self._save_settings(public_sources={**sources, name: binding})

    def public_source(self, name: str) -> dict | None:
        try:
            binding = self._settings()['public_sources'][name]
            result = {}
            for key in ('lock', 'source_root', 'workspace'):
                item = binding[key]
                path = Path(item['path'])
                if item['relative']:
                    if path.is_absolute() or '..' in path.parts:
                        return None
                    path = self.root / path
                elif not path.is_absolute():
                    return None
                reject_links(path)
                result[key] = path.absolute()
            return result
        except (KeyError, TypeError, ValueError, OSError):
            return None

    def needs_self_containment(self, workspace: Path) -> bool:
        workspace = Path(workspace).absolute()
        rollback = self.data / 'self-contained-rollback.json'
        reject_links(rollback)
        if rollback.is_file():
            if rollback.stat().st_size > 64 * 1024:
                raise ValueError('Слишком большой журнал отката')
            record = json.loads(rollback.read_text('utf-8'))
            if record.get('rolled_back') and workspace == self.last_project():
                return False
        if not workspace.is_relative_to(self.projects):
            return True
        for name in self._settings().get('public_sources', {}):
            binding = self.public_source(name)
            if binding is None:
                return True
            if binding['workspace'] == workspace and any(
                    not binding[key].is_relative_to(self.sources) for key in ('lock', 'source_root')):
                return True
        return False

    def _save_settings(self, **updates) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        reject_links(self.settings_path)
        payload = {**self._settings(), 'schema_version': 1, **updates}
        raw = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
        descriptor, temporary = tempfile.mkstemp(prefix='.settings-', suffix='.tmp', dir=self.data)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.settings_path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save_last_project(self, project: Path) -> None:
        project = Path(project).expanduser().absolute()
        try:
            stored_path = project.relative_to(self.root)
            relative = True
        except ValueError:
            stored_path = project
            relative = False
        self._save_settings(last_project=str(stored_path), last_project_relative=relative)


@dataclass(frozen=True)
class WorkspaceCandidate:
    path: Path
    name: str
    version: int | None
    modified: datetime
    record_count: int
    translated_count: int
    manual_count: int
    catalog_references: tuple[str, ...]


class WorkspaceDiscoveryService:
    """Inspect saved, portable and explicitly known roots without disk-wide scans."""

    def __init__(self, paths: PathManager, *, known_roots=()):
        self.paths = paths
        self.known_roots = tuple(Path(root).expanduser().absolute() for root in known_roots)

    def discover(self) -> list[WorkspaceCandidate]:
        saved = self.paths.last_project()
        roots = [self.paths.projects, *self.known_roots]
        seen: set[Path] = set()
        candidates: dict[Path, WorkspaceCandidate] = {}

        def inspect(path: Path) -> None:
            try:
                reject_links(path)
                path = path.resolve(strict=True)
                if path in seen or not path.is_dir():
                    return
                seen.add(path)
                db_path = path / 'translations.sqlite'
                reject_links(db_path)
                if not db_path.is_file():
                    return
                db = sqlite3.connect(f'{db_path.as_uri()}?mode=ro', uri=True)
                try:
                    if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                        return
                    row = db.execute(
                        "SELECT count(*),sum(target IS NOT NULL AND translated_hash=source_hash "
                        "AND status NOT IN ('INVALID','NEEDS_REVIEW')) FROM records WHERE active=1"
                    ).fetchone()
                    columns = {column[1] for column in db.execute('PRAGMA table_info(records)')}
                    manual_count = db.execute(
                        "SELECT count(*) FROM records WHERE active=1 AND target IS NOT NULL "
                        "AND status NOT IN ('INVALID','NEEDS_REVIEW') AND manual=1"
                    ).fetchone()[0] if 'manual' in columns else 0
                    version = db.execute('PRAGMA user_version').fetchone()[0]
                finally:
                    db.close()
                manifest = path / 'workspace.json'
                catalog_references = []
                if manifest.is_file() and manifest.stat().st_size <= 1024 * 1024:
                    try:
                        payload = json.loads(manifest.read_text(encoding='utf-8'))
                        catalog_references = [item['path'] for item in payload.get('resource_catalogs', [])
                                              if isinstance(item, dict) and isinstance(item.get('path'), str)]
                    except (OSError, ValueError, TypeError, AttributeError):
                        catalog_references = []
                local_catalogs = path / 'catalogs'
                if local_catalogs.is_dir():
                    catalog_references.extend(str(item) for item in local_catalogs.iterdir()
                                              if item.is_file() and not item.is_symlink()
                                              and item.suffix.casefold() in ('.jsonl', '.sqlite'))
                candidates[path] = WorkspaceCandidate(
                    path=path, name=path.name, version=version or None,
                    modified=datetime.fromtimestamp(db_path.stat().st_mtime).astimezone(),
                    record_count=row[0], translated_count=row[1] or 0,
                    manual_count=manual_count,
                    catalog_references=tuple(dict.fromkeys(catalog_references)))
            except (OSError, sqlite3.Error, ValueError):
                return

        if saved is not None:
            inspect(saved)
        for root in roots:
            try:
                reject_links(root)
                if not root.is_dir():
                    continue
                inspect(root)
                # Only the root and its immediate child projects are considered.
                for child in root.iterdir():
                    if child.is_dir():
                        inspect(child)
            except OSError:
                continue
        return sorted(candidates.values(), key=lambda item: (
            0 if saved is not None and item.path == saved.resolve(strict=False) else 1,
            item.name.casefold(), str(item.path).casefold()))


@dataclass(frozen=True)
class MigrationResult:
    destination: Path
    backup: Path
    destination_hash: str
    backup_hash: str
    record_count: int
    reused: bool


class MigrationService:
    """Snapshot and migrate only the translation database; never copy PAK trees."""

    def __init__(self, paths: PathManager):
        self.paths = paths
        self.journal = paths.data / 'migration-journal.jsonl'

    def make_self_contained(self, workspace: Path, *, bindings=None, cancelled=None) -> MigrationResult:
        """Publish settings only after the project and pinned sources are verified.

        Originals and verified SQLite/settings backups remain available for rollback.
        Source folders are shared by pinned revision, never copied per project.
        """
        from .manifest import check_cancel
        from .public_corpus import read_lock

        workspace = Path(workspace).absolute()
        self.paths.initialize()
        manifest_file = workspace / 'workspace.json'
        reject_links(manifest_file)
        if manifest_file.is_file() and manifest_file.stat().st_size > 1024 * 1024:
            raise ValueError('Слишком большой манифест проекта')
        if workspace.is_relative_to(self.paths.projects) and manifest_file.is_file():
            manifest = json.loads(manifest_file.read_text('utf-8'))
            if manifest.get('self_contained') and bindings is None:
                database = workspace / 'translations.sqlite'
                digest = self._hash(database)
                backup = inside(self.paths.root, manifest['backup'])
                if self._hash(backup) != manifest['backup_sha256']:
                    raise ValueError('Контрольная сумма резервной копии не совпала')
                candidates = WorkspaceDiscoveryService(self.paths, known_roots=[workspace]).discover()
                candidate = next(item for item in candidates if item.path == workspace)
                return MigrationResult(workspace, backup, digest, manifest['backup_sha256'], candidate.record_count, True)
        settings_before = self.paths._settings()
        if bindings is None:
            bindings = {}
            for name in settings_before.get('public_sources', {}):
                binding = self.paths.public_source(name)
                if binding is None:
                    raise ValueError(f'Некорректная привязка корпуса: {name}')
                if binding['workspace'] == workspace:
                    bindings[name] = binding
        portable_bindings = {}
        for name, binding in bindings.items():
            check_cancel(cancelled)
            source = read_lock(binding['lock'], name)
            # The repository basename and revision come from the validated lock.
            folder = inside(self.paths.sources, source['repository'].rsplit('/', 1)[1] + '-' + source['revision'])
            folder.mkdir(exist_ok=True)
            for item in source['files']:
                check_cancel(cancelled)
                target = inside(folder, item['path'])
                if not target.exists():
                    original = inside(binding['source_root'], item['path'])
                    if original.stat().st_size != item['size'] or self._hash(original) != item['sha256']:
                        raise ValueError('Корпус не совпадает с lock: ' + item['path'])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self._copy_atomic(original, target)
                if target.stat().st_size != item['size'] or self._hash(target) != item['sha256']:
                    raise ValueError('Контрольная сумма корпуса не совпала: ' + item['path'])
            # Store only this pinned source, not unrelated corpora from the lock.
            lock = folder / 'source-lock.json'
            raw = json.dumps(dict(schema_version=1, sources={name: source}), ensure_ascii=False, indent=2) + '\n'
            reject_links(lock)
            if lock.exists():
                if lock.read_text('utf-8') != raw:
                    raise ValueError('Конфликт закреплённого lock корпуса')
            else:
                with tempfile.TemporaryDirectory(dir=self.paths.sources) as temp:
                    staged = Path(temp) / 'lock.json'
                    staged.write_text(raw, encoding='utf-8')
                    self._copy_atomic(staged, lock)
            portable_bindings[name] = dict(lock=lock, source_root=folder, source=source)
        check_cancel(cancelled)
        if workspace.is_relative_to(self.paths.projects):
            backup = create_project_backup(self.paths, workspace)
            result = MigrationResult(workspace, backup['path'], backup['sha256'], backup['sha256'],
                                     backup['record_count'], True)
        else:
            result = self.migrate(workspace)
        # Keep existing diagnostic catalogs inside the project too; never copy PAK.
        manifest_path = result.destination / 'workspace.json'
        reject_links(manifest_path)
        manifest = json.loads(manifest_path.read_text('utf-8')) if manifest_path.exists() else dict(schema_version=1)
        for reference in manifest.get('resource_catalogs', []):
            target = Path(reference['path'])
            if not target.is_absolute():
                target = inside(result.destination, reference['path'])
            reject_links(target)
            if target.suffix.casefold() not in ('.jsonl', '.sqlite') or target.parent.name.casefold() != 'catalogs':
                raise ValueError('Недопустимая ссылка на диагностический каталог')
            local = inside(result.destination, 'catalogs/' + target.name)
            if not local.exists():
                local.parent.mkdir(exist_ok=True)
                self._copy_atomic(target, local)
            if self._hash(local) != reference['sha256']:
                raise ValueError('Контрольная сумма каталога не совпала')
            reference['path'] = local.relative_to(result.destination).as_posix()
        db = sqlite3.connect(result.destination / 'translations.sqlite')
        try:
            with db:
                for binding in portable_bindings.values():
                    source = binding['source']
                    prefix = 'public/' + source['repository'].removeprefix('https://github.com/') + '/'
                    for item in source['files']:
                        relative = os.path.relpath(binding['source_root'] / item['path'], result.destination)
                        db.execute('UPDATE resources SET path=? WHERE name=?', (relative, prefix + item['path']))
        finally:
            db.close()
        final_hash = self._hash(result.destination / 'translations.sqlite')
        manifest.update(translations_sha256=final_hash, self_contained=True,
                        backup=str(result.backup.relative_to(self.paths.root)), backup_sha256=result.backup_hash)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        sources = dict(settings_before.get('public_sources', {}))
        for name, binding in portable_bindings.items():
            sources[name] = {key: dict(path=str(path.relative_to(self.paths.root)), relative=True)
                             for key, path in dict(lock=binding['lock'], source_root=binding['source_root'],
                                                   workspace=result.destination).items()}
        # A settings rollback changes the active project; it never deletes new work.
        if self.paths.settings_path.exists():
            digest = self._hash(self.paths.settings_path)
            backup = self.paths.backups / ('settings-' + digest + '.json')
            if not backup.exists():
                self._copy_atomic(self.paths.settings_path, backup)
            if self._hash(backup) != digest:
                raise ValueError('Контрольная сумма настроек не совпала')
            rollback = self.paths.data / 'self-contained-rollback.json'
            reject_links(rollback)
            rollback.write_text(json.dumps(dict(settings=str(backup.relative_to(self.paths.root)),
                                                sha256=digest)), encoding='utf-8')
        check_cancel(cancelled)
        self.paths._save_settings(public_sources=sources,
                                  last_project=str(result.destination.relative_to(self.paths.root)),
                                  last_project_relative=True)
        self._record('self_contained', destination=str(result.destination.relative_to(self.paths.root)),
                     sha256=final_hash, source_files=sum(len(b['source']['files']) for b in portable_bindings.values()))
        return MigrationResult(result.destination, result.backup, final_hash, result.backup_hash,
                               result.record_count, result.reused)

    def rollback_self_contained(self) -> None:
        rollback = self.paths.data / 'self-contained-rollback.json'
        reject_links(rollback)
        if rollback.stat().st_size > 64 * 1024:
            raise ValueError('Слишком большой журнал отката')
        record = json.loads(rollback.read_text('utf-8'))
        backup = inside(self.paths.root, record['settings'])
        if self._hash(backup) != record['sha256']:
            raise ValueError('Контрольная сумма настроек для отката не совпала')
        # Preserve any work done since migration before restoring the old locator.
        current = self.paths.last_project()
        if current is not None:
            create_project_backup(self.paths, current)
        descriptor, temporary = tempfile.mkstemp(dir=self.paths.data)
        os.close(descriptor)
        try:
            shutil.copyfile(backup, temporary)
            os.replace(temporary, self.paths.settings_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._record('self_contained_rollback', settings_sha256=record['sha256'])
        record['rolled_back'] = True
        rollback.write_text(json.dumps(record), encoding='utf-8')

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()

    def _record(self, event: str, **values) -> None:
        self.paths.data.mkdir(parents=True, exist_ok=True)
        reject_links(self.journal)
        with self.journal.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps({'time': datetime.now().astimezone().isoformat(),
                                     'event': event, **values}, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _copy_atomic(source: Path, destination: Path) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix='.migration-', suffix='.tmp', dir=destination.parent)
        os.close(descriptor)
        try:
            shutil.copyfile(source, temporary)
            with open(temporary, 'r+b') as stream:
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _backup_path(self, label: str, digest: str) -> Path:
        base = self.paths.backups / f'{label}-{digest[:12]}.sqlite'
        candidate, suffix = base, 2
        while candidate.exists():
            reject_links(candidate)
            if self._hash(candidate) == digest:
                return candidate
            candidate = base.with_name(f'{base.stem}-{suffix}{base.suffix}')
            suffix += 1
        return candidate

    def migrate(self, source_workspace: Path) -> MigrationResult:
        source_workspace = Path(source_workspace).expanduser().absolute()
        reject_links(source_workspace)
        source_db = source_workspace / 'translations.sqlite'
        reject_links(source_db)
        if not source_db.is_file():
            raise ValueError(f'В проекте нет translations.sqlite: {source_workspace}')
        self.paths.initialize()
        migration_id = uuid4().hex
        self._record('started', migration_id=migration_id, source=str(source_workspace))
        with tempfile.TemporaryDirectory(prefix='.migration-', dir=self.paths.projects) as temporary:
            temporary = Path(temporary)
            snapshot = temporary / 'snapshot.sqlite'
            source = sqlite3.connect(f'{source_db.as_uri()}?mode=ro', uri=True)
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            check = sqlite3.connect(f'{snapshot.as_uri()}?mode=ro', uri=True)
            try:
                if check.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise ValueError('Проверка integrity_check не пройдена')
                row = check.execute(
                    "SELECT count(*),sum(target IS NOT NULL AND translated_hash=source_hash "
                    "AND status NOT IN ('INVALID','NEEDS_REVIEW')) FROM records WHERE active=1"
                ).fetchone()
            finally:
                check.close()
            digest = self._hash(snapshot)
            self._record('snapshot_verified', migration_id=migration_id, sha256=digest,
                         rows=row[0])

            existing = []
            for child in self.paths.projects.iterdir():
                if not child.is_dir() or child.name.startswith('.migration-'):
                    continue
                candidate = child / 'translations.sqlite'
                try:
                    reject_links(candidate)
                    if candidate.is_file() and self._hash(candidate) == digest:
                        existing.append(child)
                except OSError:
                    continue
            reused = bool(existing)
            destination = sorted(existing, key=lambda item: item.name.casefold())[0] if reused else None
            backup = self._backup_path(source_workspace.name, digest)
            if not backup.exists():
                self._copy_atomic(snapshot, backup)
            backup_hash = self._hash(backup)
            if backup_hash != digest:
                raise ValueError('Контрольная сумма резервной копии не совпала')
            self._record('backup_verified', migration_id=migration_id,
                         backup=str(backup), sha256=backup_hash)

            if destination is None:
                base = source_workspace.name or 'WuWa Russian'
                destination = self.paths.projects / base
                suffix = 2
                while destination.exists():
                    destination = self.paths.projects / f'{base} ({suffix})'
                    suffix += 1
                staged = temporary / 'project'
                staged.mkdir()
                staged_db = staged / 'translations.sqlite'
                self._copy_atomic(snapshot, staged_db)
                if self._hash(staged_db) != digest:
                    raise ValueError('Контрольная сумма перенесённой базы не совпала')
                catalog_references = []
                catalogs = source_workspace / 'catalogs'
                if catalogs.is_dir():
                    for item in catalogs.iterdir():
                        if item.suffix.casefold() not in ('.jsonl', '.sqlite') or not item.is_file():
                            continue
                        reject_links(item)
                        before = item.stat()
                        catalog_digest = self._hash(item)
                        after = item.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise ValueError(f'Каталог изменился во время переноса: {item.name}')
                        catalog_references.append({'path': str(item.resolve()), 'size': before.st_size,
                                                  'sha256': catalog_digest})
                (staged / 'workspace.json').write_text(json.dumps({
                    'schema_version': 1, 'source_workspace': str(source_workspace),
                    'translations_sha256': digest, 'migration_id': migration_id,
                    'resource_catalogs': catalog_references,
                }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                os.rename(staged, destination)
            final_hash = self._hash(destination / 'translations.sqlite')
            if final_hash != digest:
                raise ValueError('Контрольная сумма итогового проекта не совпала')
            self._record('completed', migration_id=migration_id,
                         destination=str(destination), sha256=final_hash, reused=reused)
            return MigrationResult(destination, backup, final_hash, backup_hash,
                                   row[0], reused)


def connect_public_source(paths: PathManager, workspace: Path, lock: Path,
                          source_root: Path, source_name: str, *, known_roots=(), cancelled=None) -> dict:
    """Reuse the current or best matching project before invoking the existing importer."""
    from .public_corpus import import_corpus, read_lock
    from .translation_db import TranslationDB
    from .manifest import check_cancel

    source = read_lock(lock, source_name)
    namespace = 'public/' + source['repository'].removeprefix('https://github.com/') + '/'
    expected = {namespace + item['path']: item['sha256'] for item in source['files']}
    remembered = paths.public_source(source_name)
    roots = [workspace, *known_roots]
    if remembered:
        roots.append(remembered['workspace'])
    candidates = WorkspaceDiscoveryService(paths, known_roots=roots).discover()
    candidates.sort(key=lambda item: (item.path != Path(workspace).resolve(),
                                     -item.manual_count, -item.translated_count, str(item.path)))
    selected = Path(workspace).absolute()
    for candidate in candidates:
        check_cancel(cancelled)
        db = sqlite3.connect((candidate.path / 'translations.sqlite').as_uri() + '?mode=ro', uri=True)
        try:
            actual = {}
            for name, raw in db.execute('SELECT name,inspection FROM resources'):
                metadata = json.loads(raw)
                if metadata.get('repository') == source['repository']:
                    if metadata.get('revision') != source['revision']:
                        break
                    actual[name] = metadata.get('sha256')
            else:
                if actual == expected and candidate.record_count:
                    selected = candidate.path
                    break
        except (sqlite3.Error, ValueError, AttributeError):
            continue
        finally:
            db.close()
    check_cancel(cancelled)
    # Migrate before opening a legacy database for writes. New empty projects have
    # nothing to snapshot yet; they still get the shared verified source inventory.
    if ((selected / 'translations.sqlite').is_file() and
            (paths.needs_self_containment(selected) or not Path(source_root).is_relative_to(paths.sources))):
        migrated = MigrationService(paths).make_self_contained(selected, bindings={source_name: dict(
            lock=lock, source_root=source_root, workspace=selected)}, cancelled=cancelled)
        selected = migrated.destination
        binding = paths.public_source(source_name)
        lock, source_root = binding['lock'], binding['source_root']
    core = TranslationDB(selected)
    try:
        result = import_corpus(core, lock, source_root, source_name, cancelled=cancelled)
    finally:
        core.close()
    paths.save_public_source(source_name, lock, source_root, selected)
    paths.save_last_project(selected)
    if paths.needs_self_containment(selected):
        migrated = MigrationService(paths).make_self_contained(selected, cancelled=cancelled)
        selected = migrated.destination
        source_root = paths.public_source(source_name)['source_root']
    return dict(result, workspace=str(selected), source_root=str(source_root))


class GameDiscoveryService:
    """Read-only adapter over the existing Steam/registry installation discovery."""

    def __init__(self, paths: PathManager, *, system: bool = True):
        self.paths = paths
        self.system = system

    def discover(self) -> dict:
        from .discovery import discover
        previous = self.paths.game_path()
        result = discover(explicit=[previous] if previous else (), system=self.system)
        if result.installations:
            self.paths.save_game_path(result.installations[0].root)
        return {
            'installations': [
                {'path': str(item.root), 'origins': list(item.origins),
                 'client_version': item.client_version,
                 'steam_build_id': item.steam_build_id,
                 'reader_state': 'NOT_CHECKED'}
                for item in result.installations],
            'warnings': result.warnings,
        }


def create_project_backup(paths: PathManager, workspace: Path) -> dict:
    """Create a verified online SQLite snapshot without copying external resources."""
    workspace = Path(workspace).expanduser().absolute()
    source_path = workspace / 'translations.sqlite'
    reject_links(source_path)
    if not source_path.is_file():
        raise ValueError(f'В проекте нет translations.sqlite: {workspace}')
    paths.initialize()
    staging_dir = tempfile.TemporaryDirectory(prefix='.backup-', dir=paths.backups)
    try:
        snapshot = Path(staging_dir.name) / 'translations.sqlite'
        source = sqlite3.connect(f'{source_path.as_uri()}?mode=ro', uri=True)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        check = sqlite3.connect(f'{snapshot.as_uri()}?mode=ro', uri=True)
        try:
            if check.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ValueError('Резервная копия не прошла integrity_check')
            records = check.execute('SELECT count(*) FROM records WHERE active=1').fetchone()[0]
        finally:
            check.close()
        digest = MigrationService._hash(snapshot)
        backup = paths.backups / f'{workspace.name}-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{digest[:12]}.sqlite'
        if backup.exists():
            reject_links(backup)
            if MigrationService._hash(backup) != digest:
                raise ValueError(f'Путь резервной копии занят другими данными: {backup}')
        else:
            MigrationService._copy_atomic(snapshot, backup)
        if MigrationService._hash(backup) != digest:
            raise ValueError('Контрольная сумма резервной копии не совпала')
        return {'path': backup, 'sha256': digest, 'record_count': records}
    finally:
        staging_dir.cleanup()


def project_files_report(workspace: Path, backup_root: Path | None = None) -> dict:
    """Summarize project-managed files and catalog references; never traverse PAKs."""
    workspace = Path(workspace).expanduser().absolute()
    database = workspace / 'translations.sqlite'
    reject_links(workspace)
    reject_links(database)
    if not database.is_file():
        raise ValueError(f'В проекте нет translations.sqlite: {workspace}')
    files = [database]
    catalogs = workspace / 'catalogs'
    reject_links(catalogs)
    if catalogs.is_dir():
        files.extend(item for item in catalogs.iterdir()
                     if item.is_file() and not item.is_symlink() and item.suffix.casefold() in ('.jsonl', '.sqlite'))
    size = sum(item.stat().st_size for item in files)
    outputs = workspace / 'output'
    exports = sorted((item for item in outputs.rglob('*') if item.is_file() and
                      not item.is_symlink() and item.suffix.casefold() != '.pak'),
                     key=lambda item: item.stat().st_mtime, reverse=True)[:10] if outputs.is_dir() else []
    backups = []
    if backup_root is not None and Path(backup_root).is_dir():
        reject_links(Path(backup_root))
        backups = sorted(Path(backup_root).glob(f'{workspace.name}-*.sqlite'),
                         key=lambda item: item.stat().st_mtime, reverse=True)[:10]
    linked_catalogs = []
    manifest = workspace / 'workspace.json'
    reject_links(manifest)
    if manifest.is_file() and manifest.stat().st_size <= 1024 * 1024:
        try:
            payload = json.loads(manifest.read_text(encoding='utf-8'))
            for reference in payload.get('resource_catalogs', []):
                if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
                    continue
                target = Path(reference['path'])
                if not target.is_absolute():
                    target = inside(workspace, reference['path'])
                if target.suffix.casefold() not in ('.jsonl', '.sqlite') or target.parent.name.casefold() != 'catalogs':
                    continue
                reject_links(target)
                available = target.is_file()
                actual_hash = MigrationService._hash(target) if available else None
                linked_catalogs.append({'path': target, 'available': available,
                                        'verified': actual_hash == reference.get('sha256')})
        except (OSError, ValueError, TypeError, AttributeError):
            linked_catalogs = []
    return {'workspace': workspace, 'database': database, 'database_bytes': database.stat().st_size,
            'catalog_count': len(files) - 1, 'catalog_bytes': size - database.stat().st_size,
            'managed_bytes': size, 'exports': exports, 'backups': backups,
            'linked_catalogs': linked_catalogs}
