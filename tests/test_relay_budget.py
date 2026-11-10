"""Offline regressions for explicit, bounded RelayModels spending."""
from unittest.mock import patch
import sqlite3
import threading
from contextlib import closing

from tests.test_discovery import FixtureCase
from wuwaru.pilot import prepare_synthetic, preview_pilot, run_pilot
from wuwaru.provider_config import ProviderProfile
from wuwaru.providers import MockProvider, OpenAICompatibleProvider, ProviderError
from wuwaru.manifest import Cancelled
from wuwaru.translation_db import TranslationDB, MIGRATIONS, APPLICATION_ID
from wuwaru.billing import pricing


class RelayBudgetTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.core = prepare_synthetic(self.home)
        self.addCleanup(self.core.close)
        self.profile = ProviderProfile('RelayModels')
        self.limits = dict(max_requests=1, max_rows=5, max_output_tokens=512)

    def preview(self):
        return preview_pilot(self.core, profile=self.profile, **self.limits)

    def test_unknown_money_stays_null_and_request_body_is_bounded(self):
        report = self.preview()
        self.assertEqual(report['records'], 5)
        self.assertEqual(len(report['sample']), 5)
        self.assertEqual(report['pricing']['status'], 'UNKNOWN')
        self.assertIsNone(report['pricing']['input'])
        self.assertIsNone(report['pricing']['output'])
        self.assertIsNone(report['preview']['estimated_cost'])
        self.assertIsNone(report['preview']['estimated_cost_with_retries'])
        self.assertEqual(report['limits']['max_requests'], 1)
        self.assertTrue(report['requests'])
        self.assertTrue(all(r['body']['max_tokens'] == 512 for r in report['requests']))

    def test_provider_pricing_never_uses_relay_multiplier_as_usd(self):
        self.profile.input_price, self.profile.output_price = 100, 500
        relay = pricing(self.profile)
        self.assertEqual(relay['status'], 'UNKNOWN')
        self.assertEqual(relay['coefficient'], 0.38)
        self.assertIsNone(relay['input'])
        self.assertIsNone(relay['output'])
        self.assertIsNone(relay['currency'])
        official = pricing(ProviderProfile('OpenAI', input_price=999, output_price=999))
        self.assertEqual((official['input'], official['output']), (0.1, 0.5))
        self.assertTrue(official['status'].startswith('CONFIRMED_USD'))
        self.assertEqual(pricing(ProviderProfile('OpenAI', model='unknown'))['status'], 'UNKNOWN')
        custom = ProviderProfile('Custom OpenAI-compatible', base_url='https://custom.invalid/v1')
        self.assertEqual(pricing(custom)['status'], 'UNKNOWN')
        custom.input_price, custom.output_price = 1, 2
        self.assertEqual(pricing(custom)['status'], 'USER_SUPPLIED')

    def test_unknown_price_requires_confirmation_then_runs_only_authored_rows(self):
        report = self.preview()
        with patch.object(OpenAICompatibleProvider, 'translate') as transport:
            with self.assertRaises(PermissionError):
                run_pilot(self.core, expected=report['preview_hash'],
                          profile=self.profile, **self.limits)
            transport.assert_not_called()
        calls = []

        def translate(provider, items, *, allow_network=False):
            self.assertTrue(allow_network)
            self.assertLessEqual(len(items), 5)
            self.assertEqual(provider.max_output_tokens, 512)
            calls.append(items)
            return MockProvider().translate(items)

        with patch('socket.socket', side_effect=AssertionError('external network forbidden')), \
                patch.object(OpenAICompatibleProvider, 'translate', new=translate):
            run_pilot(self.core, confirmed=True, expected=report['preview_hash'],
                      profile=self.profile, **self.limits)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.core.db.execute('SELECT count(*) FROM usage').fetchone()[0], 1)
        self.assertEqual(self.core.db.execute(
            "SELECT count(*) FROM records WHERE target IS NOT NULL").fetchone()[0], 5)

    def test_retryable_error_cannot_exceed_total_request_limit(self):
        report = self.preview()
        with patch('socket.socket', side_effect=AssertionError('external network forbidden')), \
                patch.object(OpenAICompatibleProvider, 'translate',
                             side_effect=ProviderError('synthetic timeout', retryable=True)) as transport:
            with self.assertRaises((ProviderError, ValueError)):
                run_pilot(self.core, confirmed=True, expected=report['preview_hash'],
                          profile=self.profile, **self.limits)
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.core.db.execute('SELECT count(*) FROM usage').fetchone()[0], 1)

    def test_changed_preview_limits_need_fresh_confirmation(self):
        report = self.preview()
        with patch.object(OpenAICompatibleProvider, 'translate') as transport:
            with self.assertRaises(ValueError):
                run_pilot(self.core, confirmed=True, expected=report['preview_hash'],
                          profile=self.profile, max_requests=1, max_rows=4,
                          max_output_tokens=512)
            transport.assert_not_called()

    def test_pause_resume_counts_prior_attempts_and_preserves_finished_rows(self):
        limits = dict(max_requests=1, max_rows=30, max_output_tokens=512)
        def run(**extra):
            report = preview_pilot(self.core, profile=self.profile, **limits)
            return run_pilot(self.core, confirmed=True, expected=report['preview_hash'],
                             profile=self.profile, **limits, **extra)
        def translate(provider, items, *, allow_network=False):
            return MockProvider().translate(items)
        with patch.object(OpenAICompatibleProvider, 'translate', autospec=True, side_effect=translate) as transport:
            self.assertEqual(run()['state'], 'PAUSED')
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(self.core.db.execute('SELECT count(*) FROM records WHERE target IS NOT NULL').fetchone()[0], 25)
            before = [tuple(r) for r in self.core.db.execute('SELECT seq,target FROM records WHERE target IS NOT NULL ORDER BY seq')]
            self.assertEqual(run()['state'], 'PAUSED')
            self.assertEqual(transport.call_count, 1)
            limits['max_requests'] = 2
            self.assertEqual(run()['state'], 'DONE')
            self.assertEqual(transport.call_count, 2)
            self.assertEqual(self.core.db.execute('SELECT count(*) FROM records WHERE target IS NOT NULL').fetchone()[0], 30)
            self.assertEqual(before, [tuple(r) for r in self.core.db.execute('SELECT seq,target FROM records WHERE seq<=25 ORDER BY seq')])
        self.assertTrue(all(r[0] is None for r in self.core.db.execute('SELECT estimated_cost FROM usage')))

    def test_cancellation_after_checkpoint_resumes_without_repeating_completed_batch(self):
        limits = dict(max_requests=2, max_rows=30, max_output_tokens=512)
        event = threading.Event()
        def run(**extra):
            report = preview_pilot(self.core, profile=self.profile, **limits)
            return run_pilot(self.core, confirmed=True, expected=report['preview_hash'],
                             profile=self.profile, **limits, **extra)
        def translate(provider, items, *, allow_network=False):
            return MockProvider().translate(items)
        with patch.object(OpenAICompatibleProvider, 'translate', autospec=True, side_effect=translate) as transport:
            with self.assertRaises(Cancelled):
                run(cancelled=event, after_batch=lambda _: event.set())
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(self.core.db.execute('SELECT processed FROM runs').fetchone()[0], 25)
            self.assertEqual(run()['state'], 'DONE')
            self.assertEqual(transport.call_count, 2)
            self.assertEqual(len(transport.call_args.args[1]), 5)

    def test_usage_migration_retains_previous_attempt_and_accepts_unknown_estimate(self):
        folder = self.home / 'legacy'
        folder.mkdir()
        with closing(sqlite3.connect(folder / 'translations.sqlite')) as db, db:
            for migration in MIGRATIONS[:-1]:
                for statement in migration:
                    db.execute(statement)
            db.execute(f'PRAGMA application_id={APPLICATION_ID}')
            db.execute(f'PRAGMA user_version={len(MIGRATIONS)-1}')
            db.execute("INSERT INTO usage(attempt,run_id,provider,estimated_cost,state,actual_usage_json) "
                       "VALUES (7,'old','test-provider',0.125,'RECEIVED','{\"prompt_tokens\":12}')")
        upgraded = TranslationDB(folder)
        self.addCleanup(upgraded.close)
        old = upgraded.db.execute('SELECT * FROM usage WHERE attempt=7').fetchone()
        self.assertEqual(old['estimated_cost'], 0.125)
        self.assertEqual(old['actual_usage_json'], '{"prompt_tokens":12}')
        self.assertEqual(old['state'], 'RECEIVED')
        with upgraded.db:
            upgraded.db.execute("INSERT INTO usage(run_id,provider,estimated_cost) VALUES ('new','relay',NULL)")
        self.assertEqual(upgraded.db.execute('SELECT count(*) FROM usage').fetchone()[0], 2)
