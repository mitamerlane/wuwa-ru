import json
import os
from unittest.mock import patch

from tests.test_discovery import FixtureCase
from tests.test_providers import FakeOpener
from wuwaru.provider_config import ProviderProfile, ProviderSettings, WindowsCredentials
from wuwaru.portable import PathManager
from wuwaru.providers import TranslationItem, ProviderError, OpenAICompatibleProvider


class ProviderConfigTests(FixtureCase):
    def test_settings_roundtrip_is_secret_free_and_prices_are_separate(self):
        paths = PathManager(self.home / 'portable')
        paths.initialize()
        settings = ProviderSettings(paths)
        relay = settings.profile('RelayModels')
        self.assertEqual(relay.base_url, 'https://api.relaymodels.com/v1')
        self.assertEqual(relay.model, 'gpt-6-luna')
        self.assertIsNone(relay.input_price)
        relay.input_price, relay.output_price = 1.0, 2.0
        settings.save(relay)
        loaded = ProviderSettings(paths)
        self.assertEqual(loaded.selected, 'RelayModels')
        self.assertEqual(loaded.profile('RelayModels').output_price, 2.0)
        self.assertIsNone(loaded.profile('OpenAI').input_price)
        self.assertNotIn('api_key', paths.settings_path.read_text('utf-8'))

    def test_key_is_bound_to_provider_and_endpoint(self):
        openai = ProviderProfile('OpenAI')
        relay = ProviderProfile('RelayModels')
        custom = ProviderProfile('Custom OpenAI-compatible', base_url=relay.base_url)
        self.assertEqual(len({p.credential_target for p in (openai, relay, custom)}), 3)
        before = custom.credential_target
        custom.base_url = 'https://other.invalid/v1'
        self.assertNotEqual(before, custom.credential_target)
        with patch.dict(os.environ, {'WUWARU_API_KEY': 'synthetic-openai'}):
            with patch.object(WindowsCredentials, 'get', return_value=None):
                self.assertIsNone(relay.key())
                self.assertEqual(openai.key(), 'synthetic-openai')

    def test_relay_request_and_models_never_send_reasoning(self):
        profile = ProviderProfile('RelayModels')
        provider = profile.provider()
        body = provider.request_body([TranslationItem('a', 'Hello {Player}', '', {})])
        self.assertNotIn('reasoning_effort', body)
        self.assertNotIn('store', body)
        self.assertEqual(body['max_tokens'], 8192)
        self.assertEqual(body['response_format']['type'], 'json_schema')
        generic = OpenAICompatibleProvider('https://custom.invalid/v1', 'gpt-6-luna')
        self.assertNotIn('reasoning_effort', generic.request_body([]))
        opener = FakeOpener(json.dumps({'data': [{'id': 'gpt-6-luna'}, {'id': 'another'}]}).encode())
        with patch.object(WindowsCredentials, 'get', return_value='synthetic-relay'), patch(
                'urllib.request.build_opener', return_value=opener):
            self.assertEqual(provider.list_models(allow_network=True), ['another', 'gpt-6-luna'])
        self.assertEqual(opener.request.get_method(), 'GET')
        self.assertEqual(opener.request.full_url, profile.base_url + '/models')
        self.assertIsNone(opener.request.data)
        with self.assertRaises(PermissionError):
            provider.list_models()

    def test_invalid_configuration_stays_offline(self):
        for url in ('http://host/v1', 'https://user:password@host/v1', 'https://host/v1?key=x'):
            with self.assertRaises(ValueError):
                ProviderProfile('Custom OpenAI-compatible', base_url=url).validate()
        profile = ProviderProfile('RelayModels')
        profile.model = ''
        with self.assertRaises(ValueError):
            profile.validate()

    def test_prices_follow_model_and_relay_unknown_requires_confirmation(self):
        from wuwaru.pilot import prepare_synthetic, preview_pilot, run_pilot
        paths = PathManager(self.home / 'portable')
        paths.initialize()
        settings = ProviderSettings(paths)
        profile = settings.profile('RelayModels')
        profile.input_price, profile.output_price = 1, 2
        settings.save(profile)
        other = settings.profile('RelayModels', model='another-model')
        self.assertIsNone(other.input_price)
        other.input_price, other.output_price = 3, 4
        settings.save(other)
        self.assertEqual(settings.profile('RelayModels', model='gpt-6-luna').input_price, 1)
        custom = ProviderProfile('Custom OpenAI-compatible', base_url='https://first.invalid/v1',
                                 input_price=1, output_price=2)
        settings.save(custom)
        self.assertIsNone(settings.profile(custom.name, model=custom.model,
                                          base_url='https://second.invalid/v1').input_price)
        core = prepare_synthetic(self.home)
        self.addCleanup(core.close)
        with patch('socket.socket', side_effect=AssertionError('network')):
            unknown = ProviderProfile('RelayModels')
            report = preview_pilot(core, profile=unknown)
            self.assertIsNone(report['preview']['estimated_cost'])
            self.assertEqual(report['pricing']['status'], 'UNKNOWN')
            with patch.object(OpenAICompatibleProvider, 'translate') as transport:
                with self.assertRaises(PermissionError):
                    run_pilot(core, confirmed=False, expected=report['preview_hash'], profile=unknown)
                transport.assert_not_called()
            self.assertIsNone(preview_pilot(core, profile=profile)['preview']['estimated_cost'])

    def test_remote_key_echo_never_reaches_diagnostics_or_model_list(self):
        provider = ProviderProfile('RelayModels').provider()
        opener = FakeOpener(b'{"data":[{"id":"synthetic-secret"}]}')
        with patch.object(WindowsCredentials, 'get', return_value='synthetic-secret'), patch(
                'urllib.request.build_opener', return_value=opener):
            with self.assertRaises(ProviderError) as error:
                provider.list_models(allow_network=True)
        self.assertNotIn('synthetic-secret', str(error.exception))

    def test_native_vault_roundtrip_and_delete(self):
        # App-specific synthetic credential; never touch an existing provider credential.
        vault = WindowsCredentials()
        target = 'WuWaRussian/test/' + self.home.name
        try:
            vault.set(target, 'synthetic-not-a-secret')
            self.assertEqual(vault.get(target), 'synthetic-not-a-secret')
        finally:
            vault.delete(target)
        self.assertIsNone(vault.get(target))
