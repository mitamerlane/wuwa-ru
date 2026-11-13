"""Take a newer public English table for the Play package: one file of the pinned public source, by Git blob.

`fetch` resolves a revision of Loping151/WWUIResourcesFull (the default branch head, or a full SHA-1 given),
walks its tree to Text/ConfigDB/en/lang_multi_text.db and downloads only that file. It is installed only if size
and Git blob match the tree; native_package.install_table then checks it again and writes table.json.
`show` prints the table Data/Native is set up with. Run with the app closed. Never reads or writes the game,
never uses keys; this is public data, not a game file. A newer table needs the game version accepted again.

`build --revision SHA` is for the days when that source has no table for a new game version yet: the same table
is built from Textmaps/en/multi_text/MultiText.json of Arikatsu/WutheringWaves_Data at that revision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wuwaru import native_package as package  # noqa: E402
from wuwaru.table_fetch import (API, MAX_JSON, NAME, RAW, SHA, _json, _open, build, download,  # noqa: E402,F401
                                fetch, locate, resolve)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('fetch', 'build', 'show'))
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument('--root', help='portable folder WuWaRussian (uses Data/Native)')
    where.add_argument('--native', help='Data/Native folder')
    parser.add_argument('--revision', help='full SHA-1 of the source revision; default: the default branch head')
    args = parser.parse_args(argv)
    native = Path(args.native) if args.native else Path(args.root) / 'Data' / 'Native'
    if args.command in ('fetch', 'build'):
        if args.command == 'build' and not args.revision:
            parser.error('build: нужна --revision, полный SHA-1 ревизии Arikatsu/WutheringWaves_Data')
        record = (build if args.command == 'build' else fetch)(native, args.revision)
        print('Таблица принята:', package.table_label(record))
        print('Коммит:', record['data_version'] or 'UNKNOWN')
        print('SHA-256:', record['sha256'])
        print('«Играть» соберёт пакет заново. Версию игры с новой таблицей нужно принять заново.')
    else:
        record = package.table_record(native)
        print(package.table_label(record), '(закреплённый снимок)' if record == package.PINNED_TABLE else '')
        print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    os.environ.setdefault('PYTHONUTF8', '1')
    raise SystemExit(main())
