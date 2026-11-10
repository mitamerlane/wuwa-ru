"""Offline game package built from the project: patched English MultiText plus the unchanged font.

Moved from tools/native_text_batch.py, tools/native_font_probe.py and tools/native_waves_probe.py; the tools
re-export these names. Never reads or writes the game. A cell goes in only with the exact English of the pinned
table, RedirectDbIndex=0, a clean check and every letter in the font. Everything else stays English and is counted
by reason. Machine translation, not a release.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import tempfile
from threading import Event, Lock

from wuwaru.flow_context import text_kind
from wuwaru.manifest import check_cancel
from wuwaru.pak_builder import write_archive
from wuwaru.pak_reader import PREFIX, V12ArchiveReader
from wuwaru.paths import reject_links
from wuwaru.validation import validate

SOURCE_SHA256 = '014b09753e986bf04ca14ce953351a84c8f0348d95ac1adf16e25c26b7182a9a'
# The one public source a table may come from; only this file of it, by revision and Git blob.
TABLE_REPOSITORY = 'Loping151/WWUIResourcesFull'
TABLE_PATH = 'Text/ConfigDB/en/lang_multi_text.db'
TABLE_RECORD = 'table.json'
TABLE_MAX = 512 * 1024 * 1024
PINNED_TABLE = dict(repository=TABLE_REPOSITORY, path=TABLE_PATH, revision='30b665d7e783f8aedd8a9c2c4f17b7555eb2b10d',
                    blob='15e99ad486a7dc09d2edbcc0f3a10e81b7f94c8b', size=45834240, sha256=SOURCE_SHA256,
                    data_version='3.7: README counts', data_date='2026-09-30T06:52:00Z')
FONT_SHA256 = 'c546e2e5bf816d8d42fb8223fde67a4f3173cb0d5141597d0ae0949eecff16e8'
DATABASE = 'lang_multi_text.db'
DATABASE_ENTRY = PREFIX + DATABASE
# From third-party builders; one client read at this entry was observed, the index stays closed.
FONT_ENTRY = 'Client/Content/Aki/UI/Framework/LGUI/Font/LaguSansBold.ufont'
USABLE = ('MACHINE_TRANSLATED', 'REVIEWED', 'APPROVED')
STATUS = 'MACHINE_TRANSLATION_NOT_REVIEWED'
RULES_VERSION = 1
MARKUP = re.compile(r'<[^<>\r\n]*>|\{[^{}]*\}')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def visible(text):
    """Markup and placeholders take no width here, on either side of a comparison."""
    return MARKUP.sub('', text).replace('\\n', '\n').replace('\r\n', '\n').replace('\r', '\n').strip()


def added_signs(english, target):
    """Signs the translation brings in. The game already draws the English line with the rest
    (kaomoji, "■■■", fullwidth brackets) through its own fallback, so they cannot block a cell."""
    return ''.join(sorted(set(visible(target)) - set(visible(english))))


def font_tables(data):
    count = struct.unpack_from('>H', data, 4)[0]
    tables = {}
    for index in range(count):
        tag, _, offset, length = struct.unpack_from('>4sIII', data, 12 + 16 * index)
        if offset + length > len(data):
            raise ValueError('Damaged font table')
        tables[tag] = offset
    if b'fvar' in tables:
        raise ValueError('Variable fonts are not used in this probe')
    return tables


def covered(data, wanted):
    return set(glyph_ids(data, wanted))


def glyph_ids(data, wanted):
    """Glyph of each code point of `wanted` that has one; cmap formats 4 and 12, outlines stay uninterpreted."""
    base = font_tables(data)[b'cmap']
    found = {}
    for index in range(struct.unpack_from('>H', data, base + 2)[0]):
        platform, encoding, offset = struct.unpack_from('>HHI', data, base + 4 + 8 * index)
        if platform != 0 and (platform, encoding) not in ((3, 1), (3, 10)):
            continue
        table = base + offset
        layout = struct.unpack_from('>H', data, table)[0]
        if layout == 4:
            segments = struct.unpack_from('>H', data, table + 6)[0] // 2
            ends = struct.unpack_from('>%dH' % segments, data, table + 14)
            starts = struct.unpack_from('>%dH' % segments, data, table + 16 + 2 * segments)
            deltas = struct.unpack_from('>%dH' % segments, data, table + 16 + 4 * segments)
            ranges = table + 16 + 6 * segments
            offsets = struct.unpack_from('>%dH' % segments, data, ranges)
            for segment in range(segments):
                for point in wanted:
                    if starts[segment] <= point <= ends[segment]:
                        glyph = point
                        if offsets[segment]:
                            position = ranges + 2 * segment + offsets[segment] + 2 * (point - starts[segment])
                            glyph = struct.unpack_from('>H', data, position)[0]
                        if glyph and (glyph + deltas[segment]) & 0xFFFF:
                            found[point] = (glyph + deltas[segment]) & 0xFFFF
        elif layout == 12:
            for group in range(struct.unpack_from('>I', data, table + 12)[0]):
                first, last, glyph = struct.unpack_from('>III', data, table + 16 + 12 * group)
                found.update((point, glyph + point - first) for point in wanted
                             if first <= point <= last and glyph + point - first)
    return found


def open_read_only(path):
    return sqlite3.connect('file:' + path.as_posix() + '?mode=ro&immutable=1', uri=True)


def apply_entries(source, work, entries):
    """Byte copy first, then one transaction of exact-cell updates. No VACUUM, no rebuild."""
    reject_links(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as stream, work.open('xb') as output:
        shutil.copyfileobj(stream, output, 1024 * 1024)
    database = sqlite3.connect(work, isolation_level=None)
    try:
        database.execute('BEGIN IMMEDIATE')
        for entry in entries:
            row = database.execute('SELECT Content, RedirectDbIndex, typeof(Content), typeof(RedirectDbIndex) '
                                   'FROM MultiText WHERE Id=?', (entry['id'],)).fetchone()
            if row != (entry['source'], 0, 'text', 'integer'):
                raise ValueError('Cell differs from the reviewed source: ' + entry['id'])
            changed = database.execute('UPDATE MultiText SET Content=? WHERE Id=? AND Content=? AND RedirectDbIndex=0',
                                       (entry['target'], entry['id'], entry['source'])).rowcount
            if changed != 1:
                raise ValueError('Expected one changed row: ' + entry['id'])
        database.execute('COMMIT')
    finally:
        database.close()


def compare(source, work, entries):
    """Every row by rowid with storage classes; schema, pragmas and integrity of both files."""
    expected = {entry['id']: entry for entry in entries}
    query = 'SELECT rowid, Id, Content, RedirectDbIndex, typeof(Id), typeof(Content), typeof(RedirectDbIndex) FROM MultiText ORDER BY rowid'
    left, right = open_read_only(source), open_read_only(work)
    try:
        rows = changed = 0
        before, after = left.execute(query), right.execute(query)
        while True:
            one, two = before.fetchone(), after.fetchone()
            if one is None or two is None:
                if one is not two:
                    raise ValueError('Row count changed')
                break
            rows += 1
            if one != two:
                entry = expected.get(one[1])
                restored = two[:2] + (one[2],) + two[3:]
                if entry is None or restored != one or (one[2], two[2]) != (entry['source'], entry['target']):
                    raise ValueError('Unexpected change at rowid ' + str(one[0]))
                changed += 1
        if changed != len(entries):
            raise ValueError('Not every listed cell changed')
        schema = 'SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name'
        if left.execute(schema).fetchall() != right.execute(schema).fetchall():
            raise ValueError('Schema changed')
        for database in (left, right):
            if database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('integrity_check failed')
        return dict(rows=rows, changed=changed, unchanged=rows - changed, schema_equal=True, integrity='ok',
                    encoding=right.execute('PRAGMA encoding').fetchone()[0])
    finally:
        left.close()
        right.close()


def verify_archive(archive, database, font):
    """Separate pass over the finished file; only the reader, not the writer's own values."""
    reader = V12ArchiveReader()
    entries = reader.list_entries(archive)
    if [entry.path for entry in entries] != [DATABASE_ENTRY, FONT_ENTRY]:
        raise ValueError('Unexpected entries in the package')
    raw = archive.read_bytes()
    for entry, expected in zip(entries, (database, font)):
        start = entry.offset + entry.header_size
        if entry.compression != 'None' or raw[start:start + entry.size] != expected:
            raise ValueError('Payload differs from the input: ' + entry.path)
    if reader.verify_archive(archive).archive_sha256 != hashlib.sha256(raw).hexdigest():
        raise ValueError('Archive hash differs')
    return entries[1].offset + entries[1].header_size, raw


@dataclass(frozen=True)
class NativeInputs:
    table: Path
    font: Path

    @classmethod
    def in_folder(cls, native: Path) -> 'NativeInputs':
        return cls(native / 'lang_multi_text.en.db', native / 'NotoSans-600.ttf')


_HEX40 = re.compile(r'[0-9a-f]{40}')
_HEX64 = re.compile(r'[0-9a-f]{64}')
_RECORD_FIELDS = ('repository', 'path', 'revision', 'blob', 'size', 'sha256', 'data_version', 'data_date')


def check_record(record) -> dict:
    """Provenance of a table: the pinned repository and file, a full revision, its Git blob, size and SHA-256."""
    if not isinstance(record, dict) or set(record) != set(_RECORD_FIELDS):
        raise ValueError('Описание таблицы неполное или с лишними полями')
    if record['repository'] != TABLE_REPOSITORY or record['path'] != TABLE_PATH:
        raise ValueError(f'Таблица не из закреплённого источника {TABLE_REPOSITORY}/{TABLE_PATH}')
    if not all(isinstance(record[name], str) and _HEX40.fullmatch(record[name]) for name in ('revision', 'blob')):
        raise ValueError('Ревизия и Git blob таблицы должны быть полными SHA-1')
    if not isinstance(record['sha256'], str) or not _HEX64.fullmatch(record['sha256']):
        raise ValueError('SHA-256 таблицы недопустим')
    if type(record['size']) is not int or not 0 < record['size'] <= TABLE_MAX:
        raise ValueError('Размер таблицы недопустим')
    for name, limit in (('data_version', 128), ('data_date', 64)):
        if record[name] is not None and (not isinstance(record[name], str) or len(record[name]) > limit):
            raise ValueError(f'Поле {name} недопустимо')
    return {name: record[name] for name in _RECORD_FIELDS}


def table_record(native: Path) -> dict:
    """The table `native` is set up with: table.json if a newer table was accepted, else the pinned snapshot."""
    path = Path(native) / TABLE_RECORD
    if not path.is_file():
        return dict(PINNED_TABLE)
    reject_links(path)
    with path.open('rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('table.json слишком большой')
    return check_record(json.loads(data.decode('utf-8')))


def table_label(record: dict | None) -> str:
    """'WWUIResourcesFull 30b665d7e783 · 30.09.2026': the repository, the revision and its commit date."""
    record = record or PINNED_TABLE
    label = str(record.get('repository', '')).rsplit('/', 1)[-1] + ' ' + str(record.get('revision', ''))[:12]
    date = record.get('data_date')
    if isinstance(date, str) and re.match(r'\d{4}-\d{2}-\d{2}', date):
        label += ' · ' + '.'.join(reversed(date[:10].split('-')))
    return label


def check_inputs(inputs: NativeInputs, pins: tuple[str, str] | None = None) -> None:
    """Without `pins` the table must be the one its table.json (or the pinned snapshot) names."""
    reject_links(inputs.table)
    reject_links(inputs.font)
    recorded = pins is None and (inputs.table.parent / TABLE_RECORD).is_file()
    if pins is None:
        pins = (table_record(inputs.table.parent)['sha256'], FONT_SHA256)
    if sha256(inputs.table) != pins[0]:
        raise ValueError('English table differs from table.json' if recorded else 'Pinned public snapshot changed')
    if sha256(inputs.font) != pins[1]:
        raise ValueError('Pinned font changed')


def _digests(path: Path) -> tuple[str, str, int]:
    """Git blob SHA-1, SHA-256 and size in one pass."""
    size = path.stat().st_size
    git, content, read = hashlib.sha1(b'blob %d\0' % size), hashlib.sha256(), 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            read += len(block)
            git.update(block)
            content.update(block)
    if read != size:
        raise ValueError('Файл изменился во время чтения: ' + path.name)
    return git.hexdigest(), content.hexdigest(), size


def check_table_file(path: Path) -> int:
    """A MultiText SQLite with the three columns the package edits, intact. Returns its row count."""
    try:
        database = open_read_only(path)
        try:
            columns = {row[1] for row in database.execute('PRAGMA table_info(MultiText)')}
            for column in ('Id', 'Content', 'RedirectDbIndex'):
                if column not in columns:
                    raise ValueError('В таблице MultiText нет столбца ' + column)
            if database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('integrity_check таблицы не прошёл')
            rows = database.execute('SELECT count(*) FROM MultiText').fetchone()[0]
        finally:
            database.close()
    except sqlite3.Error as error:
        raise ValueError('Таблица не открылась как SQLite: ' + str(error)) from error
    if not rows:
        raise ValueError('Таблица MultiText пуста')
    return rows


def _copy_checked(source: Path, destination: Path, pin: str) -> None:
    reject_links(destination)
    temporary = destination.with_name(destination.name + '.part')
    try:
        with source.open('rb') as stream, temporary.open('wb') as output:
            shutil.copyfileobj(stream, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if sha256(temporary) != pin:
            raise ValueError('Файл изменился во время копирования: ' + source.name)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _journal(native: Path, event: dict) -> None:
    event = dict(event, at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    with (native / 'journal.jsonl').open('a', encoding='utf-8') as journal:
        journal.write(json.dumps(event, ensure_ascii=False) + '\n')


def install_table(table: Path, record: dict, native: Path) -> dict:
    """Take a newer English table, only with provenance from the pinned public source: never a game file.

    Size, Git blob and SHA-256 of the file must match `record`, and the SQLite must hold an intact MultiText;
    anything wrong changes nothing. The copy lands next to the target, is checked again and replaces the table,
    then table.json is written. The journal keeps the previous table's hash. Run with the app closed."""
    record, table, native = check_record(record), Path(table), Path(native)
    reject_links(table)
    if not table.is_file():
        raise ValueError('Нет файла таблицы: ' + table.name)
    if _digests(table) != (record['blob'], record['sha256'], record['size']):
        raise ValueError('Таблица не совпадает с Git blob, размером или SHA-256 из описания')
    check_table_file(table)
    reject_links(native)
    native.mkdir(parents=True, exist_ok=True)
    target = NativeInputs.in_folder(native).table
    previous = sha256(target) if target.is_file() and not target.is_symlink() else None
    _copy_checked(table, target, record['sha256'])
    path = native / TABLE_RECORD
    if record == PINNED_TABLE:
        path.unlink(missing_ok=True)
    else:
        reject_links(path)
        temporary = path.with_name(path.name + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    _journal(native, {'event': 'install_table', 'revision': record['revision'], 'blob': record['blob'],
                      'sha256': record['sha256'], 'previous_sha256': previous})
    return record


def install_inputs(table: Path, font: Path, native: Path, pins: tuple[str, str] | None = None) -> NativeInputs:
    """Copy the two chosen files into `native` under the fixed names; both hashes are checked first.

    A wrong hash copies nothing. Each copy goes to a temp file next to the target, is checked again
    and then replaces the old file. The table here is the pinned snapshot (or `pins[0]`): a newer one
    comes only through install_table, so a table.json goes away with this copy."""
    if pins is None:
        pins = (PINNED_TABLE['sha256'], FONT_SHA256)
    table, font, native = Path(table), Path(font), Path(native)
    for path, pin, what in ((table, pins[0], 'Английская таблица'), (font, pins[1], 'Файл шрифта')):
        reject_links(path)
        if not path.is_file() or sha256(path) != pin:
            raise ValueError(f'{what} не совпадает с закреплённым SHA-256: {path.name}')
    target = NativeInputs.in_folder(native)
    reject_links(native)
    native.mkdir(parents=True, exist_ok=True)
    for source, destination, pin in ((table, target.table, pins[0]), (font, target.font, pins[1])):
        _copy_checked(source, destination, pin)
    (native / TABLE_RECORD).unlink(missing_ok=True)
    check_inputs(target, pins)
    return target


def font_coverage(font: bytes):
    """Same answer as one cmap lookup per text, but each code point is looked up only once:
    parsing the cmap for every one of ~150 000 cells took minutes."""
    known: dict[str, bool] = {}  # whitespace needs no glyph

    def has_glyph(text):
        letters = set(text)
        new = letters - known.keys()
        if new:
            points = {ord(letter) for letter in new if not letter.isspace()}
            found = covered(font, points) if points else set()
            known.update((letter, letter.isspace() or ord(letter) in found) for letter in new)
        return all(map(known.__getitem__, letters))
    return has_glyph


def select_cells(project_db, inputs: NativeInputs, kinds: tuple[str, ...], *,
                 has_glyph=None) -> tuple[list[dict], dict[str, int]]:
    """Cells for the package and the count of every reason a cell stayed out.

    `has_glyph` replaces the check against the font file (for tools that bring their own)."""
    reject_links(Path(project_db))
    database = sqlite3.connect(Path(project_db).absolute().as_uri() + '?mode=ro', uri=True)
    query = 'SELECT id_value, source, target, status FROM records WHERE active=1 AND target IS NOT NULL ORDER BY seq'
    table = None
    try:
        # One read transaction for both passes, so the digest describes exactly the rows selected below.
        database.execute('BEGIN')
        signature = None
        if has_glyph is None:
            reject_links(inputs.font)
            font = inputs.font.read_bytes()
            signature = _signature(database.execute(query), kinds, sha256(inputs.table),
                                   hashlib.sha256(font).hexdigest())
            with _LAST_LOCK:
                last = _LAST.get('select')
            if last is not None and last[0] == signature:
                return [_entry(*cell) for cell in last[1]], dict(last[2])
            has_glyph = font_coverage(font)
        table = open_read_only(inputs.table)
        entries, left_out, taken = [], Counter(), set()
        cells = {row[0]: (row[1], row[2]) for row in table.execute('SELECT Id, Content, RedirectDbIndex FROM MultiText')}
        for identifier, english, target, status in database.execute(query):
            if text_kind(identifier) not in kinds:
                continue
            cell = cells.get(identifier)
            reason = ('DUPLICATE_ID' if identifier in taken else 'NOT_IN_TABLE' if cell is None else 'MASKED' if cell[1] != 0
                      else 'ENGLISH_DIFFERS' if cell[0] != english
                      else 'STATUS_' + status if status not in USABLE else 'CHECK_FAILED' if validate(english, target)
                      else 'NO_GLYPH' if not has_glyph(added_signs(english, target)) else None)
            if reason:
                left_out[reason] += 1
            else:
                taken.add(identifier)
                entries.append(_entry(identifier, english, target))
    finally:
        database.close()
        if table is not None:
            table.close()
    left_out = dict(sorted(left_out.items()))
    if signature is not None:
        with _LAST_LOCK:
            _LAST['select'] = (signature, tuple((cell['id'], cell['source'], cell['target']) for cell in entries),
                               dict(left_out))
    return entries, left_out


def _entry(identifier, english, target):
    return dict(id=identifier, kind='TRANSLATION', status=STATUS, source=english, target=target)


# One remembered answer: a click on «Играть» inspects, builds and inspects again over the same rows.
# Kept as tuples sharing the strings of the returned cells: far smaller than a second list of dicts.
_LAST: dict = {}
_LAST_LOCK = Lock()


def _signature(rows, kinds, table_sha256: str, font_sha256: str) -> bytes:
    """Digest of everything the answer depends on: rules, kinds, both inputs and every row, length-prefixed."""
    digest = hashlib.blake2b(digest_size=32)
    for part in (str(RULES_VERSION), repr(tuple(kinds)), table_sha256, font_sha256):
        digest.update(part.encode('utf-8') + b'\0')
    for row in rows:
        for value in row:
            encoded = str(value).encode('utf-8', errors='surrogatepass')
            digest.update(len(encoded).to_bytes(8, 'little'))
            digest.update(encoded)
    return digest.digest()


def build_key(cells: list[dict], inputs: NativeInputs) -> str:
    payload = dict(rules=RULES_VERSION, table=sha256(inputs.table), font=sha256(inputs.font),
                   cells=[[cell['id'], cell['target']] for cell in cells])
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def current_key(project_db, native, kinds, *, pins: tuple[str, str] | None = None) -> str:
    inputs = NativeInputs.in_folder(Path(native))
    check_inputs(inputs, pins)
    return build_key(select_cells(project_db, inputs, tuple(kinds))[0], inputs)


def _sweep(staging: Path, keep: Path | None) -> None:
    """One package only: other keys, half-written files and work folders of a killed build go away."""
    for item in staging.iterdir():
        if item == keep or item.is_symlink():
            continue
        if item.is_dir() and item.name.startswith('.work-'):
            shutil.rmtree(item, ignore_errors=True)
        elif item.is_file() and (item.name.endswith('.pak.part') or (keep is not None and item.suffix == '.pak')):
            item.unlink(missing_ok=True)


def _read_report(path: Path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_package(project_db, native, kinds, *, cancelled: Event | None = None, installed: Path | None = None,
                  pins: tuple[str, str] | None = None) -> dict:
    native = Path(native)
    kinds = tuple(kinds)
    inputs = NativeInputs.in_folder(native)
    check_cancel(cancelled)
    check_inputs(inputs, pins)
    table = table_record(native)
    if table['sha256'] != sha256(inputs.table):  # explicit pins of a tool: no provenance to name
        table = None
    cells, left_out = select_cells(project_db, inputs, kinds)
    if not cells:
        raise ValueError('Nothing to package')
    key = build_key(cells, inputs)
    staging = native / 'staging'
    report_path = native / 'package.json'
    archive = staging / (key + '.pak')
    previous = _read_report(report_path)
    if previous and previous.get('key') == key:
        wanted = previous.get('pak_sha256')
        if (archive.is_file() and sha256(archive) == wanted) or (
                installed is not None and installed.is_file() and sha256(installed) == wanted):
            return dict(previous, rebuilt=False)
    sizes = inputs.table.stat().st_size, inputs.font.stat().st_size
    staging.mkdir(parents=True, exist_ok=True)
    _sweep(staging, keep=None)
    need, free = 2 * sum(sizes), shutil.disk_usage(staging).free
    if free < need:
        raise OSError('Not enough free space: %d bytes needed, %d available' % (need, free))
    check_cancel(cancelled)
    part = staging / (key + '.pak.part')
    try:
        with tempfile.TemporaryDirectory(prefix='.work-', dir=staging) as temporary:
            copy = Path(temporary) / DATABASE
            apply_entries(inputs.table, copy, cells)
            compare(inputs.table, copy, cells)
            database, font = copy.read_bytes(), inputs.font.read_bytes()
            check_cancel(cancelled)
            write_archive({DATABASE_ENTRY: database, FONT_ENTRY: font}, part, cancelled=cancelled)
            _, raw = verify_archive(part, database, font)
        os.replace(part, archive)
        _sweep(staging, keep=archive)
    finally:
        part.unlink(missing_ok=True)
    report = dict(schema_version=1, key=key, pak_sha256=hashlib.sha256(raw).hexdigest(),
                  pak_sha1=hashlib.sha1(raw).hexdigest().upper(), pak_bytes=len(raw), cells=len(cells),
                  left_out=left_out, kinds=list(kinds), table=table, built_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    temporary = report_path.with_name(report_path.name + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, report_path)
    return dict(report, rebuilt=True)
