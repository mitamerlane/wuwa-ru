"""A newer public English table: accepted only with its provenance, bound to the game version it was accepted for.

Self-authored SQLite in `.test-tmp/` only; the fetch talks to a fake GitHub, never the network or the game."""
import hashlib
import io
import json
import sqlite3
from unittest import mock

from tests.test_discovery import fixture
from tests.test_native_launch import CHANGED, VERIFIED, mount_bytes
from tests.test_native_package import TABLE, PackageCase
from tools import native_table as tool
from wuwaru import native_launch, native_package as package
from wuwaru.native_launch import Phase
from wuwaru.pak_reader import V12ArchiveReader

NEWER = dict(TABLE, PrefabTextItem_3_Text='New text')  # the project's English of item 3 is now official
REVISION = 'a' * 40


def blob(data: bytes) -> str:
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


class TableCase(PackageCase):
    def setUp(self):
        super().setUp()
        font_pin = mock.patch.object(package, 'FONT_SHA256', self.pins[1])  # the synthetic font stands in for Noto
        font_pin.start()
        self.addCleanup(font_pin.stop)
        self.newer = self.home / 'download' / 'lang_multi_text.db'
        self.newer.parent.mkdir()
        with sqlite3.connect(self.newer) as db:
            db.execute('CREATE TABLE MultiText(Id TEXT PRIMARY KEY, Content TEXT, RedirectDbIndex INTEGER)')
            db.executemany('INSERT INTO MultiText VALUES (?,?,?)',
                           ((key, value, int(key == 'PrefabTextItem_4_Text')) for key, value in NEWER.items()))
        db.close()

    def record(self, source=None, **changes):
        data = (source or self.newer).read_bytes()
        record = dict(repository=package.TABLE_REPOSITORY, path=package.TABLE_PATH, revision=REVISION,
                      blob=blob(data), size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                      data_version='3.8: Text - per-language string tables', data_date='2026-11-07T06:00:00Z')
        record.update(changes)
        return record

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.native.iterdir() if path.is_file()}


class TableRecordTests(TableCase):
    def test_without_record_the_pinned_snapshot_is_the_table(self):
        record = package.table_record(self.native)
        self.assertEqual(record['revision'], '30b665d7e783f8aedd8a9c2c4f17b7555eb2b10d')
        self.assertEqual(record['sha256'], package.SOURCE_SHA256)
        self.assertEqual(record['blob'], '15e99ad486a7dc09d2edbcc0f3a10e81b7f94c8b')
        self.assertEqual(record['size'], 45834240)
        with self.assertRaisesRegex(ValueError, 'Pinned public snapshot changed'):
            package.check_inputs(self.inputs)  # the synthetic table is not the pinned snapshot

    def test_install_table_replaces_the_table_and_records_provenance(self):
        record = self.record()
        package.install_table(self.newer, record, self.native)
        self.assertEqual(self.inputs.table.read_bytes(), self.newer.read_bytes())
        self.assertEqual(package.table_record(self.native), record)
        package.check_inputs(self.inputs)  # pins now come from table.json
        journal = [json.loads(line) for line in (self.native / 'journal.jsonl').read_text('utf-8').splitlines()]
        self.assertEqual(journal[-1]['event'], 'install_table')
        self.assertEqual(journal[-1]['revision'], REVISION)
        self.assertEqual(journal[-1]['previous_sha256'], self.pins[0])

    def test_wrong_provenance_or_content_changes_nothing(self):
        bad = {'blob': dict(blob='0' * 40), 'sha256': dict(sha256='0' * 64), 'size': dict(size=1),
               'repository': dict(repository='someone/WWUIResourcesFull'), 'path': dict(path='Text/ConfigDB/ru/x.db'),
               'revision': dict(revision='main'), 'extra': dict(key='game.pak')}
        before = self.snapshot()
        for name, changes in bad.items():
            with self.subTest(name), self.assertRaises(ValueError):
                package.install_table(self.newer, self.record(**changes), self.native)
            self.assertEqual(self.snapshot(), before)
        broken = self.home / 'download' / 'broken.db'
        broken.write_bytes(b'not sqlite at all')
        with self.assertRaises(ValueError):
            package.install_table(broken, self.record(broken), self.native)
        other = self.home / 'download' / 'other.db'
        with sqlite3.connect(other) as db:
            db.execute('CREATE TABLE MultiText(Id TEXT PRIMARY KEY, Content TEXT)')
            db.execute("INSERT INTO MultiText VALUES ('a', 'b')")
        db.close()
        with self.assertRaisesRegex(ValueError, 'RedirectDbIndex'):
            package.install_table(other, self.record(other), self.native)
        self.assertEqual(self.snapshot(), before)

    def test_table_must_match_its_record(self):
        package.install_table(self.newer, self.record(), self.native)
        self.inputs.table.write_bytes(self.inputs.table.read_bytes() + b'x')
        with self.assertRaisesRegex(ValueError, 'table.json'):
            package.check_inputs(self.inputs)
        (self.native / 'table.json').write_text('{"revision": "x"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            package.table_record(self.native)

    def test_install_inputs_with_the_pinned_table_drops_a_newer_record(self):
        pinned = self.home / 'pinned.db'
        pinned.write_bytes(self.inputs.table.read_bytes())
        package.install_table(self.newer, self.record(), self.native)
        # The input dialog accepts only the pinned bytes; the newer record goes away with them.
        with mock.patch.object(package, 'PINNED_TABLE', dict(package.PINNED_TABLE, sha256=package.sha256(pinned))):
            with self.assertRaisesRegex(ValueError, 'таблица'):
                package.install_inputs(self.newer, self.inputs.font, self.home / 'elsewhere')
            package.install_inputs(pinned, self.inputs.font, self.native)
            self.assertFalse((self.native / 'table.json').exists())
            self.assertEqual(package.table_record(self.native)['sha256'], package.sha256(pinned))
            package.check_inputs(self.inputs)


class TableBuildTests(TableCase):
    def test_newer_table_lets_new_english_in_and_names_the_table(self):
        old_cells, old_left = package.select_cells(self.project, self.inputs, ('ui',))
        self.assertEqual(old_left['ENGLISH_DIFFERS'], 1)
        old_key = self.key()
        package.install_table(self.newer, self.record(), self.native)
        report = package.build_package(self.project, self.native, ('ui',))
        self.assertNotEqual(report['key'], old_key)
        self.assertEqual(report['cells'], len(old_cells) + 1)
        self.assertNotIn('ENGLISH_DIFFERS', report['left_out'])
        self.assertEqual(report['table']['revision'], REVISION)
        self.assertEqual(report['table']['sha256'], self.record()['sha256'])
        archive = self.native / 'staging' / (report['key'] + '.pak')
        entries = V12ArchiveReader().list_entries(archive)
        self.assertEqual([entry.path for entry in entries], [package.DATABASE_ENTRY, package.FONT_ENTRY])
        saved = json.loads((self.native / 'package.json').read_text('utf-8'))
        self.assertEqual(saved['table'], report['table'])

    def test_label_names_revision_and_date(self):
        self.assertEqual(package.table_label(package.PINNED_TABLE), 'WWUIResourcesFull 30b665d7e783 · 30.09.2026')
        self.assertEqual(package.table_label(None), 'WWUIResourcesFull 30b665d7e783 · 30.09.2026')
        self.assertEqual(package.table_label(self.record(data_date=None)), 'WWUIResourcesFull aaaaaaaaaaaa')


class TableVersionTests(TableCase):
    """Our PAK replaces the whole table: an accepted game version holds only for the table it was accepted with."""

    def setUp(self):
        super().setUp()
        self.game = fixture(self.home / 'game')
        self.mount = self.game / native_launch.MOUNT
        self.mount.write_bytes(mount_bytes())

    def inspect(self, key='k1'):
        return native_launch.inspect(self.game, self.native, key, running=lambda game: [], guard=lambda game: None)

    def test_verified_version_holds_only_for_the_pinned_table(self):
        self.assertIn(VERIFIED, native_launch.accepted_fingerprints(self.native))
        package.install_table(self.newer, self.record(), self.native)
        self.assertNotIn(VERIFIED, native_launch.accepted_fingerprints(self.native))
        state = self.inspect()
        self.assertEqual(state.phase, Phase.VERSION_CHANGED)
        self.assertIn('Таблица строк не проверена с этой версией игры', state.reasons)
        self.assertEqual(state.table['revision'], REVISION)
        native_launch.accept_fingerprint(self.native, VERIFIED)
        self.assertIn(VERIFIED, native_launch.accepted_fingerprints(self.native))
        self.assertNotEqual(self.inspect().phase, Phase.VERSION_CHANGED)
        saved = json.loads((self.native / 'accepted.json').read_text('utf-8'))
        self.assertEqual(saved['accepted'][-1]['table_sha256'], self.record()['sha256'])

    def test_old_table_on_a_new_game_version_is_flagged(self):
        self.mount.write_bytes(mount_bytes(first=CHANGED))
        fp = self.inspect().fingerprint
        self.assertTrue(native_launch.table_reused(self.native, fp))  # pinned table belongs to VERIFIED
        package.install_table(self.newer, self.record(), self.native)
        self.assertFalse(native_launch.table_reused(self.native, fp))
        native_launch.accept_fingerprint(self.native, fp)
        self.assertFalse(native_launch.table_reused(self.native, fp))

    def test_old_accepted_entries_belong_to_the_pinned_table(self):
        (self.native / 'accepted.json').write_text(json.dumps({'schema_version': 1, 'accepted': [
            {'fingerprint': [CHANGED, VERIFIED[1]], 'accepted_at': '2026-11-01T00:00:00+00:00'}]}), encoding='utf-8')
        self.assertIn((CHANGED, VERIFIED[1]), native_launch.accepted_fingerprints(self.native))
        package.install_table(self.newer, self.record(), self.native)
        self.assertNotIn((CHANGED, VERIFIED[1]), native_launch.accepted_fingerprints(self.native))


class FakeGitHub:
    def __init__(self, data: bytes, *, served: bytes | None = None):
        self.data, self.served, self.urls = data, data if served is None else served, []
        self.tree = {'root': [{'path': 'Text', 'type': 'tree', 'sha': 't1'}],
                     't1': [{'path': 'ConfigDB', 'type': 'tree', 'sha': 't2'}],
                     't2': [{'path': 'en', 'type': 'tree', 'sha': 't3'}],
                     't3': [{'path': 'lang_multi_text.db', 'type': 'blob', 'sha': blob(data), 'size': len(data)},
                            {'path': 'lang_ui.db', 'type': 'blob', 'sha': 'b' * 40, 'size': 10}]}

    def __call__(self, url):
        self.urls.append(url)
        api = 'https://api.github.com/repos/Loping151/WWUIResourcesFull'
        if url == api:
            body = {'default_branch': 'main'}
        elif url in (api + '/commits/main', api + '/commits/' + REVISION):
            body = {'sha': REVISION, 'commit': {'message': '3.8: Text - per-language string tables\n\nmore',
                                                'committer': {'date': '2026-11-07T06:00:00Z'},
                                                'tree': {'sha': 'root'}}}
        elif url.startswith(api + '/git/trees/'):
            body = {'tree': self.tree[url.rsplit('/', 1)[1]], 'truncated': False}
        elif url == ('https://raw.githubusercontent.com/Loping151/WWUIResourcesFull/' + REVISION +
                     '/Text/ConfigDB/en/lang_multi_text.db'):
            return io.BytesIO(self.served)
        else:
            raise AssertionError('unexpected url ' + url)
        return io.BytesIO(json.dumps(body).encode('utf-8'))


class FetchTests(TableCase):
    def test_fetch_latest_installs_with_git_provenance(self):
        github = FakeGitHub(self.newer.read_bytes())
        record = tool.fetch(self.native, opener=github)
        self.assertEqual(record, dict(self.record(), data_version='3.8: Text - per-language string tables'))
        self.assertEqual(package.table_record(self.native), record)
        self.assertEqual(self.inputs.table.read_bytes(), self.newer.read_bytes())
        self.assertFalse(any(path.name.startswith('.fetch-') for path in self.native.iterdir()))

    def test_fetch_of_a_named_revision(self):
        github = FakeGitHub(self.newer.read_bytes())
        self.assertEqual(tool.fetch(self.native, REVISION, opener=github)['revision'], REVISION)
        self.assertNotIn('https://api.github.com/repos/Loping151/WWUIResourcesFull', github.urls)
        with self.assertRaises(ValueError):
            tool.fetch(self.native, 'main', opener=github)

    def test_download_that_differs_from_the_tree_changes_nothing(self):
        before = self.snapshot()
        data = self.newer.read_bytes()
        for served in (data[:-1] + bytes([data[-1] ^ 1]), data + b'x', data[:-5]):
            with self.subTest(len(served)), self.assertRaises(ValueError):
                tool.fetch(self.native, opener=FakeGitHub(data, served=served))
            self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(path.name.startswith('.fetch-') for path in self.native.iterdir()))


TEXT_REVISION = 'c' * 40
TEXT_API = 'https://api.github.com/repos/Arikatsu/WutheringWaves_Data'


def text_json(rows) -> bytes:
    return json.dumps([dict(Id=key, Content=value, RedirectDbIndex=redirect) for key, value, redirect in rows],
                      ensure_ascii=False, indent=2).encode('utf-8')


class FakeTextGitHub:
    """Arikatsu/WutheringWaves_Data at one revision: Textmaps/en/multi_text/MultiText.json."""

    def __init__(self, data: bytes, *, served: bytes | None = None, commits=None):
        self.data, self.served, self.urls = data, data if served is None else served, []
        self.commits = commits
        self.tree = {'root': [{'path': 'Textmaps', 'type': 'tree', 'sha': 't1'}],
                     't1': [{'path': 'en', 'type': 'tree', 'sha': 't2'}],
                     't2': [{'path': 'multi_text', 'type': 'tree', 'sha': 't3'}],
                     't3': [{'path': 'MultiText.json', 'type': 'blob', 'sha': blob(data), 'size': len(data)}]}

    def __call__(self, url):
        self.urls.append(url)
        if url == TEXT_API + '/commits/' + TEXT_REVISION:
            body = {'sha': TEXT_REVISION, 'commit': {'message': 'GameVer 3.8.0 | ResVer 3.8.9 | Release',
                                                     'committer': {'date': '2026-11-12T02:56:18Z'},
                                                     'tree': {'sha': 'root'}}}
        elif url.startswith(TEXT_API + '/git/trees/'):
            body = {'tree': self.tree[url.rsplit('/', 1)[1]], 'truncated': False}
        elif url == TEXT_API + '/commits?path=Textmaps/en/multi_text/MultiText.json&per_page=30':
            body = self.commits
        elif url == ('https://raw.githubusercontent.com/Arikatsu/WutheringWaves_Data/' + TEXT_REVISION +
                     '/Textmaps/en/multi_text/MultiText.json'):
            return io.BytesIO(self.served)
        else:
            raise AssertionError('unexpected url ' + url)
        return io.BytesIO(json.dumps(body).encode('utf-8'))


class BuildFromTextTests(TableCase):
    """No public table for the new version yet: the same rows come from the public text source."""

    ROWS = [(key, value, int(key == 'PrefabTextItem_4_Text')) for key, value in reversed(list(NEWER.items()))]

    def test_build_writes_the_rows_in_source_order_with_provenance(self):
        data = text_json(self.ROWS)
        record = tool.build(self.native, TEXT_REVISION, opener=FakeTextGitHub(data))
        built = self.inputs.table.read_bytes()
        self.assertEqual(record, dict(
            repository='Arikatsu/WutheringWaves_Data', path='Textmaps/en/multi_text/MultiText.json',
            revision=TEXT_REVISION, blob=blob(built), size=len(built), sha256=hashlib.sha256(built).hexdigest(),
            data_version='GameVer 3.8.0 | ResVer 3.8.9 | Release', data_date='2026-11-12T02:56:18Z',
            source_blob=blob(data)))
        self.assertEqual(package.table_record(self.native), record)
        database = package.open_read_only(self.inputs.table)
        try:
            self.assertEqual(database.execute('SELECT Id, Content, RedirectDbIndex FROM MultiText ORDER BY rowid')
                             .fetchall(), self.ROWS)
            self.assertEqual(database.execute("SELECT sql FROM sqlite_master WHERE name='MultiText'").fetchone()[0],
                             package.TABLE_DDL)
        finally:
            database.close()
        self.assertIn('WutheringWaves_Data ' + TEXT_REVISION[:12], package.table_label(record))
        self.assertFalse(any(path.name.startswith('.fetch-') for path in self.native.iterdir()))
        package.check_inputs(self.inputs)

    def test_the_revision_must_be_named_in_full(self):
        for revision in (None, '3.8', 'c' * 39):
            with self.subTest(revision), self.assertRaises(ValueError):
                tool.build(self.native, revision, opener=FakeTextGitHub(text_json(self.ROWS)))

    def test_bad_source_changes_nothing(self):
        before = self.snapshot()
        good = text_json(self.ROWS)
        first = self.ROWS[0]
        bad = {'duplicate': text_json(self.ROWS + [first]), 'empty': b'[]', 'not a list': b'{}',
               'content': text_json([(first[0], 5, 0)]), 'redirect': text_json([(first[0], 'x', '0')]),
               'bool': text_json([(first[0], 'x', True)]), 'id': text_json([('', 'x', 0)]),
               'extra field': json.dumps([dict(Id='a', Content='b', RedirectDbIndex=0, More=1)]).encode(),
               'not json': b'\xff\xfe'}
        for name, data in bad.items():
            with self.subTest(name), self.assertRaises(ValueError):
                tool.build(self.native, TEXT_REVISION, opener=FakeTextGitHub(data))
            self.assertEqual(self.snapshot(), before)
        with self.assertRaises(ValueError):  # the download is not the file of the tree
            tool.build(self.native, TEXT_REVISION, opener=FakeTextGitHub(good, served=good + b' '))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(path.name.startswith('.fetch-') for path in self.native.iterdir()))

    def test_source_blob_belongs_only_to_a_built_table(self):
        built = tool.build(self.native, TEXT_REVISION, opener=FakeTextGitHub(text_json(self.ROWS)))
        without = {name: value for name, value in built.items() if name != 'source_blob'}
        for record in (without, dict(self.record(), source_blob='d' * 40), dict(built, source_blob='x'),
                       dict(built, path=package.TABLE_PATH)):
            with self.subTest(sorted(record)), self.assertRaises(ValueError):
                package.check_record(record)


def listed(*items):
    return [{'sha': sha, 'commit': {'message': message}} for sha, message in items]


class TableForTheGameVersionTests(TableCase):
    """The launcher takes the table itself: the newest text revision that names the game's version."""

    ROWS = BuildFromTextTests.ROWS
    COMMITS = listed(('d' * 40, 'GameVer 3.9.0 | ResVer 3.9.2 | Changelist 1 | Release'),
                     (TEXT_REVISION, 'GameVer 3.8.0 | ResVer 3.8.9 | Changelist 2 | Release\n\nbody'),
                     ('e' * 40, 'GameVer 3.8.0 | ResVer 3.8.6 | Changelist 3 | Release'),
                     ('f' * 40, 'Update README'))

    def github(self, commits=None):
        return FakeTextGitHub(text_json(self.ROWS), commits=self.COMMITS if commits is None else commits)

    def test_the_newest_revision_of_the_same_game_version_is_chosen(self):
        self.assertEqual(tool.find_revision(self.github(), '3.8.0'), TEXT_REVISION)
        self.assertEqual(tool.find_revision(self.github(), '3.9.0'), 'd' * 40)

    def test_a_version_the_source_does_not_name_is_refused_not_guessed(self):
        for version in ('3.7.0', '3.8.1', '13.8.0'):
            with self.subTest(version), self.assertRaisesRegex(ValueError, 'нет версии ' + version):
                tool.find_revision(self.github(), version)
        for version in (None, '', '3.8', '3.8.0 ', 'GameVer 3.8.0', '../3.8.0'):
            with self.subTest(version), self.assertRaises(ValueError):
                tool.find_revision(self.github(), version)

    def test_a_malformed_listing_is_refused(self):
        good = self.COMMITS[1]
        bad = {'not a list': {'sha': TEXT_REVISION}, 'entry': ['x'], 'short sha': listed(('c' * 39, good['commit']['message'])),
               'no message': [{'sha': TEXT_REVISION, 'commit': {}}], 'no commit': [{'sha': TEXT_REVISION}],
               'too long': [good] * 31}
        for name, commits in bad.items():
            with self.subTest(name), self.assertRaises(ValueError):
                tool.find_revision(self.github(commits), '3.8.0')

    def test_update_builds_the_table_once(self):
        github = self.github()
        first = tool.update(self.native, '3.8.0', opener=github)
        self.assertTrue(first['changed'])
        self.assertEqual(first['record']['revision'], TEXT_REVISION)
        self.assertEqual(first['record']['repository'], 'Arikatsu/WutheringWaves_Data')
        self.assertEqual(package.table_record(self.native), first['record'])
        package.check_inputs(self.inputs)
        again = self.github()
        second = tool.update(self.native, '3.8.0', opener=again)
        self.assertEqual(second, {'record': first['record'], 'changed': False})
        self.assertEqual(again.urls, [TEXT_API + '/commits?path=Textmaps/en/multi_text/MultiText.json&per_page=30'])

    def test_update_without_the_version_changes_nothing(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            tool.update(self.native, '3.7.0', opener=self.github())
        self.assertEqual(self.snapshot(), before)
