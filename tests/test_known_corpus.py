import hashlib
import json
import shutil
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from tests.test_discovery import FixtureCase
from wuwaru.portable import PathManager
from wuwaru.translation_db import TranslationDB
from wuwaru.public_corpus import import_corpus
from wuwaru.editor import Editor


class KnownCorpusTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.paths = PathManager(self.home / 'portable')
        self.paths.initialize()
        self.source = self.paths.imports / 'Arikatsu'
        file = self.source / 'Textmaps/en/Test.json'
        file.parent.mkdir(parents=True)
        file.write_text('{"confirm":"Confirm"}', encoding='utf-8')
        self.lock = self.paths.imports / 'public-text-sources.json'
        self.lock.write_text(json.dumps(dict(schema_version=1, sources={'Arikatsu': dict(
            repository='https://github.com/example/data', revision='a'*40,
            data_version='3.7', data_date=None, files=[dict(path='Textmaps/en/Test.json',
            size=file.stat().st_size, sha256=hashlib.sha256(file.read_bytes()).hexdigest())])})), encoding='utf-8')

    def test_reuses_imported_project_and_manual_translation_then_remembers_after_move(self):
        from wuwaru.portable import connect_public_source
        project = self.paths.projects / 'Translated'
        with closing(TranslationDB(project)) as core:
            import_corpus(core, self.lock, self.source, 'Arikatsu')
            editor = Editor(core)
            editor.save(1, 'Подтвердить', editor.version(1))
        current = self.paths.projects / 'Empty'
        result = connect_public_source(self.paths, current, self.lock, self.source, 'Arikatsu')
        self.assertEqual(result['workspace'], str(project))
        self.assertEqual(result['new'], 0)
        with closing(TranslationDB(project)) as core:
            self.assertEqual(core.db.execute('SELECT count(*) FROM records').fetchone()[0], 1)
            self.assertEqual(core.record(1)['target'], 'Подтвердить')
            self.assertEqual(core.record(1)['manual'], 1)
        self.assertFalse((current / 'translations.sqlite').exists())
        moved = self.home / 'Moved'
        shutil.move(self.paths.root, moved)
        remembered = PathManager(moved).public_source('Arikatsu')
        self.assertTrue(remembered['source_root'].is_relative_to(moved / 'Data/Sources'))
        self.assertEqual(remembered['workspace'], moved / 'Data/Projects/Translated')

    def test_corrupt_source_fails_before_import_and_is_not_remembered(self):
        from wuwaru.portable import connect_public_source
        (self.source / 'Textmaps/en/Test.json').write_text('{}', encoding='utf-8')
        project = self.paths.projects / 'Empty'
        with self.assertRaises(ValueError):
            connect_public_source(self.paths, project, self.lock, self.source, 'Arikatsu')
        self.assertIsNone(self.paths.public_source('Arikatsu'))
        with closing(TranslationDB(project)) as core:
            self.assertEqual(core.db.execute('SELECT count(*) FROM records').fetchone()[0], 0)

    def test_first_connection_imports_once_and_repeated_connection_is_unchanged(self):
        from wuwaru.portable import connect_public_source
        project = self.paths.projects / 'New'
        first = connect_public_source(self.paths, project, self.lock, self.source, 'Arikatsu')
        second = connect_public_source(self.paths, project, self.lock, self.source, 'Arikatsu')
        self.assertEqual(first['new'], 1)
        self.assertEqual(second['new'], 0)
        self.assertEqual(second['unchanged'], 1)
        self.assertEqual(self.paths.last_project(), project)

    def test_self_contained_migration_move_without_external_sources_and_rollback(self):
        from wuwaru.portable import MigrationService, connect_public_source
        from wuwaru.glossary import Glossary
        legacy = self.home / 'legacy'
        with closing(TranslationDB(legacy)) as core:
            import_corpus(core, self.lock, self.source, 'Arikatsu')
            editor = Editor(core)
            editor.save(1, 'Подтвердить', editor.version(1), status='APPROVED')
            Glossary(core).add('Confirm', 'Подтвердить', category='other', verified=True)
            before = {table: core.db.execute(f'SELECT * FROM {table}').fetchall()
                      for table in ('records', 'history', 'glossary')}
        self.paths.save_last_project(legacy)
        self.paths.save_public_source('Arikatsu', self.lock, self.source, legacy)
        old_settings = self.paths.settings_path.read_bytes()
        result = MigrationService(self.paths).make_self_contained(legacy)
        self.assertEqual(result.record_count, 1)
        self.assertEqual(self.paths.last_project(), result.destination)
        self.assertTrue(self.paths.public_source('Arikatsu')['source_root'].is_relative_to(self.paths.sources))
        moved = self.home / 'Другой каталог' / 'WuWaRussian'
        moved.parent.mkdir()
        shutil.move(self.paths.root, moved)
        legacy.rename(self.home / 'legacy-unavailable')
        paths = PathManager(moved)
        # Imports is no longer needed after migrating the pinned inventory.
        (moved / 'Imports').rename(moved / 'Imports-unavailable')
        binding = paths.public_source('Arikatsu')
        connect_public_source(paths, paths.last_project(), **{k: binding[k] for k in ('lock', 'source_root')}, source_name='Arikatsu')
        with closing(TranslationDB(paths.last_project())) as core:
            for table, rows in before.items():
                self.assertEqual([tuple(row) for row in core.db.execute(f'SELECT * FROM {table}')],
                                 [tuple(row) for row in rows])
            self.assertFalse(Path(core.db.execute('SELECT path FROM resources').fetchone()[0]).is_absolute())
        MigrationService(paths).rollback_self_contained()
        self.assertEqual(paths.settings_path.read_bytes(), old_settings)
        self.assertFalse(paths.needs_self_containment(paths.last_project()))
        self.assertTrue((paths.projects / result.destination.name / 'translations.sqlite').is_file())

    def test_self_contained_bad_corpus_keeps_original_settings_and_retries(self):
        from wuwaru.portable import MigrationService
        legacy = self.home / 'legacy'
        with closing(TranslationDB(legacy)) as core:
            import_corpus(core, self.lock, self.source, 'Arikatsu')
        self.paths.save_last_project(legacy)
        self.paths.save_public_source('Arikatsu', self.lock, self.source, legacy)
        before = self.paths.settings_path.read_bytes()
        file = self.source / 'Textmaps/en/Test.json'
        original = file.read_bytes()
        file.write_bytes(b'{}')
        with self.assertRaises(ValueError):
            MigrationService(self.paths).make_self_contained(legacy)
        self.assertEqual(self.paths.settings_path.read_bytes(), before)
        file.write_bytes(original)
        result = MigrationService(self.paths).make_self_contained(legacy)
        again = MigrationService(self.paths).make_self_contained(result.destination)
        self.assertEqual(again.destination, result.destination)

    def test_self_contained_copy_interruption_keeps_settings_and_resumes(self):
        from wuwaru.portable import MigrationService
        legacy = self.home / 'legacy'
        with closing(TranslationDB(legacy)) as core:
            import_corpus(core, self.lock, self.source, 'Arikatsu')
        self.paths.save_last_project(legacy)
        self.paths.save_public_source('Arikatsu', self.lock, self.source, legacy)
        before = self.paths.settings_path.read_bytes()
        with patch.object(MigrationService, '_copy_atomic', side_effect=OSError('interrupted')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                MigrationService(self.paths).make_self_contained(legacy)
        self.assertEqual(self.paths.settings_path.read_bytes(), before)
        migrated = MigrationService(self.paths).make_self_contained(legacy)
        self.assertEqual(migrated.record_count, 1)
        rollback = json.loads((self.paths.data / 'self-contained-rollback.json').read_text('utf-8'))
        (self.paths.root / rollback['settings']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'сумма'):
            MigrationService(self.paths).rollback_self_contained()
        self.assertEqual(self.paths.last_project(), migrated.destination)

    def test_self_contained_rejects_pak_catalog_reference(self):
        from wuwaru.portable import MigrationService
        project = self.paths.projects / 'project'
        with closing(TranslationDB(project)):
            pass
        pak = self.home / 'catalogs/game.pak'
        pak.parent.mkdir()
        pak.write_bytes(b'never copy this')
        (project / 'workspace.json').write_text(json.dumps(dict(resource_catalogs=[dict(
            path=str(pak), sha256=hashlib.sha256(pak.read_bytes()).hexdigest())])), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'каталог'):
            MigrationService(self.paths).make_self_contained(project)
        self.assertFalse((project / 'catalogs/game.pak').exists())
