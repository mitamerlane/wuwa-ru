import tempfile
import unittest
from pathlib import Path

from wuwaru.discovery import discover, inspect_installation


def fixture(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'Wuthering Waves.exe').write_bytes(b'synthetic, not executable')
    (root / 'Client/Content/Paks').mkdir(parents=True)
    (root / 'Client/Content/Paks/base.pak').write_bytes(b'synthetic PAK inventory only')
    mounts = root / 'Client/Saved/Resources/3.7.0/Mount'
    mounts.mkdir(parents=True)
    (mounts / 'MountResource.txt').write_text(
        '::Mount::\nResource/3.7.16/patch,20,ABC,DEF,,\n::Del::\n', encoding='utf-8')
    (mounts / 'MountLang_en.txt').write_text('::Mount::\n::Del::\n', encoding='utf-8')
    patch = mounts.parent / 'Resource/3.7.16/patch.pak'
    patch.parent.mkdir(parents=True)
    patch.write_bytes(b'patch fixture')
    return root


class FixtureCase(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / '.test-tmp'
        temporary_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)


class DiscoveryTests(FixtureCase):
    def test_explicit_path_and_unknown_client(self):
        game = fixture(self.home / 'Игра')
        result = discover(explicit=[game], system=False)
        self.assertEqual([i.root for i in result.installations], [game.resolve()])
        self.assertIsNone(result.installations[0].client_version)

    def test_rejects_non_game(self):
        with self.assertRaises(ValueError):
            inspect_installation(self.home)

    def test_steam_additional_library_and_build_separate_from_client(self):
        steam = self.home / 'Steam'
        library = self.home / 'Библиотека'
        game = fixture(library / 'steamapps/common/Wuthering Waves')
        (steam / 'steamapps').mkdir(parents=True)
        escaped = str(library).replace('\\', '\\\\')
        (steam / 'steamapps/libraryfolders.vdf').write_text(
            f'"libraryfolders" {{ "0" {{ "path" "{escaped}" }} }}', encoding='utf-8')
        (library / 'steamapps/appmanifest_3513350.acf').write_text(
            '"AppState" { "appid" "3513350" "installdir" "Wuthering Waves" '
            '"buildid" "12345" "LastOwner" "DO_NOT_EXPORT" }', encoding='utf-8')
        result = discover(steam_roots=[steam], system=False)
        self.assertEqual(len(result.installations), 1)
        self.assertEqual(result.installations[0].root, game.resolve())
        self.assertEqual(result.installations[0].steam_build_id, '12345')
        self.assertNotIn('DO_NOT_EXPORT', str(result))

    def test_uninstall_location_and_deduplication(self):
        game = fixture(self.home / 'Kuro Game')
        result = discover(explicit=[game], uninstall_locations=[game], system=False)
        self.assertEqual(len(result.installations), 1)
        self.assertEqual(set(result.installations[0].origins), {'explicit', 'uninstall_registry'})

    def test_steam_path_traversal_reported(self):
        steam = self.home / 'Steam'
        apps = steam / 'steamapps'
        apps.mkdir(parents=True)
        (apps / 'appmanifest_3513350.acf').write_text(
            '"AppState" { "appid" "3513350" "installdir" "../escape" }')
        result = discover(steam_roots=[steam], system=False)
        self.assertFalse(result.installations)
        self.assertTrue(result.warnings)

    def test_malformed_steam_manifest_is_not_silently_ignored(self):
        steam = self.home / 'Steam'
        apps = steam / 'steamapps'
        apps.mkdir(parents=True)
        (apps / 'appmanifest_3513350.acf').write_text('"AppState" {')
        result = discover(steam_roots=[steam], system=False)
        self.assertFalse(result.installations)
        self.assertTrue(result.warnings)


class NestedLayoutTests(FixtureCase):
    """Since 3.8 the Steam folder holds the Kuro launcher and the game one level down."""

    def test_the_game_under_the_launcher_folder_is_the_installation(self):
        top = self.home / 'steamapps/common/Wuthering Waves'
        game = fixture(top / 'Wuthering Waves Game')
        (top / 'launcher.exe').write_bytes(b'synthetic, not executable')
        (top / 'Client/Saved/Resources/3.7.0/Mount').mkdir(parents=True)  # leftovers of the old layout
        self.assertEqual(inspect_installation(top).root, game.resolve())
        self.assertEqual(inspect_installation(game).root, game.resolve())
        (self.home / 'steamapps/appmanifest_3513350.acf').write_text(
            '"AppState" { "appid" "3513350" "installdir" "Wuthering Waves" "buildid" "77" }', encoding='utf-8')
        found = discover(steam_roots=[self.home], system=False).installations
        self.assertEqual([(item.root, item.steam_build_id) for item in found], [(game.resolve(), '77')])

    def test_a_folder_without_the_game_is_still_refused(self):
        top = self.home / 'Wuthering Waves'
        (top / 'Wuthering Waves Game').mkdir(parents=True)
        with self.assertRaises(ValueError):
            inspect_installation(top)
