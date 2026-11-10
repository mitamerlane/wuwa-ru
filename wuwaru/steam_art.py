"""Find the game's cover in the local Steam cache. Read-only: only stats files, never opens them."""
import os
from collections.abc import Iterable
from pathlib import Path

from .paths import UnsafePath, reject_links, relative_parts

HERO_NAME = 'library_hero.jpg'


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _candidates(base: Path) -> Iterable[Path]:
    # Depth <= 2 below the app folder: base/<name> and base/<hash dir>/<name>.
    try:
        entries = list(os.scandir(base))
    except OSError:
        return
    for entry in entries:
        path = Path(entry.path)
        if _is_link(path):
            continue
        if entry.name == HERO_NAME:
            yield path
        elif entry.is_dir(follow_symlinks=False):
            yield path / HERO_NAME


def find_hero(steam_roots: Iterable[Path], app_id: str = '3513350',
              limit: int = 8 * 1024 * 1024) -> Path | None:
    """Newest-by-mtime library hero image under any Steam root, or None. Never raises for a missing cache."""
    try:
        relative_parts(app_id)
    except UnsafePath:
        return None
    best: tuple[float, Path] | None = None
    for root in steam_roots:
        base = Path(root) / 'appcache' / 'librarycache' / app_id
        try:
            reject_links(base)
        except UnsafePath:
            continue
        for path in _candidates(base):
            try:
                if _is_link(path) or not path.is_file():
                    continue
                info = path.stat()
            except OSError:
                continue
            if info.st_size > limit:
                continue
            if best is None or info.st_mtime > best[0]:
                best = (info.st_mtime, path)
    return best[1] if best else None
