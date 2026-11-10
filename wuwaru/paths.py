"""Conservative Windows path rules, also testable on other platforms."""
import re
from pathlib import Path, PureWindowsPath


class UnsafePath(ValueError):
    pass


def relative_parts(value: str) -> tuple[str, ...]:
    normalized = value.replace('\\', '/')
    parts = tuple(normalized.split('/'))
    if (not normalized or PureWindowsPath(value).drive or normalized.startswith('/')
            or any(p in ('', '.', '..') or p.endswith((' ', '.'))
                   or re.search(r'[<>:"|?*\x00-\x1f]', p)
                   or p.split('.')[0].upper() in
                   {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                    *(f'LPT{i}' for i in range(1, 10))} for p in parts)):
        raise UnsafePath(f'Недопустимый относительный путь: {value!r}')
    return parts


def reject_links(path: Path) -> None:
    """Reject junctions and symlinks in every existing ancestor, before resolve()."""
    for part in (path, *path.parents):
        # A junction exists only on Windows; Python before 3.12, as on a Linux server, has no such check.
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise UnsafePath(f'Ссылки и junction не поддерживаются: {part}')


def inside(root: Path, relative: str) -> Path:
    path = root.joinpath(*relative_parts(relative)).absolute()
    reject_links(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise UnsafePath(f'Путь вышел за пределы установки: {relative}')
    return path
