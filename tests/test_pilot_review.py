import json
from tests.test_discovery import FixtureCase
from wuwaru.pilot import prepare_synthetic, pilot_run_id, pilot_results, approve_pilot_result
from wuwaru.provider_config import ProviderProfile
from wuwaru.editor import Editor, Conflict


class PilotReviewTests(FixtureCase):
    def test_saved_response_usage_approval_and_manual_conflict_without_network(self):
        core = prepare_synthetic(self.home)
        self.addCleanup(core.close)
        profile = ProviderProfile('RelayModels', 'https://api.relaymodels.com/v1', 'gpt-6-luna')
        identity = profile.provider(max_output_tokens=1024).identity
        run_id = pilot_run_id(profile, max_rows=5, max_output_tokens=1024)
        before = core.record(1)
        target = before['source'].replace('Open the lantern menu for station', 'Открой меню фонарей станции').replace('Press', 'Нажми').replace('to confirm.', 'для подтверждения.')
        with core.db:
            core.db.execute("INSERT INTO runs(id,signature,state,processed) VALUES (?, 'fixture', 'DONE', 5)", (run_id,))
            core.db.execute("INSERT INTO usage(run_id,provider,estimated_cost,state,actual_usage_json) VALUES (?,?,NULL,'RECEIVED',?)",
                (run_id, identity, json.dumps(dict(model='gpt-6-luna', input_tokens=563, output_tokens=338))))
            core.db.execute("UPDATE records SET target=?,translated_hash=source_hash,status='MACHINE_TRANSLATED',provider=? WHERE seq=1", (target, identity))
            core.audit(1, 'AUTOMATIC', before)
        report = pilot_results(core, profile=profile, max_rows=5, max_output_tokens=1024)
        self.assertEqual(len(report['rows']), 1)
        self.assertEqual(report['usage'][0]['actual_usage']['input_tokens'], 563)
        row = report['rows'][0]
        self.assertEqual(row['model_target'], target)
        approve_pilot_result(core, seq=1, expected=row['version'], revision=row['revision'])
        self.assertEqual(core.record(1)['status'], 'APPROVED')
        self.assertEqual(core.record(1)['manual'], 1)
        with self.assertRaises(Conflict):
            approve_pilot_result(core, seq=1, expected=row['version'], revision=row['revision'])
        edited = Editor(core)
        edited.save(1, target + ' Ручная правка.', edited.version(1))
        again = pilot_results(core, profile=profile, max_rows=5, max_output_tokens=1024)['rows'][0]
        self.assertEqual(again['model_target'], target)
        self.assertTrue(again['manual'])
        self.assertNotEqual(again['target'], target)
        other = ProviderProfile('OpenAI', 'https://api.openai.com/v1', 'gpt-6-luna')
        self.assertEqual(pilot_results(core, profile=other, max_rows=5, max_output_tokens=1024)['rows'], [])
        with core.db:
            before = core.record(2)
            core.db.execute("UPDATE records SET target='Ошибка без плейсхолдера',translated_hash=source_hash,status='INVALID',provider=? WHERE seq=2", (identity,))
            core.audit(2, 'AUTOMATIC', before)
        bad = pilot_results(core, profile=profile, max_rows=5, max_output_tokens=1024)['rows'][1]
        self.assertFalse(bad['can_approve'])
        self.assertTrue(any('ERROR' in q for q in bad['qa']))
        with self.assertRaises(ValueError):
            approve_pilot_result(core, seq=2, expected=bad['version'], revision=bad['revision'])
        with core.db:
            before = core.record(3)
            core.db.execute("UPDATE records SET target='Mock',provider='mock-v1' WHERE seq=3")
            core.audit(3, 'AUTOMATIC', before)
        self.assertEqual(len(pilot_results(core, profile=profile, max_rows=5, max_output_tokens=1024)['rows']), 2)
