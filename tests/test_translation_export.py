import sqlite3
import threading
from unittest.mock import patch

from tests.test_discovery import FixtureCase
from tests.test_sqlite_source import database
from wuwaru.translation_db import TranslationDB
from wuwaru.engine import TranslationEngine
from wuwaru.providers import MockProvider
from wuwaru.export import export_databases, verify_copy
from wuwaru.manifest import Cancelled


class ExportTests(FixtureCase):
    def prepare(self):
        path = database(self.home / 'lang.db')
        before = path.read_bytes()
        core = TranslationDB(self.home / 'work')
        self.addCleanup(core.close)
        core.import_source('lang.db', path)
        TranslationEngine(core, MockProvider(), interval=0).run('test')
        return core, path, before

    def test_export_preserves_schema_typed_ids_extra_and_source(self):
        core, path, before = self.prepare()
        result = export_databases(core, 'ru-test', allow_machine=True)
        output = core.workspace / 'ru-test/lang.db'
        with sqlite3.connect(output) as db:
            rows = db.execute('SELECT typeof(Id), Id, Content, Extra FROM Text').fetchall()
            self.assertEqual({(r[0], r[1], r[3]) for r in rows},
                             {('integer', 1, 7), ('text', '1', 7), ('integer', 2, 7)})
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        db.close()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result['rows'], 3)
        self.assertFalse(result['installation_allowed'])
        with self.assertRaises(FileExistsError):
            export_databases(core, 'ru-test', allow_machine=True)

    def test_missing_invalid_stale_and_unreviewed_block_publication(self):
        core, path, before = self.prepare()
        with self.assertRaises(ValueError):
            export_databases(core, 'review-required')
        core.set_manual(1, 'Привет', approved=True)
        core.set_manual(2, 'Сломано')
        with self.assertRaises(ValueError):
            export_databases(core, 'invalid', allow_machine=True)
        self.assertFalse((core.workspace / 'invalid').exists())
        with sqlite3.connect(path) as db:
            db.execute('UPDATE Text SET Extra=42')
        db.close()
        with self.assertRaises(ValueError):
            export_databases(core, 'stale', allow_machine=True)
        self.assertFalse((core.workspace / 'stale').exists())

    def test_cancel_publish_failure_and_traversal_leave_no_output(self):
        core, _, _ = self.prepare()
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            export_databases(core, 'cancel', allow_machine=True, cancelled=event)
        with patch('wuwaru.export.os.rename', side_effect=OSError('publish failed')):
            with self.assertRaises(OSError):
                export_databases(core, 'failed', allow_machine=True)
        self.assertFalse((core.workspace / 'failed').exists())
        self.assertFalse(list(core.workspace.glob('.export-*')))
        for name in ('../outside', 'C:/game', 'foo:bar', 'translations.sqlite/x'):
            with self.assertRaises((ValueError, OSError)):
                export_databases(core, name, allow_machine=True)

    def test_verifier_detects_id_and_noncontent_corruption(self):
        core, source, _ = self.prepare()
        export_databases(core, 'copy', allow_machine=True)
        target = core.workspace / 'copy/lang.db'
        for sql in ('UPDATE Text SET Extra=42', "UPDATE Text SET Id='changed' WHERE typeof(Id)='integer'"):
            with sqlite3.connect(target) as db:
                db.execute(sql)
            db.close()
            with self.assertRaises(ValueError):
                verify_copy(source, target, None)
