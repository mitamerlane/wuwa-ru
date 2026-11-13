"""tools.public_export: allowlist, dependent tests dropped, secrets stop the export (synthetic)."""
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tools import public_export as export

TMP = Path(__file__).resolve().parents[1] / '.test-tmp' / 'public-export'


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, TMP, True)

    def test_selection_keeps_app_and_drops_research_and_dependent_tests(self):
        root = TMP / 'repo'
        files = {
            'wuwaru/a.py': '', 'AGENTS.md': '', 'docs/NATIVE-X.md': '', 'reports/r.json': '',
            'tools/release.py': '', 'tools/probe.py': '',
            'tests/test_a.py': 'from wuwaru import a\n',
            'tests/test_probe.py': 'from tools import probe\n',
            'tests/test_uses_probe_fixture.py': 'from tests.test_probe import X\n',
            'tests/test_release.py': 'from tools import release\nimport tests.test_a\n',
        }
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        with mock.patch.object(export, 'ROOT', root):
            chosen = export.select(sorted(files))
        self.assertEqual(chosen, ['tools/release.py', 'wuwaru/a.py', 'tests/test_a.py', 'tests/test_release.py'])

    def test_scan_finds_tokens_keys_and_forbidden_files(self):
        token = 'gh' + 'p_' + 'a' * 36
        samples = {
            'wuwaru/x.py': f"TOKEN = '{token}'\n".encode(),
            'wuwaru/key.txt': b'-----BEGIN ' + b'PRIVATE KEY-----\n',
            'data/game.pak': b'x',
            'wuwaru/fine.py': b"VERSION = '1.2.3'\n",
        }
        paths = {}
        for name, data in samples.items():
            path = TMP / name.replace('/', '_')
            path.write_bytes(data)
            paths[name] = path
        hits = export.scan(paths)
        self.assertEqual({hit.split(':')[0] for hit in hits}, {'wuwaru/x.py', 'wuwaru/key.txt', 'data/game.pak'})
        self.assertIn('wuwaru/x.py:1: GitHub token', hits)

    def test_personal_markers_come_from_the_machine_and_spare_no_file(self):
        home = TMP / 'Users' / 'Synthetic-Login'
        (home / '.wuwaru-release').mkdir(parents=True)
        (home / '.wuwaru-release' / 'personal.txt').write_text('# mine\nsynthetic.mail\n\n', encoding='utf-8')
        with mock.patch.object(export.Path, 'home', return_value=home):
            markers = export.personal()
        path = TMP / 'tool.py'
        path.write_bytes(b"A = r'C:\\Users\\synthetic-login\\x'\nB = 'Synthetic.Mail@example.com'\nC = 'synthetic-login'\n")
        hits = export.scan({'tools/public_export.py': path}, markers=markers)
        self.assertEqual(hits, ['tools/public_export.py:1: personal path', 'tools/public_export.py:2: personal marker'])

    def test_the_tool_itself_names_no_person(self):
        source = Path(export.__file__).read_bytes()
        self.assertEqual([label for pattern, label in export.personal() if pattern.search(source)], [])

    def test_export_refuses_a_folder_inside_the_repository(self):
        with self.assertRaises(SystemExit):
            export.export(export.ROOT / '.test-tmp' / 'out')

    def test_current_tree_has_no_secrets(self):
        files = {name: export.ROOT / name for name in export.select(export.tracked())}
        files.update(export.overlay())
        self.assertEqual(export.scan(files), [])


if __name__ == '__main__':
    unittest.main()
