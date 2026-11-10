import json
import threading
from unittest.mock import patch

from tests.test_discovery import FixtureCase
from wuwaru.translation_db import TranslationDB
from tests.test_sqlite_source import database
from wuwaru.engine import TranslationEngine, Budget
from wuwaru.glossary import Glossary
from wuwaru.manifest import Cancelled
from wuwaru.providers import MockProvider, ProviderError, OpenAICompatibleProvider


class EngineTests(FixtureCase):
    def test_selection_never_translates_unselected_records_and_pins_resume(self):
        core = self.prepare(((1, 'One'), (2, 'Two'), (3, 'Three')))
        TranslationEngine(core, MockProvider(), selection=[1, 3], interval=0).run('pilot')
        self.assertIsNone(core.record(2)['target'])
        self.assertIsNotNone(core.record(3)['target'])
        with self.assertRaises(ValueError):
            TranslationEngine(core, MockProvider(), selection=[2]).run('pilot')

    def open_core(self):
        core = TranslationDB(self.home / 'work')
        self.addCleanup(core.close)
        return core

    def prepare(self, rows=((1, 'Hello {Player}'), (2, 'Hello {Player}'), (3, 'World'))):
        core = self.open_core()
        core.import_source('lang.db', database(self.home / 'source.db', rows))
        return core

    def test_offline_cache_and_manual_priority(self):
        core = self.prepare()
        core.set_manual(3, 'Мир', approved=True)
        provider = MockProvider()
        with patch('socket.socket', side_effect=AssertionError('network')):
            result = TranslationEngine(core, provider).run('first', batch_size=1)
        self.assertEqual(result['state'], 'DONE')
        self.assertEqual(provider.calls, 1)
        self.assertEqual(core.record(3)['target'], 'Мир')
        self.assertEqual(core.record(3)['status'], 'APPROVED')
        self.assertEqual(core.db.execute('SELECT count(*) FROM cache').fetchone()[0], 1)

    def test_failure_retry_and_resume_after_committed_batch(self):
        core = self.prepare(((1, 'One'), (2, 'Two'), (3, 'Three')))
        class Broken(MockProvider):
            def translate(self, items):
                if self.calls == 1:
                    raise ProviderError('temporary', retryable=True)
                return super().translate(items)
        provider = Broken()
        with self.assertRaises(ProviderError):
            TranslationEngine(core, provider, attempts=2, sleep=lambda _: None).run('resume', batch_size=1)
        self.assertEqual(core.db.execute('SELECT cursor FROM runs').fetchone()[0], 1)
        self.assertIsNone(core.record(2)['target'])
        good = MockProvider()
        TranslationEngine(core, good).run('resume', batch_size=1)
        self.assertEqual(good.calls, 2)
        self.assertEqual(core.db.execute('SELECT count(*) FROM usage').fetchone()[0], 5)

    def test_cancelled_response_is_not_committed(self):
        core = self.prepare()
        event = threading.Event()
        class Cancel(MockProvider):
            def translate(self, items):
                result = super().translate(items)
                event.set()
                return result
        with self.assertRaises(Cancelled):
            TranslationEngine(core, Cancel()).run('cancel', cancelled=event)
        self.assertEqual(core.db.execute('SELECT cursor FROM runs').fetchone()[0], 0)
        self.assertIsNone(core.record(1)['target'])
        TranslationEngine(core, MockProvider()).run('cancel')
        self.assertIsNotNone(core.record(1)['target'])

    def test_bad_id_batch_rolls_back_and_bad_tokens_are_invalid(self):
        core = self.prepare()
        class Wrong(MockProvider):
            def translate(self, items):
                return {'wrong-id': 'Привет'}
        with self.assertRaises(ProviderError):
            TranslationEngine(core, Wrong()).run('wrong')
        self.assertIsNone(core.record(1)['target'])
        class Damage(MockProvider):
            def translate(self, items):
                return {item.key: 'Сломано' for item in items}
        TranslationEngine(core, Damage()).run('damaged')
        self.assertEqual(core.record(1)['status'], 'INVALID')

    def test_lone_surrogate_is_invalid_without_poisoning_sqlite(self):
        core = self.prepare(((1, 'Hello'),))
        class Corrupt(MockProvider):
            def translate(self, items):
                return {item.key: 'Привет\ud800' for item in items}
        TranslationEngine(core, Corrupt()).run('unicode')
        self.assertEqual(core.record(1)['status'], 'INVALID')
        self.assertIsNone(core.record(1)['target'])
        self.assertEqual(core.db.execute('SELECT count(*) FROM cache').fetchone()[0], 0)

    def test_repeated_word_filler_never_reaches_provider(self):
        # A developer filler like "Test Test …" made the model repeat itself past the timeout on every attempt.
        filler = ' '.join(['Test'] * 130)
        core = self.prepare(((1, filler), (2, 'Hello'), (3, 'test TEST Test test test test test test')))
        sent = []
        class Recording(MockProvider):
            def translate(self, items):
                sent.extend(item.source for item in items)
                return super().translate(items)
        engine = TranslationEngine(core, Recording(), interval=0)
        self.assertEqual(engine.preview(batch_size=10)['requests'], 1)
        engine.run('filler', batch_size=10)
        self.assertEqual(sent, ['Hello'])
        for seq, source in ((1, filler), (3, 'test TEST Test test test test test test')):
            record = core.record(seq)
            self.assertEqual((record['status'], record['target'], record['provider']), ('INVALID', source, 'placeholder'))
        self.assertEqual(core.db.execute('SELECT count(*) FROM cache').fetchone()[0], 1)

    def test_short_repetition_is_still_translated(self):
        core = self.prepare(((1, 'Ha ha ha ha!'), (2, 'No, no, no, no, no, no, no, no.')))
        provider = MockProvider()
        TranslationEngine(core, provider, interval=0).run('laugh', batch_size=10)
        self.assertEqual(provider.calls, 1)
        self.assertNotEqual(core.record(1)['provider'], 'placeholder')
        self.assertNotEqual(core.record(2)['provider'], 'placeholder')

    def test_oversized_string_stops_before_provider(self):
        core = self.prepare(((1, 'a' * (128 * 1024 + 1)),))
        provider = MockProvider()
        with self.assertRaises(ValueError):
            TranslationEngine(core, provider).run('oversized')
        self.assertEqual(provider.calls, 0)

    def test_glossary_versions_context_and_cache_invalidation(self):
        core = self.prepare(((1, 'Rover'),))
        glossary = Glossary(core)
        glossary.add('Rover', 'Скиталец', category='character', verified=True)
        glossary.add('Rover', 'Ровер', context='lang.db/Text', category='character', verified=True)
        glossary.add('Rover', 'Не проверено', context='lang.db/Text', category='character')
        provider = MockProvider()
        TranslationEngine(core, provider).run('glossary')
        self.assertEqual(core.record(1)['target'], 'Ровер')
        self.assertEqual(provider.calls, 0)
        core.set_manual(1, 'Мой скиталец', approved=True)
        glossary.add('Rover', 'Другое', category='character', verified=True)
        TranslationEngine(core, provider).run('manual')
        self.assertEqual(core.record(1)['target'], 'Мой скиталец')
        self.assertEqual(glossary.version, 4)

    def test_estimate_budget_gate_and_rate_limiting(self):
        core = self.prepare(((1, 'One'), (2, 'Two')))
        class Paid(MockProvider):
            requires_permission = True
        provider = Paid()
        engine = TranslationEngine(core, provider, attempts=2)
        preview = engine.preview(batch_size=1, input_price=1, output_price=2)
        self.assertEqual(preview['requests'], 2)
        self.assertEqual(preview['max_attempts'], 4)
        with self.assertRaises(PermissionError):
            engine.run('paid')
        self.assertEqual(provider.calls, 0)
        with self.assertRaises(ValueError):
            engine.run('paid', budget=Budget(allowed=True, max_requests=1, max_cost=0,
                                            input_price=1, output_price=2), batch_size=1)
        self.assertEqual(provider.calls, 0)
        waits = []
        engine = TranslationEngine(core, provider, interval=0.2, sleep=waits.append, clock=lambda: 0)
        engine.run('allowed', budget=Budget(allowed=True, max_requests=2, max_cost=1,
                                          input_price=1, output_price=2), batch_size=1)
        self.assertEqual(provider.calls, 2)
        self.assertTrue(any(wait >= 0.2 for wait in waits))

    def test_resume_refuses_changed_source_or_glossary(self):
        core = self.prepare()
        TranslationEngine(core, MockProvider()).run('done')
        core.import_source('lang.db', self.home / 'source.db')
        self.assertEqual(TranslationEngine(core, MockProvider()).run('done')['state'], 'DONE')
        Glossary(core).add('World', 'Мир', category='location', verified=True)
        with self.assertRaises(ValueError):
            TranslationEngine(core, MockProvider()).run('done')

    def test_compatible_api_contract_without_network(self):
        provider = OpenAICompatibleProvider('https://example.invalid/v1', 'test-model')
        core = self.prepare(((1, 'Hello'),))
        engine = TranslationEngine(core, provider)
        self.assertEqual(engine.preview()['requests'], 1)
        with self.assertRaises(PermissionError):
            engine.run('network-forbidden')
        self.assertEqual(core.db.execute('SELECT count(*) FROM usage').fetchone()[0], 0)

    def test_restart_process_equivalent_and_cache_context_isolation(self):
        core = self.prepare(((1, 'Hello'), (2, 'World')))
        def interrupt(_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            TranslationEngine(core, MockProvider(), interval=0).run('restart', batch_size=1,
                                                                  after_batch=interrupt)
        core.close()
        reopened = self.open_core()
        provider = MockProvider()
        TranslationEngine(reopened, provider, interval=0).run('restart', batch_size=1)
        self.assertEqual(provider.calls, 1)
        reopened.import_source('other.db', database(self.home / 'other.db', ((1, 'Hello'),)))
        TranslationEngine(reopened, provider, interval=0).run('other')
        self.assertEqual(provider.calls, 2)

    def test_retry_success_and_manual_edit_during_response(self):
        core = self.prepare(((1, 'Hello'),))
        class Retry(MockProvider):
            tries = 0
            def translate(self, items):
                self.tries += 1
                if self.tries == 1:
                    raise ProviderError('retry', retryable=True, retry_after=2)
                core.set_manual(1, 'Ручной перевод', approved=True)
                return super().translate(items)
        waits = []
        provider = Retry()
        TranslationEngine(core, provider, sleep=waits.append).run('retry')
        self.assertGreaterEqual(sum(waits), 2)
        self.assertEqual(provider.tries, 2)
        self.assertEqual(core.record(1)['target'], 'Ручной перевод')
        self.assertEqual(core.record(1)['status'], 'APPROVED')

    def test_changed_original_retranslates_automatic_but_preserves_manual(self):
        core = self.prepare(((1, 'Hello'), (2, 'World')))
        provider = MockProvider()
        engine = TranslationEngine(core, provider, interval=0)
        engine.run('v1')
        core.set_manual(2, 'Мир', approved=True)
        import sqlite3
        with sqlite3.connect(self.home / 'source.db') as db:
            db.execute("UPDATE Text SET Content='Changed ' || Id")
        db.close()
        core.import_source('lang.db', self.home / 'source.db')
        engine.run('v2')
        self.assertEqual(provider.calls, 2)
        self.assertEqual(core.record(1)['status'], 'MACHINE_TRANSLATED')
        self.assertEqual(core.record(2)['target'], 'Мир')
        self.assertEqual(core.record(2)['status'], 'NEEDS_REVIEW')

    def test_fatal_failure_not_retried_and_snapshot_change_blocks_batch(self):
        core = self.prepare(((1, 'Hello'),))
        class Fatal(MockProvider):
            def translate(self, items):
                self.calls += 1
                raise ProviderError('fatal')
        provider = Fatal()
        with self.assertRaises(ProviderError):
            TranslationEngine(core, provider).run('fatal')
        self.assertEqual(provider.calls, 1)
        class Changed(MockProvider):
            def translate(self, items):
                Glossary(core).add('Hello', 'Привет', category='other', verified=True)
                return super().translate(items)
        with self.assertRaises(ValueError):
            TranslationEngine(core, Changed()).run('changed')
        self.assertIsNone(core.record(1)['target'])

    def test_glossary_hint_and_cache_version(self):
        core = self.prepare(((1, 'Meet Rover'),))
        glossary = Glossary(core)
        glossary.add('Rover', 'Скиталец', category='character', verified=True)
        version = glossary.version
        self.assertEqual(glossary.hints('Meet Rover', 'lang.db/Text'), {'Rover': 'Скиталец'})
        self.assertEqual(glossary.hints("Rover's bag", 'lang.db/Text'), {"Rover's": 'Скиталец'})
        self.assertEqual(glossary.hints("The Rovers' bags", 'lang.db/Text'), {"Rovers'": 'Скиталец'})
        self.assertEqual(glossary.hints('Two Rovers', 'lang.db/Text'), {'Rovers': 'Скиталец'})
        glossary.add('Mt. Firmament', 'гора Твердь', category='location', verified=True)
        glossary.add('I.R.I.S.', 'И.Р.И.С.', category='character', verified=True)
        self.assertEqual(glossary.hints('Time flows on Mt. Firmament, says I.R.I.S.', 'lang.db/Text'),
                         {'Mt. Firmament': 'гора Твердь', 'I.R.I.S.': 'И.Р.И.С.'})
        provider = MockProvider()
        engine = TranslationEngine(core, provider, interval=0)
        engine.run('before')
        # A new row in the SAME context can reuse cache until glossary revision changes.
        import sqlite3
        with sqlite3.connect(self.home / 'source.db') as db:
            db.execute("INSERT INTO Text VALUES (2,'Meet Rover',7)")
        db.close()
        core.import_source('lang.db', self.home / 'source.db')
        engine.run('reuse')
        self.assertEqual(provider.calls, 1)
        glossary.add('Rover', 'Странник', category='character', verified=True)
        self.assertEqual(Glossary(core, version=version).exact('Rover', ''), 'Скиталец')
        with sqlite3.connect(self.home / 'source.db') as db:
            db.execute("INSERT INTO Text VALUES (3,'Meet Rover',7)")
        db.close()
        core.import_source('lang.db', self.home / 'source.db')
        engine.run('new-version')
        self.assertEqual(provider.calls, 2)

    def test_explicit_context_invalidates_translation_and_separates_cache(self):
        core = self.prepare(((1, 'Hello'), (2, 'Hello')))
        core.set_context(1, 'speaker:Yangyang')
        core.set_context(2, 'speaker:Rover')
        provider = MockProvider()
        engine = TranslationEngine(core, provider, interval=0)
        engine.run('contexts', batch_size=1)
        self.assertEqual(provider.calls, 2)
        core.set_manual(1, 'Привет', approved=True)
        core.set_context(1, 'speaker:other')
        self.assertEqual(core.record(1)['status'], 'NEEDS_REVIEW')
        self.assertEqual(core.record(1)['target'], 'Привет')
        self.assertEqual(core.record(1)['manual'], 1)
        with self.assertRaises(ValueError):
            engine.run('contexts')
        core.import_source('lang.db', self.home / 'source.db')
        self.assertEqual(core.record(1)['context'], 'speaker:other')

    def test_database_failure_rolls_back_entire_batch_and_checkpoint(self):
        core = self.prepare(((1, 'Hello'), (2, 'World')))
        audit = core.audit
        def fail_second(seq, event, before):
            if seq == 2:
                raise OSError('synthetic disk failure')
            audit(seq, event, before)
        with patch.object(core, 'audit', side_effect=fail_second):
            with self.assertRaises(OSError):
                TranslationEngine(core, MockProvider()).run('atomic')
        self.assertIsNone(core.record(1)['target'])
        self.assertIsNone(core.record(2)['target'])
        self.assertEqual(core.db.execute('SELECT cursor FROM runs').fetchone()[0], 0)
        self.assertEqual(core.db.execute('SELECT count(*) FROM cache').fetchone()[0], 0)
        self.assertEqual(core.db.execute('SELECT count(*) FROM history').fetchone()[0], 2)
        TranslationEngine(core, MockProvider()).run('atomic')
        self.assertEqual(core.db.execute('SELECT count(*) FROM history').fetchone()[0], 4)
