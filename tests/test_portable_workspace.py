import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wuwaru.portable import (GameDiscoveryService, MigrationService, PathManager,
                              WorkspaceDiscoveryService, create_project_backup,
                              project_files_report)


class PortableWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.test-tmp')
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def make_project(self, parent, name, rows=0):
        project = parent / name
        project.mkdir(parents=True)
        db = sqlite3.connect(project / 'translations.sqlite')
        try:
            db.execute('CREATE TABLE records (active INTEGER, target TEXT, translated_hash TEXT, source_hash TEXT, status TEXT)')
            db.executemany('INSERT INTO records VALUES (?, ?, ?, ?, ?)',
                           [(1, f'ru-{i}', 'same', 'same', 'APPROVED') for i in range(rows)])
            db.commit()
        finally:
            db.close()
        return project

    def test_path_manager_uses_application_root_and_creates_unified_layout(self):
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        self.assertEqual(paths.projects, self.root / 'WuWaRussian' / 'Data' / 'Projects')
        self.assertTrue(all((self.root / 'WuWaRussian' / name).is_dir()
                            for name in ('Data', 'Imports', 'Exports', 'Backups', 'Logs')))

    def test_native_folder_is_created_and_native_kinds_default_to_checked_waves(self):
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        self.assertEqual(paths.native, self.root / 'WuWaRussian' / 'Data' / 'Native')
        self.assertTrue(paths.native.is_dir())
        self.assertEqual(paths.native_kinds(), ('speaker', 'ui'))
        paths.save_native_kinds(('speaker', 'ui', 'item'))
        self.assertEqual(json.loads(paths.settings_path.read_text('utf-8'))['native_kinds'],
                         ['speaker', 'ui', 'item'])
        self.assertEqual(paths.native_kinds(), ('speaker', 'ui', 'item'))
        paths._save_settings(native_kinds='ui')
        self.assertEqual(paths.native_kinds(), ('speaker', 'ui'))
        paths._save_settings(native_kinds=['ui', 7])
        self.assertEqual(paths.native_kinds(), ('speaker', 'ui'))
        paths.save_native_kinds([])
        self.assertEqual(paths.native_kinds(), ('speaker', 'ui'))
        paths._save_settings(native_kinds=['ui', 'bogus', 'item', 'ui'])
        self.assertEqual(paths.native_kinds(), ('ui', 'item'))
        paths._save_settings(native_kinds=['bogus', 'scene'])
        self.assertEqual(paths.native_kinds(), ('speaker', 'ui'))

    def test_path_manager_exposes_write_failure_for_explicit_user_choice(self):
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        with patch('wuwaru.portable.tempfile.NamedTemporaryFile', side_effect=PermissionError('read-only')):
            self.assertFalse(paths.is_writable())
        self.assertEqual(paths.root, self.root / 'WuWaRussian')

    def test_discovery_reads_last_project_then_bounded_known_roots_and_counts_rows(self):
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        recent = self.make_project(self.root / 'legacy', 'Последний проект', 2)
        known = self.make_project(self.root / 'legacy', 'Другой', 4)
        paths.save_last_project(recent)
        results = WorkspaceDiscoveryService(paths, known_roots=[recent.parent]).discover()
        self.assertEqual([item.path for item in results], [recent, known])
        self.assertEqual(results[0].record_count, 2)
        self.assertEqual(results[0].translated_count, 2)
        self.assertEqual(results[0].name, 'Последний проект')

    def test_discovery_rejects_corrupt_database_and_does_not_recurse_unboundedly(self):
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        corrupt = self.root / 'legacy' / 'broken'
        corrupt.mkdir(parents=True)
        (corrupt / 'translations.sqlite').write_bytes(b'not sqlite')
        hidden = self.root / 'legacy' / 'deep' / 'not-scanned'
        hidden.mkdir(parents=True)
        db = sqlite3.connect(hidden / 'translations.sqlite')
        try:
            db.execute('CREATE TABLE records (active INTEGER, target TEXT, translated_hash TEXT, source_hash TEXT, status TEXT)')
        finally:
            db.close()
        results = WorkspaceDiscoveryService(paths, known_roots=[self.root / 'legacy']).discover()
        self.assertEqual(results, [])

    def test_settings_round_trip_is_json_and_relative_to_portable_root(self):
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        project = self.root / 'Переводы' / 'Проект'
        paths.save_last_project(project)
        payload = json.loads(paths.settings_path.read_text('utf-8'))
        self.assertEqual(payload['last_project'], str(project.absolute()))
        self.assertEqual(paths.last_project(), project.absolute())

    def test_last_portable_project_survives_folder_move(self):
        old = self.root / 'portable-old'
        paths = PathManager(old)
        paths.initialize()
        project = paths.projects / 'project'
        project.mkdir()
        paths.save_last_project(project)
        moved = self.root / 'Диск' / 'WuWaRussian'
        moved.parent.mkdir()
        shutil.move(str(old), str(moved))
        self.assertEqual(PathManager(moved).last_project(), moved / 'Data' / 'Projects' / 'project')

    def test_migration_preserves_approved_translations_and_skips_large_files(self):
        source = self.make_project(self.root / 'legacy', 'Старый проект', 2)
        db = sqlite3.connect(source / 'translations.sqlite')
        try:
            db.execute("UPDATE records SET target='Утверждено',status='APPROVED' WHERE active=1")
            db.commit()
        finally:
            db.close()
        (source / 'game.pak').write_bytes(b'large game resource marker')
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        result = MigrationService(paths).migrate(source)
        self.assertEqual(result.destination, paths.projects / 'Старый проект')
        self.assertTrue(result.destination_hash)
        self.assertEqual(result.destination_hash, result.backup_hash)
        self.assertFalse((result.destination / 'game.pak').exists())
        self.assertTrue((result.destination / 'workspace.json').is_file())
        db = sqlite3.connect(result.destination / 'translations.sqlite')
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM records WHERE target="Утверждено"').fetchone()[0], 2)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            db.close()

    def test_migration_is_idempotent_for_same_snapshot_and_never_overwrites(self):
        source = self.make_project(self.root / 'legacy', 'Project', 1)
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        service = MigrationService(paths)
        first = service.migrate(source)
        second = service.migrate(source)
        self.assertEqual(second.destination, first.destination)
        self.assertTrue(second.reused)
        changed = self.make_project(self.root / 'other', 'Project', 3)
        third = service.migrate(changed)
        self.assertNotEqual(third.destination, first.destination)
        self.assertTrue(first.destination.joinpath('translations.sqlite').is_file())

    def test_interrupted_migration_can_be_retried_without_touching_source(self):
        source = self.make_project(self.root / 'legacy', 'Retry', 2)
        paths = PathManager(self.root / 'WuWaRussian')
        service = MigrationService(paths)
        with patch.object(MigrationService, '_copy_atomic', side_effect=OSError('synthetic interruption')):
            with self.assertRaisesRegex(OSError, 'interruption'):
                service.migrate(source)
        self.assertTrue((source / 'translations.sqlite').is_file())
        result = service.migrate(source)
        self.assertEqual(result.record_count, 2)
        events = [json.loads(line)['event'] for line in service.journal.read_text('utf-8').splitlines()]
        self.assertIn('started', events)
        self.assertEqual(events[-1], 'completed')

    def test_atomic_migration_copy_refuses_existing_destination(self):
        source = self.root / 'source.bin'
        destination = self.root / 'destination.bin'
        source.write_bytes(b'new')
        destination.write_bytes(b'approved old data')
        with self.assertRaises(FileExistsError):
            MigrationService._copy_atomic(source, destination)
        self.assertEqual(destination.read_bytes(), b'approved old data')

    def test_game_discovery_is_read_only_and_keeps_unknown_version_unknown(self):
        from tests.test_discovery import fixture
        paths = PathManager(self.root / 'WuWaRussian')
        paths.initialize()
        game = fixture(self.root / 'Игра')
        paths.save_game_path(game)
        before = {item.relative_to(game): item.read_bytes() for item in game.rglob('*') if item.is_file()}
        paths.save_last_project(self.root / 'workspace')
        result = GameDiscoveryService(paths, system=False).discover()
        after = {item.relative_to(game): item.read_bytes() for item in game.rglob('*') if item.is_file()}
        self.assertEqual(result['installations'][0]['path'], str(game.resolve()))
        self.assertIsNone(result['installations'][0]['client_version'])
        self.assertEqual(result['installations'][0]['reader_state'], 'NOT_CHECKED')
        self.assertEqual(before, after)
        self.assertEqual(paths.last_project(), (self.root / 'workspace').absolute())
        self.assertEqual(paths.game_path(), game.resolve())

    def test_backup_is_verified_and_files_report_excludes_pak_bytes(self):
        workspace = self.make_project(self.root, 'Файлы', 2)
        catalog = workspace / 'catalogs'
        catalog.mkdir()
        (catalog / 'resources.jsonl').write_text('{}\n', encoding='utf-8')
        (workspace / 'game.pak').write_bytes(b'do not include in managed project copies')
        paths = PathManager(self.root / 'WuWaRussian')
        result = create_project_backup(paths, workspace)
        report = project_files_report(workspace, paths.backups)
        self.assertTrue(result['path'].is_file())
        self.assertEqual(result['record_count'], 2)
        self.assertEqual(report['catalog_count'], 1)
        self.assertEqual(report['backups'], [result['path']])
        self.assertEqual(report['managed_bytes'], report['database_bytes'] + report['catalog_bytes'])

    def test_migration_links_catalogs_without_copy_and_reports_checksum(self):
        source = self.make_project(self.root / 'legacy', 'Catalog project', 1)
        catalog_dir = source / 'catalogs'
        catalog_dir.mkdir()
        catalog = catalog_dir / 'resources.sqlite'
        catalog.write_bytes(b'diagnostic resource catalog')
        paths = PathManager(self.root / 'WuWaRussian')
        result = MigrationService(paths).migrate(source)
        manifest = json.loads((result.destination / 'workspace.json').read_text('utf-8'))
        self.assertEqual(manifest['resource_catalogs'][0]['path'], str(catalog.resolve()))
        self.assertFalse((result.destination / 'catalogs').exists())
        report = project_files_report(result.destination, paths.backups)
        self.assertTrue(report['linked_catalogs'][0]['verified'])
        self.assertEqual(report['catalog_count'], 0)


if __name__ == '__main__':
    unittest.main()
