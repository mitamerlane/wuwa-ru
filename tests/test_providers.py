import io
import json
import os
import urllib.error
from unittest.mock import patch

from tests.test_discovery import FixtureCase
from wuwaru.providers import OpenAICompatibleProvider, TranslationItem, ProviderError


class FakeResponse(io.BytesIO):
    pass


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def open(self, request, timeout):
        self.calls += 1
        self.request = request
        if self.error:
            raise self.error
        return FakeResponse(self.response)


class ProviderTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.provider = OpenAICompatibleProvider('https://example.invalid/v1', 'test-model',
                                                  key_env='WUWARU_SYNTHETIC_TEST_KEY')
        self.items = [TranslationItem('key', 'Hello {Player}', 'dialogue', {'Hello': 'Привет'})]

    def call(self, opener):
        with patch.dict(os.environ, {'WUWARU_SYNTHETIC_TEST_KEY': 'synthetic-not-a-secret'}), \
                patch('urllib.request.build_opener', return_value=opener):
            return self.provider.translate(self.items, allow_network=True)

    def envelope(self, content, reason='stop'):
        return json.dumps({'choices': [{'finish_reason': reason,
                           'message': {'content': json.dumps(content)}}]}).encode()

    def test_serialized_contract_and_parsed_response(self):
        opener = FakeOpener(self.envelope({'translations': [{'key': 'key', 'target': 'Привет {Player}'}]}))
        self.assertEqual(self.call(opener), {'key': 'Привет {Player}'})
        body = json.loads(opener.request.data)
        self.assertEqual(body['model'], 'test-model')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(body['max_tokens'], 4096)
        self.assertEqual(json.loads(body['messages'][1]['content'])[0]['context'], 'dialogue')

    def test_wrong_ids_duplicates_truncation_and_malformed_responses(self):
        for content in ({'translations': [{'key': 'other', 'target': 'Привет'}]},
                        {'translations': [{'key': 'key', 'target': 'a'}, {'key': 'key', 'target': 'b'}]},
                        {'translations': [{'key': 'key', 'target': None}]}, {}, []):
            with self.subTest(content=content), self.assertRaises(ProviderError):
                self.call(FakeOpener(self.envelope(content)))
        for raw in (b'bad json', self.envelope({}, 'length'), b'x' * (4 * 1024 * 1024 + 1)):
            with self.assertRaises(ProviderError):
                self.call(FakeOpener(raw))

    def test_a_broken_answer_names_its_reason_but_never_its_text(self):
        def raw(content, reason='stop'):
            return json.dumps({'choices': [{'finish_reason': reason, 'message': {'content': content}}],
                               'usage': {'prompt_tokens': 7, 'completion_tokens': 5}}).encode()

        cases = [(raw(json.dumps({'translations': [{'key': 'key', 'target': 'секрет'}]}), 'length'), 'incomplete', False),
                 (raw('{"translations": [{"key": "key", "target": "секрет'), 'invalid_json', True),
                 (raw(json.dumps({'translations': [], 'note': 'секрет'})), 'invalid_root', True),
                 (raw(json.dumps({'translations': [{'key': 'key', 'target': None, 'note': 'секрет'}]})), 'invalid_entry', True),
                 (raw(json.dumps({'translations': [{'key': 'секрет', 'target': 'секрет'}]})), 'mismatched_ids', True),
                 (json.dumps({'error': 'секрет'}).encode(), 'invalid_envelope', False)]
        for response, reason, retryable in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(ProviderError) as caught:
                    self.call(FakeOpener(response))
                self.assertEqual((caught.exception.failure, caught.exception.retryable), (reason, retryable))
                self.assertIn(reason, str(caught.exception))
                self.assertEqual(self.provider.last_usage['failure'], reason)
                self.assertNotIn('секрет', str(caught.exception) + json.dumps(self.provider.last_usage, ensure_ascii=False))
        self.assertEqual(self.provider.last_usage['input_tokens'], None)
        self.call(FakeOpener(self.envelope({'translations': [{'key': 'key', 'target': 'Привет {Player}'}]})))
        self.assertNotIn('failure', self.provider.last_usage)

    def test_http_retry_policy_never_includes_response_body(self):
        for code in (400, 401, 429, 500):
            error = urllib.error.HTTPError('https://example.invalid', code, 'private response',
                                            {'Retry-After': '2'}, io.BytesIO(b'private response'))
            with self.assertRaises(ProviderError) as result:
                self.call(FakeOpener(error=error))
            self.assertEqual(result.exception.retryable, code in (429, 500))
            self.assertEqual(result.exception.retry_after, 2)
            self.assertNotIn('private', str(result.exception))

    def test_https_and_explicit_permission_required(self):
        for url in ('http://localhost/v1', 'https://user:password@host/v1', 'https://host/?key=a'):
            with self.assertRaises(ValueError):
                OpenAICompatibleProvider(url, 'model')
        with patch('urllib.request.build_opener', side_effect=AssertionError('network')):
            with self.assertRaises(PermissionError):
                self.provider.translate(self.items)

    def test_luna_preview_is_exact_request_without_credentials_or_network(self):
        provider = OpenAICompatibleProvider('https://api.openai.com/v1', 'gpt-6-luna')
        with patch('urllib.request.build_opener', side_effect=AssertionError('network')):
            body = provider.request_body(self.items)
        self.assertEqual(body['reasoning_effort'], 'none')
        self.assertEqual(body['max_completion_tokens'], 4096)
        self.assertNotIn('max_tokens', body)
        self.assertEqual(body['response_format']['type'], 'json_schema')
        self.assertTrue(body['response_format']['json_schema']['strict'])
