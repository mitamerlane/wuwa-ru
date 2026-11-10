"""Self-update: release lookup, signed statement, verified download, installer hand-over (synthetic)."""
import hashlib
import io
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wuwaru import updater

TMP = Path(__file__).resolve().parents[1] / '.test-tmp' / 'updater'
REPO = updater.UPDATE_REPO
DOWNLOAD = f'https://github.com/{REPO}/releases/download'


class Response(io.BytesIO):
    def __init__(self, data: bytes, url: str):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeWeb:
    """Serves bytes by URL; `redirects` maps a URL to the final URL a server would answer from."""

    def __init__(self, pages: dict[str, bytes], redirects: dict[str, str] | None = None):
        self.pages, self.redirects, self.seen = pages, redirects or {}, []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.seen.append(url)
        if url not in self.pages:
            raise OSError(f'404 {url}')
        return Response(self.pages[url], self.redirects.get(url, url))


def release_json(version='0.2.0', assets=None, tag=None):
    setup = f'WuWaRussian-Setup-{version}.exe'
    assets = assets if assets is not None else [
        {'name': setup, 'size': 11, 'browser_download_url': f'{DOWNLOAD}/v{version}/{setup}'},
        {'name': 'update.json', 'size': 300, 'browser_download_url': f'{DOWNLOAD}/v{version}/update.json'},
    ]
    return json.dumps({'tag_name': tag or f'v{version}', 'name': f'WuWa Russian {version}',
                       'body': 'Что нового', 'html_url': f'https://github.com/{REPO}/releases/tag/v{version}',
                       'assets': assets}).encode()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, TMP, True)
        self.key = Ed25519PrivateKey.generate()
        self.public = updater.public_key_text(self.key.public_key())
        self.setup_bytes = b'new-setup!!'

    def statement(self, version='0.2.0', data=None, **override):
        data = self.setup_bytes if data is None else data
        fields = {'version': version, 'file': f'WuWaRussian-Setup-{version}.exe', 'size': len(data),
                  'sha256': hashlib.sha256(data).hexdigest(), **override}
        return updater.sign_statement(fields, self.key)

    def web(self, version='0.2.0', statement=None, setup=None, api=None):
        return FakeWeb({updater.API_URL: api if api is not None else release_json(version),
                        f'{DOWNLOAD}/v{version}/update.json': statement or self.statement(version),
                        f'{DOWNLOAD}/v{version}/WuWaRussian-Setup-{version}.exe':
                            self.setup_bytes if setup is None else setup})

    # -- versions and lookup -----------------------------------------------------------------

    def test_versions_compare_numerically_and_reject_garbage(self):
        self.assertEqual(updater.parse_version('v0.10.2'), (0, 10, 2))
        self.assertGreater(updater.parse_version('0.10.0'), updater.parse_version('0.9.9'))
        for bad in ('', 'latest', '1.2', '1.2.3-beta', '1.2.3.4', 'v1.2.x'):
            with self.subTest(bad=bad), self.assertRaises(updater.UpdateError):
                updater.parse_version(bad)

    def test_latest_release_finds_setup_and_statement(self):
        web = self.web()
        release = updater.latest_release(web)
        self.assertEqual((release.version, release.setup_name), ('0.2.0', 'WuWaRussian-Setup-0.2.0.exe'))
        self.assertTrue(updater.is_newer(release, '0.1.0'))
        self.assertFalse(updater.is_newer(release, '0.2.0'))
        self.assertFalse(updater.is_newer(release, '0.3.0'))
        self.assertEqual(web.seen, [updater.API_URL])

    def test_release_without_signed_statement_or_from_foreign_url_is_refused(self):
        setup = 'WuWaRussian-Setup-0.2.0.exe'
        cases = {
            'no statement': [{'name': setup, 'size': 1, 'browser_download_url': f'{DOWNLOAD}/v0.2.0/{setup}'}],
            'foreign host': [{'name': setup, 'size': 1, 'browser_download_url': f'https://evil.example/{setup}'},
                             {'name': 'update.json', 'size': 1,
                              'browser_download_url': f'{DOWNLOAD}/v0.2.0/update.json'}],
            'other repo': [{'name': setup, 'size': 1,
                            'browser_download_url': f'https://github.com/someone/else/releases/download/v0.2.0/{setup}'},
                           {'name': 'update.json', 'size': 1,
                            'browser_download_url': f'{DOWNLOAD}/v0.2.0/update.json'}],
        }
        for name, assets in cases.items():
            with self.subTest(name), self.assertRaises(updater.UpdateError):
                updater.latest_release(FakeWeb({updater.API_URL: release_json(assets=assets)}))

    def test_oversized_or_broken_api_answer_is_refused(self):
        for body in (b'{', b'[]', b'x' * (updater.API_LIMIT + 1), release_json(tag='nightly')):
            with self.subTest(size=len(body)), self.assertRaises(updater.UpdateError):
                updater.latest_release(FakeWeb({updater.API_URL: body}))

    def test_redirect_to_unexpected_host_is_refused(self):
        web = FakeWeb({updater.API_URL: release_json()}, {updater.API_URL: 'https://evil.example/x'})
        with self.assertRaises(updater.UpdateError):
            updater.latest_release(web)

    def test_network_failure_becomes_update_error(self):
        with self.assertRaises(updater.UpdateError):
            updater.latest_release(FakeWeb({}))

    # -- statement and download ---------------------------------------------------------------

    def test_download_verifies_signature_size_and_hash(self):
        web = self.web()
        release = updater.latest_release(web)
        seen = []
        path = updater.download(release, TMP / 'Updates', web, public_key=self.public,
                                progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(path.read_bytes(), self.setup_bytes)
        self.assertEqual(path.name, 'WuWaRussian-Setup-0.2.0.exe')
        self.assertEqual(seen[-1], (len(self.setup_bytes), len(self.setup_bytes)))
        self.assertFalse(list((TMP / 'Updates').glob('*.part')))

    def test_download_removes_setups_of_earlier_updates_only(self):
        folder = TMP / 'Updates'
        folder.mkdir()
        (folder / 'WuWaRussian-Setup-0.1.5.exe').write_bytes(b'old')
        (folder / 'notes.txt').write_text('mine')
        web = self.web()
        updater.download(updater.latest_release(web), folder, web, public_key=self.public)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['WuWaRussian-Setup-0.2.0.exe', 'notes.txt'])

    def test_statement_signed_by_another_key_is_refused(self):
        other = Ed25519PrivateKey.generate()
        fields = {'version': '0.2.0', 'file': 'WuWaRussian-Setup-0.2.0.exe', 'size': len(self.setup_bytes),
                  'sha256': hashlib.sha256(self.setup_bytes).hexdigest()}
        web = self.web(statement=updater.sign_statement(fields, other))
        with self.assertRaisesRegex(updater.UpdateError, 'Подпись'):
            updater.download(updater.latest_release(web), TMP / 'Updates', web, public_key=self.public)
        self.assertFalse((TMP / 'Updates' / 'WuWaRussian-Setup-0.2.0.exe').exists())

    def test_tampered_statement_is_refused(self):
        doc = json.loads(self.statement())
        doc['sha256'] = hashlib.sha256(b'evil').hexdigest()
        web = self.web(statement=json.dumps(doc).encode())
        with self.assertRaisesRegex(updater.UpdateError, 'Подпись'):
            updater.download(updater.latest_release(web), TMP / 'Updates', web, public_key=self.public)

    def test_old_signed_statement_cannot_be_replayed_as_new_version(self):
        web = self.web(statement=self.statement('0.1.5'))
        with self.assertRaisesRegex(updater.UpdateError, 'не относится'):
            updater.download(updater.latest_release(web), TMP / 'Updates', web, public_key=self.public)

    def test_setup_that_does_not_match_statement_is_deleted(self):
        for name, setup in (('other bytes', b'evil-setup!'), ('longer', self.setup_bytes + b'x'),
                            ('shorter', self.setup_bytes[:-1])):
            with self.subTest(name), self.assertRaises(updater.UpdateError):
                web = self.web(setup=setup)
                updater.download(updater.latest_release(web), TMP / 'Updates', web, public_key=self.public)
            self.assertEqual(list((TMP / 'Updates').iterdir()), [])

    def test_cancelled_download_leaves_nothing(self):
        web = self.web()
        with self.assertRaises(updater.Cancelled):
            updater.download(updater.latest_release(web), TMP / 'Updates', web, public_key=self.public,
                             cancelled=lambda: True)
        self.assertEqual(list((TMP / 'Updates').iterdir()), [])

    def test_release_without_pinned_key_cannot_download(self):
        web = self.web()
        with mock.patch.object(updater, 'PUBLIC_KEY', ''), self.assertRaisesRegex(updater.UpdateError, 'ключ'):
            updater.download(updater.latest_release(web), TMP / 'Updates', web)

    # -- hand-over to the installer -----------------------------------------------------------

    def test_install_root_only_for_an_installed_frozen_app(self):
        app = TMP / 'app'
        app.mkdir()
        exe = app / 'WuWaRussian.exe'
        exe.write_bytes(b'x')
        with mock.patch.object(updater.sys, 'executable', str(exe)), \
                mock.patch.object(updater.sys, 'frozen', True, create=True):
            self.assertIsNone(updater.install_root())
            (app / '.install-manifest.json').write_text('{}')
            self.assertEqual(updater.install_root(), app.resolve())
        with mock.patch.object(updater.sys, 'executable', str(exe)):
            if hasattr(updater.sys, 'frozen'):
                self.skipTest('running frozen')
            self.assertIsNone(updater.install_root())

    def test_start_installer_passes_target_and_waits_for_this_process(self):
        setup = TMP / 'WuWaRussian-Setup-0.2.0.exe'
        setup.write_bytes(b'x')
        popen = mock.Mock()
        updater.start_installer(setup, TMP / 'app', pid=4242, popen=popen)
        args = popen.call_args.args[0]
        self.assertEqual(args, [str(setup), '--update', str(TMP / 'app'), '--wait-pid', '4242'])


if __name__ == '__main__':
    unittest.main()
