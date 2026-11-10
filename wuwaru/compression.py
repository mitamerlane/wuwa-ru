"""Bounded PAK block decoding. No executable/DLL loading or downloads."""
from collections.abc import Callable
import zlib

from .core_contracts import CoreError, ErrorCode

MAX_BLOCK_OUTPUT = 1024 * 1024
MAX_BLOCK_INPUT = 2 * 1024 * 1024


class CompressionProvider:
    """Zlib/Gzip and an explicitly supplied, authorized Oodle block adapter.

    The optional callable receives (compressed_bytes, exact_output_size) and
    must itself enforce that allocation bound. No Oodle implementation ships
    here. Its licensing, ABI and native-memory safety need separate validation.
    """
    def __init__(self, *, oodle_decoder: Callable[[bytes, int], bytes] | None = None):
        self.oodle_decoder = oodle_decoder

    def require(self, codec: str) -> None:
        if codec in ('Zlib', 'Gzip') or (codec == 'Oodle' and self.oodle_decoder is not None):
            return
        raise CoreError(ErrorCode.DECOMPRESSION_UNAVAILABLE, 'Нет допустимого decoder для кодека')

    def decode_block(self, codec: str, data: bytes, expected_size: int) -> bytes:
        self.require(codec)
        if not (0 < expected_size <= MAX_BLOCK_OUTPUT and 0 < len(data) <= MAX_BLOCK_INPUT):
            raise CoreError(ErrorCode.CORRUPTED_ARCHIVE, 'Превышен размер блока сжатия')
        try:
            if codec == 'Oodle':
                output = self.oodle_decoder(data, expected_size)
            else:
                decoder = zlib.decompressobj(31 if codec == 'Gzip' else 15)
                output = decoder.decompress(data, expected_size + 1)
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise ValueError
            if not isinstance(output, bytes) or len(output) != expected_size:
                raise ValueError
        except Exception:
            raise CoreError(ErrorCode.CORRUPTED_ARCHIVE, 'Некорректный compressed block') from None
        return output
