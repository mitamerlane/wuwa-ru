"""Independent synthetic SELF-2 producer. No application reader/writer imports.

AES key is caller-supplied test data; this is not an example game package.
"""
import hashlib
import struct
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

TEST_KEY = bytes(range(32))  # public synthetic test key, never a game key
TEST_GUID = bytes(range(16))


def number(value, fmt='I'):
    return struct.pack('<' + fmt, value)


def string(value):
    raw = value.encode() + b'\0'
    return number(len(raw)) + raw


def make_pak(path, data=b'abc', *, encrypted=True, key=TEST_KEY, name='text.bin',
             primary_edit=None, padding_edit=False, codec=None, block_size=65536,
             wide=False, header_edit=None, packed_blocks=None):
    def padded(raw):
        return raw + bytes(-len(raw) % 16) if encrypted else raw

    def seal(raw):
        if not encrypted:
            return raw
        cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return cipher.update(raw) + cipher.finalize()

    if codec:
        chunks = [data[i:i + block_size] for i in range(0, len(data), block_size)]
        packed = packed_blocks if packed_blocks is not None else [
            zlib.compress(c, wbits=31 if codec == 'Gzip' else 15) for c in chunks]
        stored = b''.join(packed)
    else:
        packed, stored = [], data
    header_size = 53 + (4 + 16 * len(packed) if codec else 0)
    blocks = bytearray()
    start = header_size
    for chunk in packed:
        blocks += struct.pack('<QQ', start, start + len(chunk))
        start += len(chunk)
    header = (struct.pack('<QQQI', 0, len(stored), len(data), int(bool(codec)))
              + hashlib.sha1(stored).digest()
              + (number(len(packed)) + blocks if codec else b'')
              + b'\0' + number(block_size if codec else 0))
    header = bytearray(header)
    if header_edit:
        header_edit(header)
    payload = header + stored
    block_code = block_size >> 11 if block_size % 2048 == 0 and block_size >> 11 < 63 else 63
    logical = ((0 if wide else 0xe0000000)
               | ((1 << 23) | (len(packed) << 6) | block_code if codec else 0))
    # Inverse mapping uses independently listed output bit positions.
    positions = list(range(16, 22)) + list(range(16)) + [28] + list(range(22, 28)) + [30, 31, 29]
    flags = sum(((logical >> bit) & 1) << positions[bit] for bit in range(32))
    fmt = 'Q' if wide else 'I'
    encoded = number(flags) + b'\0'
    if codec and block_code == 63:
        encoded += number(block_size)
    encoded += number(len(data), fmt) + number(0, fmt)
    if codec:
        encoded += number(len(stored), fmt)
        if len(packed) > 1:
            encoded += b''.join(number(len(c)) for c in packed)
    value = 0xcbf29ce484222325
    for byte in name.lower().encode('utf-16le'):
        value = ((value ^ byte) * 0x100000001b3) % (1 << 64)
    hashes = padded(number(1) + number(value, 'Q') + number(0) + number(0))
    parent, _, basename = name.rpartition('/')
    directory = padded(number(1) + string(parent + '/' if parent else '/')
                       + number(1) + string(basename) + number(0))
    prefix = string('../../../') + number(1) + number(0, 'Q')
    tail = number(len(encoded)) + encoded + number(0)
    primary_size = len(padded(prefix + bytes(80) + tail))
    primary = prefix
    offset = len(payload) + primary_size
    for raw in (hashes, directory):
        primary += number(1) + number(offset, 'Q') + number(len(raw), 'Q') + hashlib.sha1(raw).digest()
        offset += len(raw)
    primary = bytearray(padded(primary + tail))
    if primary_edit:
        primary_edit(primary)
    if padding_edit:
        primary[-1] = 1
    footer = ((TEST_GUID if encrypted else bytes(16)) + bytes([int(encrypted)])
              + struct.pack('<IIQQ', 0x5a6f12e1, 12, len(payload), len(primary))
              + hashlib.sha1(primary).digest()
              + ((codec.encode() + bytes(32 - len(codec)) + bytes(128)) if codec else bytes(160)))
    path.write_bytes(payload + seal(bytes(primary)) + seal(hashes) + seal(directory) + footer)
    return path
