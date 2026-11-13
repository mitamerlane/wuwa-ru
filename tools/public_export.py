"""Copy the public face of this repository into a separate checkout and refuse on any secret.

    python -m tools.public_export --out ..\\wuwa-ru     # then review, commit and push there

Only git-tracked files from the allowlist below are copied; `public/` (README, docs, .gitignore of
the public repository) is laid over them. Research notes, reports, agent instructions, local data
and experiments stay here. Files in --out that are not part of the new export are removed, except
`.git`. Every exported file is scanned; one hit stops the export before anything is overwritten.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = 'public'
FILES = ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml', 'wuwaru_desktop.py',
         'research/public-text-sources.json', 'docs/SELF_CORE_FORMAT.md')
DIRS = ('wuwaru/', 'installer/', 'packaging/')
# Build/release, the public English table and the translation pipeline. Benchmarks, one-off probes,
# experiment scripts for the developer's machine and the translation server stay private.
TOOLS = ('build_installer.py', 'build_translation_bundle.py', 'make_icons.py', 'native_table.py',
         'public_export.py', 'release.py', 'fetch_public_text.py', 'verify_public_text.py', 'run_wave.py')
# A test is exported only if every tools/tests module it imports is exported too (see `select`).
TEST_IMPORT = re.compile(r'^\s*(?:from\s+(tools|tests)(?:\.(\w+))?\s+import\s+([\w ,]+)|import\s+(tools|tests)\.(\w+))',
                         re.MULTILINE)
SECRETS = [
    (re.compile(rb'sk-[A-Za-z0-9_\-]{20,}'), 'API key (sk-)'),
    (re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}'), 'GitHub token'),
    (re.compile(rb'github_pat_[A-Za-z0-9_]{30,}'), 'GitHub token'),
    (re.compile(rb'AKIA[0-9A-Z]{16}'), 'AWS key'),
    (re.compile(rb'xox[abprs]-[A-Za-z0-9-]{10,}'), 'Slack token'),
    (re.compile(rb'\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}'), 'Telegram bot token'),
    (re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----'), 'private key'),
    (re.compile(rb'(?i)bearer\s+[A-Za-z0-9._\-]{24,}'), 'bearer token'),
    (re.compile(rb'(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*["\'][A-Za-z0-9_\-./+]{16,}["\']'),
     'assigned secret'),
]
# Reviewed false positives, exact text only. The experiment's approval word is a fixed string the user
# types to confirm a run (printed in its own instructions), not a credential.
ALLOWED = frozenset({('tools/native_pak_experiment.py', b"TOKEN = '8c247bd1634f4ff09b3a5f8b6a2d10e7'")})
# Words that name the developer (e-mail, nickname), one per line, kept beside the signing key and never in Git.
PERSONAL = Path('.wuwaru-release') / 'personal.txt'
FORBIDDEN_NAMES = re.compile(r'(?i)(^|/)(\.env[^/]*|.*\.(pem|key|pfx|p12|sqlite3?|db|pak|sig))$')
MAX_FILE = 16 * 1024 * 1024


def tracked() -> list[str]:
    out = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT, capture_output=True, check=True).stdout
    return [name for name in out.decode('utf-8').split('\0') if name]


def _test_needs(path: Path) -> set[str]:
    """'tools/x.py' and 'tests/y.py' modules a test file imports."""
    needs = set()
    for match in TEST_IMPORT.finditer(path.read_text(encoding='utf-8')):
        package, module, names, package2, module2 = match.groups()
        if package2:
            needs.add(f'{package2}/{module2}.py')
        elif module:
            needs.add(f'{package}/{module}.py')
        else:
            needs.update(f'{package}/{item.split(" as ")[0].strip()}.py' for item in names.split(','))
    return needs


def select(names: list[str]) -> list[str]:
    keep = [name for name in names if name in FILES or name.startswith(DIRS)
            or (name.startswith('tools/') and name[len('tools/'):] in TOOLS)]
    tests = {name for name in names if name.count('/') == 1 and name.endswith('.py')
             and name.split('/')[0] in ('tests', 'gui_tests')}
    needs = {name: _test_needs(ROOT / name) for name in tests}
    available = set(keep) | tests
    changed = True
    while changed:  # drop tests whose imports are gone, then tests that import those
        changed = False
        for name in sorted(tests):
            if not needs[name] <= available:
                tests.discard(name)
                available.discard(name)
                changed = True
    return keep + sorted(tests)


def overlay() -> dict[str, Path]:
    base = ROOT / OVERLAY
    return {path.relative_to(base).as_posix(): path for path in sorted(base.rglob('*')) if path.is_file()}


def personal() -> list[tuple[re.Pattern[bytes], str]]:
    """What would name the developer in an exported file: read from this machine, never written in the code."""
    home = Path.home()
    found = [(re.compile(rb'(?i)Users[\\/]+' + re.escape(home.name.encode('utf-8'))), 'personal path')]
    try:
        lines = (home / PERSONAL).read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        word = line.strip()
        if word and not word.startswith('#'):
            found.append((re.compile(b'(?i)' + re.escape(word.encode('utf-8'))), 'personal marker'))
    return found


def scan(files: dict[str, Path], markers: list[tuple[re.Pattern[bytes], str]] | None = None) -> list[str]:
    own = personal() if markers is None else markers
    hits = []
    for name, path in files.items():
        if FORBIDDEN_NAMES.search(name):
            hits.append(f'{name}: запрещённый тип файла')
            continue
        size = path.stat().st_size
        if size > MAX_FILE:
            hits.append(f'{name}: слишком большой ({size // 2**20} МиБ)')
            continue
        data = path.read_bytes()
        for pattern, label in [*SECRETS, *own]:
            if name == 'tools/public_export.py' and (pattern, label) not in own:
                continue  # the secret patterns themselves; personal markers are checked here too
            for match in pattern.finditer(data):
                if (name, match.group(0)) in ALLOWED:
                    continue
                line = data.count(b'\n', 0, match.start()) + 1
                hits.append(f'{name}:{line}: {label}')
    return hits


def export(out: Path) -> list[str]:
    out = out.resolve()
    if out == ROOT or ROOT in out.parents:
        raise SystemExit('Папка экспорта должна быть вне этого репозитория.')
    files = {name: ROOT / name for name in select(tracked())}
    files.update(overlay())
    hits = scan(files)
    if hits:
        raise SystemExit('Экспорт остановлен, найдено похожее на секрет:\n  ' + '\n  '.join(hits))
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(out.rglob('*'), reverse=True):
        rel = path.relative_to(out)
        if rel.parts[0] == '.git' or rel.as_posix() in files:
            continue
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for name, source in files.items():
        dest = out / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    return sorted(files)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Чистый экспорт для публичного репозитория')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    names = export(args.out)
    print(f'OK: {len(names)} файлов → {args.out.resolve()}; секретов не найдено.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
