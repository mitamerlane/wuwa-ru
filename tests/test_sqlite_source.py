import sqlite3
import threading

from tests.test_discovery import FixtureCase
from wuwaru.sqlite_source import inspect_database
from wuwaru.manifest import Cancelled


def database(path, rows=((1, 'Hello'), ('1', 'Привет\n{Player}'), (2, ''))):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE "Text" (Id, Content TEXT, Extra INTEGER)')
        db.executemany('INSERT INTO Text VALUES (?, ?, 7)', rows)
    db.close()
    return path


class SQLiteTests(FixtureCase):
    def test_typed_ids_unicode_empty_and_read_only(self):
        path = database(self.home / 'lang_test.db')
        before = path.read_bytes()
        result = inspect_database(path)
        self.assertEqual(result['tables'][0]['rows'], 3)
        self.assertEqual(result, inspect_database(path, independent=True))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ['lang_test.db'])

    def test_duplicate_null_blob_id_and_nontext_content_fail(self):
        for rows in (((1, 'a'), (1, 'b')), ((None, 'a'),), ((b'id', 'a'),),
                     ((1.5, 'a'),), ((1, None),), ((1, b'bad'),)):
            with self.subTest(rows=rows):
                path = self.home / 'bad.db'
                database(path, rows)
                with self.assertRaises(ValueError):
                    inspect_database(path)
                path.unlink()

    def test_schema_views_corruption_and_empty_database_fail(self):
        for sql in ('CREATE TABLE Other (value TEXT)',
                    'CREATE TABLE Text (Id, Content); CREATE VIEW Evil AS SELECT * FROM Text',
                    'CREATE TABLE Text (Id, Content)'):
            path = self.home / 'bad.db'
            with sqlite3.connect(path) as db:
                db.executescript(sql)
            db.close()
            with self.assertRaises(ValueError):
                inspect_database(path)
            path.unlink()
        path.write_bytes(b'not sqlite')
        with self.assertRaises(ValueError):
            inspect_database(path)

    def test_quoted_table_and_wal_sidecar(self):
        path = self.home / 'quoted.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE "A""B" (Id INTEGER, Content TEXT)')
            db.execute('INSERT INTO "A""B" VALUES (1, ?)', ('Unicode 雪',))
        db.close()
        self.assertEqual(inspect_database(path)['tables'][0]['name'], 'A"B')
        path.with_name(path.name + '-wal').write_bytes(b'pending')
        with self.assertRaises(ValueError):
            inspect_database(path)

    def test_limits_and_cancel(self):
        path = database(self.home / 'lang.db')
        with self.assertRaises(ValueError):
            inspect_database(path, max_rows=1)
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            inspect_database(path, cancelled=event)
