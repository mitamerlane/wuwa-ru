"""Self-authored SQLite and sfnt bytes only; never touches an installation or the public snapshot."""
import hashlib
import json
import sqlite3
from threading import Event
from types import SimpleNamespace
from unittest import mock

from tests.test_discovery import FixtureCase
from tests.font_fixture import font
from tests.test_sqlite_source import database
from wuwaru import native_package as package
from wuwaru.manifest import Cancelled
from wuwaru.pak_reader import V12ArchiveReader
from wuwaru.translation_db import TranslationDB

TABLE = {'PrefabTextItem_1_Text': 'Confirm', 'PrefabTextItem_2_Text': 'Back now', 'PrefabTextItem_3_Text': 'Old text',
         'PrefabTextItem_4_Text': '*****', 'PrefabTextItem_5_Text': 'Odd one', 'PrefabTextItem_6_Text': 'Press it'}
PROJECT = dict(TABLE, PrefabTextItem_3_Text='New text', PrefabTextItem_4_Text='Masked english')
TARGETS = {'PrefabTextItem_1_Text': ('Подтвердить', 'MACHINE_TRANSLATED'),
           'PrefabTextItem_2_Text': ('Назад', 'UNTRANSLATED'),
           'PrefabTextItem_3_Text': ('Новый текст', 'MACHINE_TRANSLATED'),
           'PrefabTextItem_4_Text': ('Скрытое', 'MACHINE_TRANSLATED'),
           'PrefabTextItem_5_Text': ('Странный ₽', 'MACHINE_TRANSLATED'),
           'PrefabTextItem_6_Text': ('Нажмите', 'REVIEWED')}


class PackageCase(FixtureCase):
    def setUp(self):
        super().setUp()
        self.native = self.home / 'native'
        self.native.mkdir()
        self.inputs = package.NativeInputs.in_folder(self.native)
        with sqlite3.connect(self.inputs.table) as db:
            db.execute('CREATE TABLE MultiText(Id TEXT PRIMARY KEY, Content TEXT, RedirectDbIndex INTEGER)')
            db.executemany('INSERT INTO MultiText VALUES (?,?,?)',
                           ((key, value, int(key == 'PrefabTextItem_4_Text')) for key, value in TABLE.items()))
        db.close()
        self.inputs.font.write_bytes(font())
        self.pins = (package.sha256(self.inputs.table), package.sha256(self.inputs.font))
        self.core = TranslationDB(self.home / 'work')
        self.addCleanup(self.core.close)
        self.core.import_source('lang.db', database(self.home / 'source.db', tuple(PROJECT.items())))
        for identifier, (target, status) in TARGETS.items():
            self.core.db.execute('UPDATE records SET target=?,translated_hash=source_hash,status=? WHERE id_value=?',
                                 (target, status, identifier))
        self.core.db.commit()
        self.project = self.home / 'work/translations.sqlite'

    def build(self, **extra):
        return package.build_package(self.project, self.native, ('ui',), pins=self.pins, **extra)

    def set_target(self, identifier, target):
        self.core.db.execute('UPDATE records SET target=? WHERE id_value=?', (target, identifier))
        self.core.db.commit()

    def key(self):
        return package.current_key(self.project, self.native, ('ui',), pins=self.pins)


class SelectTests(PackageCase):
    def test_select_cells_reasons(self):
        cells, left_out = package.select_cells(self.project, self.inputs, ('ui',))
        self.assertEqual([cell['id'] for cell in cells], ['PrefabTextItem_1_Text', 'PrefabTextItem_6_Text'])
        self.assertEqual(left_out, dict(ENGLISH_DIFFERS=1, MASKED=1, NO_GLYPH=1, STATUS_UNTRANSLATED=1))

    def test_font_coverage_parses_each_letter_once(self):
        has_glyph = package.font_coverage(font())
        with mock.patch.object(package, 'covered', wraps=package.covered) as parse:
            self.assertTrue(has_glyph('Подтвердить'))
            self.assertTrue(has_glyph('Подтвердить'))
            self.assertTrue(has_glyph('твердо'))
            self.assertFalse(has_glyph('Странный ₽'))
            self.assertFalse(has_glyph('₽'))
        self.assertEqual(parse.call_count, 2)  # 'Подтвердить', then only the new letters of 'Странный ₽'
        self.assertEqual(set(parse.call_args_list[1].args[1]), {ord(c) for c in 'Странный₽'} - {ord(c) for c in 'Подтвердить'})

    def test_select_cells_reuses_the_last_answer_only_for_the_same_rows(self):
        first = package.select_cells(self.project, self.inputs, ('ui',))
        with mock.patch.object(package, 'validate', wraps=package.validate) as check:
            again = package.select_cells(self.project, self.inputs, ('ui',))
            self.assertEqual(check.call_count, 0)
            self.assertEqual(again, first)
            again[0][0]['target'] = 'испорчено'  # the caller's copy, not the remembered one
            self.assertEqual(package.select_cells(self.project, self.inputs, ('ui',)), first)
            self.set_target('PrefabTextItem_6_Text', 'Нажми')
            changed = package.select_cells(self.project, self.inputs, ('ui',))
            self.assertGreater(check.call_count, 0)
        self.assertEqual(changed[0][1]['target'], 'Нажми')
        for font_bytes in (None, font() + b'\0'):  # other kinds, then the same kinds with another font
            if font_bytes:
                self.inputs.font.write_bytes(font_bytes)
            with mock.patch.object(package, 'validate', wraps=package.validate) as check:
                package.select_cells(self.project, self.inputs, ('ui', 'speaker'))
            self.assertGreater(check.call_count, 0)

    def test_a_sign_kept_from_the_english_line_does_not_block_the_cell(self):
        # The game already draws the English line with that sign; only signs the translation adds count.
        no_p = lambda text: 'P' not in text  # noqa: E731
        self.set_target('PrefabTextItem_6_Text', 'Нажмите P')
        cells, left_out = package.select_cells(self.project, self.inputs, ('ui',), has_glyph=no_p)
        self.assertIn('PrefabTextItem_6_Text', [cell['id'] for cell in cells])
        self.set_target('PrefabTextItem_1_Text', 'Подтвердить P')  # 'Confirm' has no P
        cells, left_out = package.select_cells(self.project, self.inputs, ('ui',), has_glyph=no_p)
        self.assertNotIn('PrefabTextItem_1_Text', [cell['id'] for cell in cells])
        self.assertEqual(left_out['NO_GLYPH'], 1)

    def test_build_key_stable_and_changes_with_target(self):
        first = self.key()
        self.assertEqual(first, self.key())
        self.assertRegex(first, '^[0-9a-f]{64}$')
        self.set_target('PrefabTextItem_1_Text', 'Другое')
        self.assertNotEqual(first, self.key())


class BuildTests(PackageCase):
    def test_build_package_rereads_and_skips_same_key(self):
        report = self.build()
        self.assertIs(report['rebuilt'], True)
        archive = self.native / 'staging' / (report['key'] + '.pak')
        self.assertEqual(len(V12ArchiveReader().list_entries(archive)), 2)
        raw = archive.read_bytes()
        self.assertEqual(report['pak_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(report['pak_sha1'], hashlib.sha1(raw).hexdigest().upper())
        saved = json.loads((self.native / 'package.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['cells'], 2)
        self.assertEqual(saved['kinds'], ['ui'])
        self.assertEqual(saved['left_out'], report['left_out'])
        before = archive.stat().st_mtime_ns
        again = self.build()
        self.assertIs(again['rebuilt'], False)
        self.assertEqual(archive.stat().st_mtime_ns, before)

    def test_installed_copy_counts_as_built(self):
        report = self.build()
        installed = self.home / 'installed.pak'
        archive = self.native / 'staging' / (report['key'] + '.pak')
        installed.write_bytes(archive.read_bytes())
        archive.unlink()
        self.assertIs(self.build(installed=installed)['rebuilt'], False)
        self.assertIs(self.build()['rebuilt'], True)

    def test_build_package_rejects_foreign_input(self):
        data = bytearray(self.inputs.table.read_bytes())
        data[-1] ^= 1
        self.inputs.table.write_bytes(bytes(data))
        with self.assertRaisesRegex(ValueError, 'Pinned public snapshot changed'):
            self.build()

    def test_cancel_leaves_no_staging(self):
        event = Event()
        event.set()
        with self.assertRaises(Cancelled):
            self.build(cancelled=event)
        staging = self.native / 'staging'
        self.assertEqual(list(staging.iterdir()) if staging.exists() else [], [])
        self.assertFalse((self.native / 'package.json').exists())

    def test_cancel_during_write_removes_partial_files(self):
        old = self.build()['key']
        self.set_target('PrefabTextItem_1_Text', 'Другое')
        event = Event()
        with mock.patch.object(package, 'write_archive', side_effect=lambda *a, **k: event.set() or package.check_cancel(event)):
            with self.assertRaises(Cancelled):
                self.build(cancelled=event)
        # The finished package of the previous key stays; no partial file or work folder is left.
        self.assertEqual([item.name for item in (self.native / 'staging').iterdir()], [old + '.pak'])

    def test_new_key_leaves_exactly_one_pak_and_sweeps_leftovers(self):
        first = self.build()
        staging = self.native / 'staging'
        (staging / 'x.pak.part').write_bytes(b'1')
        (staging / '.work-dead').mkdir()
        self.set_target('PrefabTextItem_1_Text', 'Другое')
        second = self.build()
        self.assertNotEqual(first['key'], second['key'])
        self.assertEqual([item.name for item in staging.iterdir()], [second['key'] + '.pak'])

    def test_corrupted_staging_file_is_rebuilt(self):
        report = self.build()
        archive = self.native / 'staging' / (report['key'] + '.pak')
        archive.write_bytes(archive.read_bytes()[:-10])
        again = self.build()
        self.assertIs(again['rebuilt'], True)
        self.assertEqual(package.sha256(archive), again['pak_sha256'])
        self.assertEqual(len(V12ArchiveReader().list_entries(archive)), 2)

    def test_refuses_without_space(self):
        need = 2 * (self.inputs.table.stat().st_size + self.inputs.font.stat().st_size)
        with mock.patch('shutil.disk_usage', return_value=SimpleNamespace(free=need - 1)):
            with self.assertRaises(OSError) as raised:
                self.build()
        self.assertIn(str(need), str(raised.exception))
        self.assertIn(str(need - 1), str(raised.exception))
        self.assertEqual(list((self.native / 'staging').iterdir()), [])


class InputTests(PackageCase):
    def picked(self):
        folder = self.home / 'picked'
        folder.mkdir()
        table, font_file = folder / 'any-name.db', folder / 'font.ttf'
        table.write_bytes(self.inputs.table.read_bytes())
        font_file.write_bytes(self.inputs.font.read_bytes())
        return table, font_file

    def test_install_inputs_copies_under_exact_names_after_hash_check(self):
        table, font_file = self.picked()
        target = self.home / 'Data/Native'
        installed = package.install_inputs(table, font_file, target, pins=self.pins)
        self.assertEqual(installed, package.NativeInputs.in_folder(target))
        self.assertEqual(package.sha256(installed.table), self.pins[0])
        self.assertEqual(package.sha256(installed.font), self.pins[1])
        self.assertEqual(sorted(path.name for path in target.iterdir()),
                         ['NotoSans-600.ttf', 'lang_multi_text.en.db'])

    def test_wrong_hash_copies_nothing_and_keeps_existing_inputs(self):
        table, font_file = self.picked()
        font_file.write_bytes(font_file.read_bytes() + b'x')
        before = {path.name: path.read_bytes() for path in self.native.iterdir()}
        with self.assertRaisesRegex(ValueError, 'шрифт'):
            package.install_inputs(table, font_file, self.native, pins=self.pins)
        self.assertEqual({path.name: path.read_bytes() for path in self.native.iterdir()}, before)
        table.write_bytes(table.read_bytes() + b'x')
        empty = self.home / 'empty'
        with self.assertRaisesRegex(ValueError, 'таблица'):
            package.install_inputs(table, font_file, empty, pins=self.pins)
        self.assertFalse(empty.exists())
