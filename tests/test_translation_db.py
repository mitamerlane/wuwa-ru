import sqlite3

from tests.test_discovery import FixtureCase, fixture
from tests.test_sqlite_source import database
from wuwaru.translation_db import TranslationDB, MIGRATIONS, APPLICATION_ID
from wuwaru.core_contracts import ResourceSourceType
from wuwaru.validation import validate


class TranslationDatabaseTests(FixtureCase):
    def open_core(self):
        core = TranslationDB(self.home / 'work')
        self.addCleanup(core.close)
        return core

    def test_reimport_typed_ids_history_and_manual_change(self):
        source = database(self.home / 'lang.db')
        core = self.open_core()
        self.assertEqual(core.import_source('lang.db', source)['new'], 3)
        rows = core.db.execute('SELECT * FROM records ORDER BY seq').fetchall()
        self.assertEqual({(r['id_type'], r['id_value']) for r in rows},
                         {('integer', '1'), ('text', '1'), ('integer', '2')})
        key = next(r['seq'] for r in rows if r['id_type'] == 'integer' and r['id_value'] == '1')
        core.set_manual(key, 'Привет', approved=True)
        history = core.db.execute('SELECT count(*) FROM history').fetchone()[0]
        self.assertEqual(core.import_source('lang.db', source)['unchanged'], 3)
        self.assertEqual(core.db.execute('SELECT count(*) FROM history').fetchone()[0], history)
        with sqlite3.connect(source) as db:
            db.execute("UPDATE Text SET Content='Hello again' WHERE typeof(Id)='integer' AND Id=1")
            db.execute("DELETE FROM Text WHERE typeof(Id)='text'")
            db.execute("INSERT INTO Text VALUES (3, 'New', 8)")
        db.close()
        self.assertEqual(core.import_source('lang.db', source),
                         dict(new=1, changed=1, unchanged=1, removed=1))
        row = core.record(key)
        self.assertEqual((row['target'], row['manual'], row['status']), ('Привет', 1, 'NEEDS_REVIEW'))
        self.assertNotEqual(row['source_hash'], row['translated_hash'])
        core.set_manual(key, 'Снова привет', approved=True)
        self.assertEqual(core.record(key)['status'], 'APPROVED')

    def test_explicit_resource_provenance_is_preserved_and_public_is_separate(self):
        source = database(self.home / 'lang.db')
        core = self.open_core()
        core.import_source('real/lang.db', source,
                           source_type=ResourceSourceType.REAL_LOCAL_RESOURCE,
                           source_metadata={'archive': 'fixture.pak', 'entry_path': 'lang_en.db',
                                            'archive_index_sha256': 'a' * 64})
        self.assertEqual(core.db.execute(
            "SELECT DISTINCT source_type FROM records WHERE resource='real/lang.db'"
        ).fetchone()[0], 'REAL_LOCAL_RESOURCE')
        metadata = core.db.execute(
            "SELECT source_metadata FROM records WHERE resource='real/lang.db' LIMIT 1"
        ).fetchone()[0]
        self.assertIn('fixture.pak', metadata)
        with self.assertRaisesRegex(ValueError, 'PUBLIC_TEXTMAP'):
            core.import_source('public/lang.db', source,
                               source_type=ResourceSourceType.PUBLIC_TEXTMAP)

    def test_migrations_reopen_and_future_schema(self):
        core = self.open_core()
        self.assertEqual(core.db.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS))
        core.db.execute('PRAGMA user_version=999')
        core.close()
        with self.assertRaises(ValueError):
            TranslationDB(self.home / 'work')

    def test_upgrade_v1_preserves_rows_and_rollback_on_import_failure(self):
        workspace = self.home / 'work'
        workspace.mkdir()
        with sqlite3.connect(workspace / 'translations.sqlite') as db:
            for statement in MIGRATIONS[0]:
                db.execute(statement)
            db.execute(f'PRAGMA application_id={APPLICATION_ID}')
            db.execute('PRAGMA user_version=1')
            db.execute("INSERT INTO records(resource,table_name,id_type,id_value,context,source,"
                       "source_hash,generation) VALUES ('r.db','T','integer','1','r.db/T','Hello','hash',1)")
        db.close()
        core = self.open_core()
        self.assertEqual(core.record(1)['source'], 'Hello')
        self.assertEqual(core.db.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS))
        self.assertEqual(core.db.execute('SELECT count(*) FROM glossary').fetchone()[0], 0)

    def test_import_cancellation_rolls_back_changes_and_manual_history_survives_removal(self):
        from unittest.mock import patch
        from wuwaru.manifest import Cancelled
        core = self.open_core()
        source = database(self.home / 'source.db', ((1, 'Hello'), (2, 'World')))
        core.import_source('r.db', source)
        core.set_manual(1, 'Привет', approved=True)
        with sqlite3.connect(source) as db:
            db.execute('DELETE FROM Text WHERE Id=1')
            db.execute("UPDATE Text SET Content='Updated' WHERE Id=2")
        db.close()
        with patch.object(core, 'audit', side_effect=Cancelled('cancel')):
            with self.assertRaises(Cancelled):
                core.import_source('r.db', source)
        self.assertEqual(core.record(2)['source'], 'World')
        self.assertEqual(core.record(1)['active'], 1)
        core.import_source('r.db', source)
        self.assertEqual(core.record(1)['active'], 0)
        self.assertEqual(core.record(1)['target'], 'Привет')
        self.assertGreater(core.db.execute('SELECT count(*) FROM history WHERE seq=1').fetchone()[0], 1)

    def test_import_is_atomic_and_rejects_game_workspace(self):
        source = database(self.home / 'lang.db')
        core = self.open_core()
        core.import_source('lang.db', source)
        with sqlite3.connect(source) as db:
            db.execute("INSERT INTO Text VALUES (1, 'duplicate', 0)")
        db.close()
        with self.assertRaises(ValueError):
            core.import_source('lang.db', source)
        self.assertEqual(core.db.execute('SELECT count(*) FROM records').fetchone()[0], 3)
        game = fixture(self.home / 'game')
        with self.assertRaises(ValueError):
            TranslationDB(game / 'work')

    def test_validator_blocks_damage_and_allows_service_only_strings(self):
        self.assertEqual(validate('Hi <b>{Player}</b> %s $count\n',
                                  'Привет <b>{Player}</b> %s $count\n'), [])
        for target in ('Привет', 'Hi <b>{Player}</b> %s $count\n',
                       'Привет <b>{Other}</b> %s $count\n', 'Привет\ufffd', 'Привет\u202e'):
            self.assertTrue(validate('Hi <b>{Player}</b> %s $count\n', target))
        self.assertEqual(validate('', ''), [])
        self.assertEqual(validate('{Player}', '{Player}'), [])
        self.assertTrue(validate('Hello', ''))
        self.assertTrue(validate('Hello', 'Привет' * 200))
        for token in ('{0}', '{{name}}', '${amount}', '$(value)', '%PLAYER%', '%1$s', '%03d',
                      '<color=#fff>', '</color>', '<sprite name="coin"/>', '\\n', '\r\n'):
            with self.subTest(token=token):
                self.assertEqual(validate('Hello ' + token, 'Привет ' + token), [])
                self.assertTrue(validate('Hello ' + token, 'Привет'))
        self.assertTrue(validate('Hello', 'Привет\x00'))
        self.assertTrue(validate('Hello', 'Привет\ud800'))

    def test_invalid_manual_edit_is_preserved_but_not_approved(self):
        core = self.open_core()
        core.import_source('lang.db', database(self.home / 'lang.db'))
        seq = core.db.execute("SELECT seq FROM records WHERE source LIKE '%Player%'").fetchone()[0]
        with self.assertRaises(ValueError):
            core.set_manual(seq, 'Сломано', approved=True)
        self.assertIsNone(core.record(seq)['target'])
        core.set_manual(seq, 'Сломано')
        self.assertEqual(core.record(seq)['status'], 'INVALID')

    def test_resource_path_separators_share_one_stable_identity(self):
        core = self.open_core()
        source = database(self.home / 'lang.db')
        core.import_source('folder\\lang.db', source)
        self.assertEqual(core.import_source('folder/lang.db', source)['unchanged'], 3)
        self.assertEqual(core.db.execute('SELECT count(*) FROM resources').fetchone()[0], 1)
        self.assertEqual(core.db.execute('SELECT count(*) FROM records').fetchone()[0], 3)
