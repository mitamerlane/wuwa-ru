"""tools.release: key never inside the repo, signature matches the pinned key, publish checks first."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools import release
from wuwaru import updater

TMP = Path(__file__).resolve().parents[1] / '.test-tmp' / 'release'


class ReleaseToolTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, TMP, True)
        self.key = Ed25519PrivateKey.generate()
        patcher = mock.patch.object(updater, 'PUBLIC_KEY', updater.public_key_text(self.key.public_key()))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.setup = TMP / 'WuWaRussian-Setup-9.9.9.exe'
        self.setup.write_bytes(b'setup-bytes')

    def test_init_key_refuses_repository_and_existing_file(self):
        with self.assertRaisesRegex(SystemExit, 'внутри репозитория'):
            release.init_key(TMP / 'signing-key.pem')  # .test-tmp lies inside the repository
        self.assertFalse((TMP / 'signing-key.pem').exists())
        outside = Path(tempfile.mkdtemp(prefix='wuwaru-key-'))
        self.addCleanup(shutil.rmtree, outside, True)
        public = release.init_key(outside / 'signing-key.pem')
        self.assertEqual(public, updater.public_key_text(release.load_key(outside / 'signing-key.pem').public_key()))
        with self.assertRaisesRegex(SystemExit, 'уже есть'):
            release.init_key(outside / 'signing-key.pem')

    def test_signed_statement_verifies_with_pinned_key(self):
        out = release.sign(self.setup, '9.9.9', self.key)
        rel = updater.Release('9.9.9', self.setup.name, '', '', '', '')
        signed = updater.verify_statement(out.read_bytes(), rel)
        self.assertEqual(signed.size, len(b'setup-bytes'))

    def test_sign_refuses_a_key_launchers_would_reject(self):
        with self.assertRaisesRegex(SystemExit, 'PUBLIC_KEY'):
            release.sign(self.setup, '9.9.9', Ed25519PrivateKey.generate())

    def test_publish_checks_files_before_any_network(self):
        with mock.patch.object(release, 'DIST', TMP), mock.patch.object(release, 'api') as api:
            with self.assertRaises(SystemExit):
                release.publish('9.9.9', 'notes', token='t')  # no .sha256 yet
            (TMP / f'{self.setup.name}.sha256').write_text('x')
            release.sign(self.setup, '9.9.9', self.key)
            self.setup.write_bytes(b'changed-after-signing')
            with self.assertRaises(SystemExit):
                release.publish('9.9.9', 'notes', token='t')
        api.assert_not_called()

    def test_publish_creates_release_and_uploads_three_files(self):
        (TMP / f'{self.setup.name}.sha256').write_text('x')
        release.sign(self.setup, '9.9.9', self.key)
        answers = [{'upload_url': 'https://uploads.github.com/repos/o/r/releases/1/assets{?name,label}',
                    'html_url': 'https://github.com/o/r/releases/tag/v9.9.9'}, {}, {}, {}]
        with mock.patch.object(release, 'DIST', TMP), mock.patch.object(release, 'api', side_effect=answers) as api:
            url = release.publish('9.9.9', 'notes', repo='o/r', token='t')
        self.assertTrue(url.endswith('v9.9.9'))
        names = [call.args[1].split('name=')[-1] for call in api.call_args_list[1:]]
        self.assertEqual(names, [self.setup.name, f'{self.setup.name}.sha256', 'update.json'])


if __name__ == '__main__':
    unittest.main()
