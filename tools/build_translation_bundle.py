"""Ready-translation bundle for the installer: python tools/build_translation_bundle.py ...

Produces build/bundle/seed/ laid out like the portable app root:

  Data/settings.json                         opens the bundled project on first start
  Data/Projects/WuWaRussian-RU/              slim copy of a translation project
  Data/Native/lang_multi_text.en.db          pinned public English table (SHA-256 checked)
  Data/Native/NotoSans-600.ttf               pinned font (SHA-256 checked)
  Data/Native/staging/<key>.pak + package.json   package built and verified by native_package

The installer copies seed/ only where nothing exists yet, so a user's own project and edits
are never overwritten. The source project is opened read-only; the game is never touched.
Nothing from here is committed: build/ is ignored.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wuwaru import native_package  # noqa: E402
from wuwaru.portable import DEFAULT_NATIVE_KINDS  # noqa: E402

PROJECT = 'WuWaRussian-RU'
# Not needed to play or to keep editing: edit history, the provider cache and spend records.
DROP = ('history', 'cache', 'usage')


def slim_project(source: Path, destination: Path) -> dict:
    """Read-only copy of `source` without DROP tables' rows, compacted."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    src = sqlite3.connect(Path(source).absolute().as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        src.close()
    try:
        tables = {row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in DROP:
            if name in tables:
                dst.execute(f'DELETE FROM "{name}"')
        dst.commit()
        dst.execute('VACUUM')
        if dst.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise SystemExit('Копия проекта не прошла integrity_check')
        stats = dict(dst.execute(
            "SELECT status, count(*) FROM records WHERE active=1 AND target IS NOT NULL GROUP BY status"))
        dst.execute('PRAGMA journal_mode=DELETE')  # one self-contained file, no -wal/-shm to ship
    finally:
        dst.close()
    for extra in ('-wal', '-shm'):
        destination.with_name(destination.name + extra).unlink(missing_ok=True)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--project-db', required=True, help='translations.sqlite to ship (read-only)')
    ap.add_argument('--table', required=True, help='public lang_multi_text.db, SHA-256 014b0975…')
    ap.add_argument('--font', required=True, help='NotoSans-600.ttf, SHA-256 c546e2e5…')
    ap.add_argument('--out', default=str(ROOT / 'build/bundle'))
    args = ap.parse_args()

    out = Path(args.out)
    seed = out / 'seed'
    shutil.rmtree(seed, ignore_errors=True)
    data = seed / 'Data'
    project_db = data / 'Projects' / PROJECT / 'translations.sqlite'
    stats = slim_project(Path(args.project_db), project_db)
    native = data / 'Native'
    native_package.install_inputs(Path(args.table), Path(args.font), native)
    report = native_package.build_package(project_db, native, DEFAULT_NATIVE_KINDS)
    (data / 'settings.json').write_text(json.dumps({
        'schema_version': 1,
        'last_project': f'Data\\Projects\\{PROJECT}',
        'last_project_relative': True,
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    summary = {'schema_version': 1, 'project': PROJECT, 'records': stats,
               'package': {k: report[k] for k in ('key', 'pak_sha256', 'pak_sha1', 'pak_bytes',
                                                  'cells', 'left_out', 'kinds')}}
    (out / 'bundle.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    size = sum(f.stat().st_size for f in seed.rglob('*') if f.is_file())
    print(f'OK {seed} ({size // 2**20} MiB): ячеек в пакете {report["cells"]}, '
          f'pak {report["pak_bytes"] // 2**10} KiB')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
