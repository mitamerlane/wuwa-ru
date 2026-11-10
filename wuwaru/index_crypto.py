"""AES-256-ECB index boundary; no key discovery, persistence or networking."""
from pathlib import Path
from typing import Protocol

from .core_contracts import CoreError, ErrorCode


class KeyProvider(Protocol):
    """Caller supplies a key only when authorized for this archive and raw GUID.

    Return None if unavailable/not authorized. Implementations must not log keys.
    The reader never caches keys across operations or enumerates candidate keys.
    """
    def get_key(self, archive: Path, key_guid: bytes) -> bytes | None: ...


def authorized_key(provider: KeyProvider | None, archive: Path, guid: bytes) -> bytes:
    try:
        key = provider.get_key(archive, guid) if provider is not None else None
    except Exception:
        raise CoreError(ErrorCode.KEY_UNAVAILABLE, 'KeyProvider не предоставил ключ') from None
    if not isinstance(key, bytes) or len(key) != 32:
        raise CoreError(ErrorCode.KEY_UNAVAILABLE, 'Нужен разрешённый 32-byte AES ключ')
    return key


def decrypt_index(data: bytes, key: bytes) -> bytes:
    if not data or len(data) % 16:
        raise CoreError(ErrorCode.CORRUPTED_ARCHIVE, 'AES index должен быть кратен 16 bytes')
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise CoreError(ErrorCode.ENCRYPTED_INDEX, 'Требуется библиотека cryptography') from None
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()
