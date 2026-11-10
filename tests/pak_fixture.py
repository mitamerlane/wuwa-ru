"""Synthetic format fixture, based on the pinned WuWaRu V12 layout; not a builder API."""
import hashlib
import struct

PREFIX = 'Client/Content/Aki/ConfigDB/en/'


def pak(path, files, *, utf16=False, mount_point='../../../', pruned_entries=(), alignment=0):
    def n(value):
        return struct.pack('<I', value)

    def q(value):
        return struct.pack('<Q', value)

    def s(value):
        if utf16:
            data = value.encode('utf-16le') + b'\0\0'
            return struct.pack('<i', -len(data) // 2) + data
        data = value.encode() + b'\0'
        return n(len(data)) + data

    def h(data):
        return hashlib.sha1(data).digest()

    payload = bytearray()
    encoded = bytearray()
    names = []
    hashes = bytearray(n(len(files) + len(pruned_entries)))
    for name, data in files.items():
        if alignment:
            payload += bytes(-len(payload) % alignment)
        offset = len(payload)
        payload += struct.pack('<QQQI', 0, len(data), len(data), 0) + h(data) + b'\0' + n(0) + data
        names.append((name, len(encoded)))
        value = 0xCBF29CE484222325
        for byte in (PREFIX + name).lower().encode('utf-16le'):
            value = ((value ^ byte) * 0x100000001B3) & 0xffffffffffffffff
        hashes += q(value) + n(len(encoded))
        encoded += n(0xe0000000) + b'\0' + n(len(data)) + n(offset)
    for name in pruned_entries:
        value = 0xCBF29CE484222325
        for byte in (PREFIX + name).lower().encode('utf-16le'):
            value = ((value ^ byte) * 0x100000001B3) & 0xffffffffffffffff
        hashes += q(value) + n(0x80000000)
        names.append((name, 0x80000000))
    hashes += n(0)
    directory = n(1) + s(PREFIX) + n(len(names))
    for name, entry in names:
        directory += s(name) + n(entry)
    index = len(payload)
    primary_size = len(s(mount_point)) + 4 + 8 + 80 + 4 + len(encoded) + 4
    primary = (s(mount_point) + n(len(names)) + q(0)
               + n(1) + q(index + primary_size) + q(len(hashes)) + h(hashes)
               + n(1) + q(index + primary_size + len(hashes)) + q(len(directory)) + h(directory)
               + n(len(encoded)) + encoded + n(0))
    footer = bytes(17) + n(0x5a6f12e1) + n(12) + q(index) + q(len(primary)) + h(primary) + bytes(160)
    path.write_bytes(payload + primary + hashes + directory + footer)
    return path


def streamed_pak(path, size, *, sparse=False):
    """Single zero payload with bounded fixture RAM; sparse mode is metadata-only.

    Sparse mode deliberately leaves a zero SHA-1: it is NOT a verified 4 GiB
    archive. It exercises 64-bit footer offsets without allocating 4 GiB on disk.
    """
    base = pak(path, {'large.bin': b'x'}).read_bytes()
    primary_size = struct.unpack_from('<Q', base, len(base) - 221 + 33)[0]
    primary = bytearray(base[54:54 + primary_size])
    remainder = base[54 + primary_size:-221]
    offset = 53 + size
    descriptor = 4 + 10 + 4 + 8
    hash_size = struct.unpack_from('<Q', primary, descriptor + 12)[0]
    struct.pack_into('<Q', primary, descriptor + 4, offset + len(primary))
    struct.pack_into('<Q', primary, descriptor + 44, offset + len(primary) + hash_size)
    struct.pack_into('<I', primary, len(primary) - 12, size)
    footer = (bytes(17) + struct.pack('<IIQQ', 0x5a6f12e1, 12, offset, len(primary))
              + hashlib.sha1(primary).digest() + bytes(160))
    with path.open('w+b') as out:
        if sparse:
            import ctypes
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            control = kernel.DeviceIoControl
            control.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            control.restype = wintypes.BOOL
            returned = wintypes.DWORD()
            if not control(msvcrt.get_osfhandle(out.fileno()), 0x900C4, None, 0,
                           None, 0, ctypes.byref(returned), None):
                raise ctypes.WinError(ctypes.get_last_error())
        out.write(bytes(53))
        digest = hashlib.sha1()
        if sparse:
            out.seek(offset)
        else:
            block = bytes(1024 * 1024)
            remaining = size
            while remaining:
                part = block[:min(len(block), remaining)]
                out.write(part)
                digest.update(part)
                remaining -= len(part)
        out.write(primary + remainder + footer)
        out.seek(0)
        out.write(struct.pack('<QQQI', 0, size, size, 0)
                  + (bytes(20) if sparse else digest.digest()) + bytes(5))
    return path


def large_index_pak(path, count):
    """Build a valid plain archive with many empty entries using bounded payload RAM."""
    def n(value): return struct.pack('<I', value)
    def q(value): return struct.pack('<Q', value)
    def s(value):
        raw = value.encode('utf-8') + b'\0'
        return n(len(raw)) + raw
    def h(data): return hashlib.sha1(data).digest()

    encoded = bytearray()
    hashes = bytearray(n(count))
    directory = bytearray(n(1) + s(PREFIX) + n(count))
    prefix_hash = PREFIX.lower().encode('utf-16le')
    for i in range(count):
        name = f'e{i:06}.bin'
        entry_offset = len(encoded)
        value = 0xCBF29CE484222325
        for byte in prefix_hash + name.lower().encode('utf-16le'):
            value = ((value ^ byte) * 0x100000001B3) & 0xffffffffffffffff
        hashes += q(value) + n(entry_offset)
        encoded += n(0xe0000000) + b'\0' + n(0) + n(i * 53)
        directory += s(name) + n(entry_offset)
    hashes += n(0)

    index_offset = count * 53
    primary_size = len(s('../../../')) + 4 + 8 + 80 + 4 + len(encoded) + 4
    primary = (s('../../../') + n(count) + q(0)
               + n(1) + q(index_offset + primary_size) + q(len(hashes)) + h(hashes)
               + n(1) + q(index_offset + primary_size + len(hashes))
               + q(len(directory)) + h(directory)
               + n(len(encoded)) + encoded + n(0))
    footer = (bytes(17) + n(0x5a6f12e1) + n(12) + q(index_offset)
              + q(len(primary)) + h(primary) + bytes(160))
    header = struct.pack('<QQQI', 0, 0, 0, 0) + h(b'') + b'\0' + n(0)
    with path.open('wb') as out:
        for _ in range(count): out.write(header)
        out.write(primary + hashes + directory + footer)
    return path
