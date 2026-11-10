import sqlite3
import shutil
from contextlib import closing

from tests.pak_fixture import pak
from tests.test_discovery import FixtureCase
from wuwaru.core_contracts import ResourceSourceType, VersionIdentity
from wuwaru.extraction import inspect_catalog_databases
from wuwaru.export import export_databases
from wuwaru.pak_builder import build_package
from wuwaru.pak_reader import V12ArchiveReader, read_databases
from wuwaru.sqlite_source import ReadOnlySQLiteSourceAdapter
from wuwaru.translation_db import TranslationDB


class FixtureCatalog:
    def __init__(self, archives):
        self.archives = tuple(archives)

    def discover_resources(self):
        return self.archives

    def build_manifest(self):
        return {'schema_version': 1, 'inventory_fingerprint_sha256': 'fixture',
                'resource_version': None, 'inventory_complete': False}

    def identify_version(self):
        return VersionIdentity(None, None, 'UNKNOWN')

    def resolve_resource_priority(self, resources):
        return tuple(resources)


class ArchiveSourceTests(FixtureCase):
    def test_catalog_archive_sqlite_is_validated_and_origin_is_typed(self):
        source = self.home / 'source.db'
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE strings (Id INTEGER PRIMARY KEY, Content TEXT NOT NULL)')
            db.execute('ALTER TABLE strings ADD COLUMN Extra INTEGER NOT NULL DEFAULT 17')
            db.execute('INSERT INTO strings(Id, Content) VALUES (7, ?)', ('Hello {Player}',))
        db.close()
        archive = pak(self.home / 'localized.pak',
                      {'lang_en.db': source.read_bytes()})
        results = inspect_catalog_databases(
            FixtureCatalog([archive]), V12ArchiveReader(), ReadOnlySQLiteSourceAdapter(),
            self.home / 'stage', source_type=ResourceSourceType.SYNTHETIC_FIXTURE)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.source_type, ResourceSourceType.SYNTHETIC_FIXTURE)
        self.assertEqual(result.validation.rows, 1)
        self.assertEqual(result.entry.path, 'Client/Content/Aki/ConfigDB/en/lang_en.db')
        self.assertEqual(result.verification.sha256, result.validation.sha256)
        core = TranslationDB(self.home / 'core')
        self.addCleanup(core.close)
        core.import_source(result.entry.path, result.verification.destination,
                           source_type=result.source_type,
                           source_metadata=result.source_metadata)
        self.assertEqual(core.db.execute(
            'SELECT DISTINCT source_type FROM records').fetchone()[0], 'SYNTHETIC_FIXTURE')
        core.set_manual(core.db.execute('SELECT seq FROM records').fetchone()[0],
                        'Привет {Player}', approved=True)
        exported = export_databases(core, 'ru-export')
        self.assertEqual(exported['qa']['blocked_rows'], 0)
        package_source = self.home / 'pak-source/Client/Content/Aki/ConfigDB/en'
        package_source.mkdir(parents=True)
        shutil.copy2(self.home / 'core/ru-export/Client/Content/Aki/ConfigDB/en/lang_en.db',
                     package_source / 'lang_en.db')
        package = build_package(package_source.parents[4], self.home / 'built-package')
        archive = self.home / 'built-package/synthetic.pak'
        independently_read = read_databases(archive)
        self.assertEqual(len(independently_read), 1)
        self.assertEqual(package['installation_allowed'], False)
        with closing(sqlite3.connect(
                self.home / 'built-package/verified/Client/Content/Aki/ConfigDB/en/lang_en.db')) as db:
            self.assertEqual(db.execute('SELECT Id,Content,Extra FROM strings').fetchall(),
                             [(7, 'Привет {Player}', 17)])

    def test_public_textmap_cannot_be_relabelled_as_archive_sqlite(self):
        archive = pak(self.home / 'asset.pak', {'asset.bytes': b'not text'})
        with self.assertRaisesRegex(ValueError, 'PUBLIC_TEXTMAP'):
            inspect_catalog_databases(
                FixtureCatalog([archive]), V12ArchiveReader(), ReadOnlySQLiteSourceAdapter(),
                self.home / 'stage', source_type=ResourceSourceType.PUBLIC_TEXTMAP)
