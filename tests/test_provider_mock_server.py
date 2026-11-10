"""Real loopback HTTPS only, with an ephemeral self-authored certificate and key."""
import datetime
import io
import ipaddress
import json
import ssl
import threading
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.test_discovery import FixtureCase
from wuwaru.engine import Budget, TranslationEngine
from wuwaru.pilot import prepare_synthetic
from wuwaru.providers import NoRedirect, OpenAICompatibleProvider, ProviderError
from wuwaru.validation import TOKEN


class MockServerTests(FixtureCase):
    def test_models_chat_usage_auth_errors_and_placeholders_over_https(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '127.0.0.1')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(hours=1)).add_extension(x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(key, hashes.SHA256()))
        cert_path, key_path = self.home / 'cert.pem', self.home / 'key.pem'
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        requests = []
        class Handler(BaseHTTPRequestHandler):
            status = 200
            broken_tokens = False

            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.command, self.path, None))
                self.reply({'data': [{'id': 'gpt-6-luna'}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.command, self.path, body))
                values = json.loads(body['messages'][1]['content'])
                translated = {'translations': [dict(key=row['key'], target='Перевод ' +
                    ('' if self.broken_tokens else ''.join(TOKEN.findall(row['source'])))) for row in values]}
                self.reply({'choices': [dict(finish_reason='stop', message=dict(content=json.dumps(translated)))],
                            'usage': dict(prompt_tokens=123, completion_tokens=45, total_tokens=168)})

            def reply(self, value):
                if self.status != 200:
                    value = {'error': 'synthetic-api-key-do-not-log'}
                body = json.dumps(value).encode()
                self.send_response(self.status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        server = HTTPServer(('127.0.0.1', 0), Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = ssl.create_default_context(cafile=str(cert_path))
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}),
                                            urllib.request.HTTPSHandler(context=client))
        provider = OpenAICompatibleProvider(f'https://127.0.0.1:{server.server_port}/v1',
            'gpt-6-luna', profile='RelayModels', key_getter=lambda: 'synthetic-api-key-do-not-log')
        core = prepare_synthetic(self.home)
        self.addCleanup(core.close)
        logs = io.StringIO()
        try:
            with patch('urllib.request.build_opener', return_value=opener), redirect_stdout(logs), redirect_stderr(logs):
                self.assertEqual(provider.list_models(allow_network=True), ['gpt-6-luna'])
                self.assertEqual(requests, [('GET', '/v1/models', None)])
                engine = TranslationEngine(core, provider, interval=0, selection=[1, 2])
                budget = Budget(allowed=True, max_requests=3, max_cost=1, input_price=1, output_price=1,
                                bounded_pilot=True)
                engine.run('mock-https', batch_size=2, budget=budget)
                self.assertEqual(core.record(1)['status'], 'MACHINE_TRANSLATED')
                usage = json.loads(core.db.execute('SELECT actual_usage_json FROM usage').fetchone()[0])
                self.assertEqual({k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
                                 dict(prompt_tokens=123, completion_tokens=45, total_tokens=168))
                self.assertEqual(usage['model'], 'gpt-6-luna')
                self.assertEqual(usage['input_tokens'], 123)
                self.assertEqual(usage['output_tokens'], 45)
                self.assertEqual(usage['coefficient'], 0.38)
                self.assertIsNone(usage['actual_cost'])
                self.assertIsNone(usage['cost_currency'])
                body = requests[1][2]
                self.assertEqual(requests[1][:2], ('POST', '/v1/chat/completions'))
                self.assertNotIn('reasoning_effort', body)
                self.assertEqual(body['response_format']['type'], 'json_schema')
                self.assertEqual(engine.preview()['requests'], 0)
                Handler.broken_tokens = True
                TranslationEngine(core, provider, interval=0, selection=[3]).run('broken', budget=budget)
                self.assertEqual(core.record(3)['status'], 'INVALID')
                for status in (401, 403, 404, 429, 503):
                    Handler.status = status
                    with self.assertRaises(ProviderError) as error:
                        provider.list_models(allow_network=True)
                    self.assertIn(str(status), str(error.exception))
                    self.assertNotIn('synthetic-api-key-do-not-log', str(error.exception))
            self.assertNotIn('synthetic-api-key-do-not-log', logs.getvalue())
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()
