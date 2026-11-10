import threading
from unittest.mock import patch

from tests.test_discovery import FixtureCase
from wuwaru.translation_db import TranslationDB
from wuwaru.providers import MockProvider
from wuwaru.engine import TranslationEngine
from wuwaru.manifest import Cancelled
from wuwaru.pilot import prepare_synthetic, preview_pilot, run_pilot, select_public


class PilotTests(FixtureCase):
    def test_preview_gate_resume_and_cache_use_existing_engine(self):
        core = prepare_synthetic(self.home)
        self.addCleanup(core.close)
        with patch('socket.socket', side_effect=AssertionError('network')):
            report = preview_pilot(core)
            self.assertEqual(report['records'], 200)
            self.assertEqual(report['preview']['requests'], 8)
            self.assertTrue(all(row['context'] for row in report['sample']))
            self.assertTrue(any(row['glossary'] for row in report['sample']))
            with self.assertRaises(PermissionError):
                run_pilot(core)
            event = threading.Event()
            with self.assertRaises(Cancelled):
                TranslationEngine(core, MockProvider(), interval=0).run(
                    'offline', batch_size=25, cancelled=event, after_batch=lambda _: event.set())
            provider = MockProvider()
            TranslationEngine(core, provider, interval=0).run('offline', batch_size=25)
            self.assertEqual(provider.calls, 7)
            self.assertEqual(core.db.execute('SELECT count(*) FROM cache').fetchone()[0], 200)
            self.assertEqual(preview_pilot(core)['preview']['requests'], 0)

    def test_public_selection_balances_categories_and_never_allows_network(self):
        core = prepare_synthetic(self.home)
        self.addCleanup(core.close)
        with core.db:
            core.db.execute("UPDATE records SET source_type='PUBLIC_TEXTMAP'")
        rows = select_public(core)
        self.assertEqual(len(rows), 200)
        self.assertEqual(len({r['seq'] for r in rows}), 200)
        with patch('socket.socket', side_effect=AssertionError('network')):
            with self.assertRaises(PermissionError):
                run_pilot(core, confirmed=True)

    def test_modified_synthetic_source_or_context_is_not_transmitted(self):
        core = prepare_synthetic(self.home)
        self.addCleanup(core.close)
        with core.db:
            core.db.execute("UPDATE records SET context='external text' WHERE seq=1")
        with self.assertRaises(PermissionError):
            run_pilot(core, confirmed=True)
