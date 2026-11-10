"""Read-only discovery through explicit roots, Steam and uninstall registry."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .paths import inside, reject_links

MAX_METADATA = 2 * 1024 * 1024
STEAM_APP_ID = '3513350'


@dataclass
class Installation:
    root: Path
    origins: list[str] = field(default_factory=list)
    client_version: str | None = None
    steam_build_id: str | None = None


@dataclass
class DiscoveryResult:
    installations: list[Installation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def read_metadata(path: Path) -> str:
    reject_links(path)
    with path.open('rb') as stream:
        raw = stream.read(MAX_METADATA + 1)
    if len(raw) > MAX_METADATA:
        raise ValueError(f'Слишком большой файл метаданных: {path.name}')
    return raw.decode('utf-8-sig', errors='strict')


def parse_vdf(text: str) -> dict:
    """Subset of Valve KeyValues used by local manifests; no escape evaluation."""
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[{}]|//[^\n]*|\S+', text)
    tokens = [t for t in tokens if not t.startswith('//')]
    pos = 0

    def word(token: str) -> str:
        if not (token.startswith('"') and token.endswith('"')):
            raise ValueError('Неподдерживаемый формат Steam KeyValues')
        return re.sub(r'\\([\\"])', r'\1', token[1:-1])

    def block(nested: bool = False, depth: int = 0) -> dict:
        nonlocal pos
        if depth > 16:
            raise ValueError('Слишком глубокий Steam KeyValues')
        result = {}
        while pos < len(tokens):
            token = tokens[pos]
            pos += 1
            if token == '}':
                if nested:
                    return result
                raise ValueError('Лишняя скобка Steam KeyValues')
            key = word(token).casefold()
            if pos == len(tokens) or key in result:
                raise ValueError('Незавершённый или дублирующийся Steam KeyValues')
            value = tokens[pos]
            pos += 1
            result[key] = block(True, depth + 1) if value == '{' else word(value)
        if nested:
            raise ValueError('Незакрытый блок Steam KeyValues')
        return result

    return block()


def inspect_installation(root: Path, origin: str = 'explicit') -> Installation:
    root = Path(root).absolute()
    reject_links(root)
    if (not inside(root, 'Wuthering Waves.exe').is_file()
            or not inside(root, 'Client/Content/Paks').is_dir()):
        raise ValueError(f'Нет признаков установки Wuthering Waves: {root}')
    # An executable's UE engine version is not necessarily the game client version.
    return Installation(root.resolve(), [origin])


def system_locations() -> tuple[list[Path], list[Path], list[str]]:
    steam, games, warnings = [], [], []
    if os.name != 'nt':
        return steam, games, warnings
    import winreg

    for hive, key, field_name in (
        (winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
        (winreg.HKEY_LOCAL_MACHINE, r'Software\WOW6432Node\Valve\Steam', 'InstallPath'),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, field_name)
                if isinstance(value, str) and value:
                    steam.append(Path(value))
        except FileNotFoundError:
            continue
        except OSError as exc:
            warnings.append(f'Steam registry: {exc}')
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for prefix in ('Software', r'Software\WOW6432Node'):
            try:
                with winreg.OpenKey(hive, prefix + r'\Microsoft\Windows\CurrentVersion\Uninstall') as handle:
                    for i in range(winreg.QueryInfoKey(handle)[0]):
                        try:
                            with winreg.OpenKey(handle, winreg.EnumKey(handle, i)) as app:
                                name, _ = winreg.QueryValueEx(app, 'DisplayName')
                                if isinstance(name, str) and 'wuthering waves' in name.casefold():
                                    location, _ = winreg.QueryValueEx(app, 'InstallLocation')
                                    if isinstance(location, str) and location.strip():
                                        games.append(Path(location))
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            warnings.append(f'Uninstall entry: {exc}')
            except FileNotFoundError:
                continue
            except OSError as exc:
                warnings.append(f'Uninstall registry: {exc}')
    for variable in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        base = os.environ.get(variable)
        if base:
            folder = Path(base)
            games.extend((folder / 'Wuthering Waves',
                          folder / 'Wuthering Waves Game',
                          folder / 'Kuro Games' / 'Wuthering Waves',
                          folder / 'Kuro Games' / 'Wuthering Waves Game'))
    return steam, games, warnings


def discover(*, explicit: Iterable[Path] = (), steam_roots: Iterable[Path] = (),
             uninstall_locations: Iterable[Path] = (), system: bool = True) -> DiscoveryResult:
    result = DiscoveryResult()
    roots, locations = list(steam_roots), list(uninstall_locations)
    if system:
        found_roots, found_locations, warnings = system_locations()
        roots.extend(found_roots)
        locations.extend(found_locations)
        result.warnings.extend(warnings)
    candidates = [(Path(p), 'explicit', None) for p in explicit]
    candidates.extend((Path(p), 'uninstall_registry', None) for p in locations)
    libraries = set()
    for root in roots:
        root = Path(root).absolute()
        libraries.add(root)
        try:
            folders = inside(root, 'steamapps/libraryfolders.vdf')
            if folders.is_file():
                values = parse_vdf(read_metadata(folders)).get('libraryfolders', {})
                if not isinstance(values, dict):
                    raise ValueError('Некорректный список Steam libraries')
                for value in values.values():
                    path = value.get('path') if isinstance(value, dict) else None
                    if isinstance(path, str) and Path(path).is_absolute():
                        libraries.add(Path(path))
        except (OSError, ValueError) as exc:
            result.warnings.append(str(exc))
    for library in sorted(libraries):
        try:
            manifest = inside(library, f'steamapps/appmanifest_{STEAM_APP_ID}.acf')
            if not manifest.is_file():
                continue
            app = parse_vdf(read_metadata(manifest)).get('appstate', {})
            if not isinstance(app, dict) or app.get('appid') != STEAM_APP_ID:
                raise ValueError('Неверный Steam AppID')
            folder = app.get('installdir')
            if not isinstance(folder, str):
                raise ValueError('Steam installdir отсутствует')
            game = inside(inside(library, 'steamapps/common'), folder)
            build = app.get('buildid')
            if build is not None and (not isinstance(build, str) or not build.isdecimal()):
                raise ValueError('Некорректный Steam buildid')
            candidates.append((game, 'steam_manifest', build))
        except (OSError, ValueError) as exc:
            result.warnings.append(f'Steam: {exc}')
    unique = {}
    for path, origin, build in candidates:
        try:
            item = inspect_installation(path, origin)
            existing = unique.setdefault(item.root, item)
            if origin not in existing.origins:
                existing.origins.append(origin)
            if build is not None:
                existing.steam_build_id = build
        except (OSError, ValueError) as exc:
            result.warnings.append(str(exc))
    result.installations = [unique[p] for p in sorted(unique)]
    result.warnings = list(dict.fromkeys(result.warnings))
    return result
