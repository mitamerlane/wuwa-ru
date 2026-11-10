import contextlib
import io
import json

from tests.test_discovery import FixtureCase, fixture
from tests.test_sqlite_source import database
from tests.pak_fixture import pak
from wuwaru.__main__ import main
from wuwaru.discovery import inspect_installation
from wuwaru.extraction import extract


class TranslationCLITests(FixtureCase):
    def command(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(list(arguments))
        self.assertEqual(code, 0, errors.getvalue())
        return json.loads(output.getvalue())

    def test_m1_staging_to_m2_cli_round_trip(self):
        game = fixture(self.home / 'synthetic-game')
        path = database(self.home / 'lang_test.db')
        package = 'Client/Content/Paks/base.pak'
        pak(game / package, {'lang_test.db': path.read_bytes()})
        staged = self.home / 'm1-stage'
        extract(inspect_installation(game), staged, packages=[package])
        before = (game / package).read_bytes()
        workspace = str(self.home / 'work')
        result = self.command('translation', '--workspace', workspace, 'import',
                              '--resource', 'lang_test.db', '--source', str(staged / 'lang_test.db'))
        self.assertEqual(result['new'], 3)
        preview = self.command('translation', '--workspace', workspace, 'preview')
        self.assertEqual(preview['requests'], 1)
        self.assertFalse(preview['network_sent'])
        self.command('translation', '--workspace', workspace, 'run', '--run-id', 'cli')
        self.command('translation', '--workspace', workspace, 'export', '--name', 'ru', '--allow-machine')
        status = self.command('translation', '--workspace', workspace, 'status')
        self.assertEqual(status['statuses'], {'MACHINE_TRANSLATED': 3})
        qa = self.command('translation', '--workspace', workspace, 'qa', '--name', 'output/qa')
        # A valid empty original/target is exportable, but not counted as filled prose.
        self.assertEqual((qa['total'], qa['translated'], qa['unchecked']), (3, 2, 3))
        self.assertEqual((game / package).read_bytes(), before)
