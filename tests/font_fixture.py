"""Self-authored sfnt bytes with a chosen cmap; shared by the font probe and the package tests."""
import struct

ASCII = range(0x20, 0x7f)
LETTERS = 'абвгдеёжзийклмнопрстуфхцчшщъыьэюя'
RUSSIAN = {ord(letter) for letter in LETTERS} | {ord(letter.upper()) for letter in LETTERS}


def cmap_format4(codepoints):
    points = sorted(codepoints)
    count = len(points) + 1
    body = struct.pack('>HHHH', count * 2, 0, 0, 0)
    body += struct.pack('>%dH' % count, *points, 0xFFFF) + b'\0\0'
    body += struct.pack('>%dH' % count, *points, 0xFFFF)
    body += struct.pack('>%dh' % count, *(1 - point for point in points), 1)  # every code maps to glyph 1
    body += struct.pack('>%dH' % count, *[0] * count)
    table = struct.pack('>HH', 4, len(body) + 6) + struct.pack('>H', 0) + body
    return struct.pack('>HH', 0, 1) + struct.pack('>HHI', 3, 1, 12) + table


def font(codepoints=(*ASCII, *RUSSIAN), *, signature=b'\0\1\0\0', extra=()):
    tables = [(b'cmap', cmap_format4(codepoints)), *extra]
    data = signature + struct.pack('>HHHH', len(tables), 0, 0, 0)
    offset = len(data) + 16 * len(tables)
    body = b''
    for tag, table in tables:
        data += tag + struct.pack('>III', 0, offset + len(body), len(table))
        body += table
    return data + body
