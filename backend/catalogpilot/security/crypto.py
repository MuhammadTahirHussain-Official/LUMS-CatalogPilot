"""Key management and envelope encryption (SEC-CRY-05, CRED-06).

``KeyProvider`` is the KMS seam. The local provider derives a per-tenant key-encryption key
from one master key with HKDF, which gives the same hierarchy as a cloud KMS
(root -> tenant key -> per-credential data key) without any infrastructure. In production,
swap in a provider that calls the cloud KMS; nothing else changes.

Encryption context (tenant id + object id) is bound as AES-GCM associated data, so a
ciphertext copied to another row or tenant fails authentication instead of decrypting.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config import get_settings

NONCE = 12


class DecryptionError(Exception):
    pass


class KeyProvider(Protocol):
    def wrap(self, tenant_id: str, data_key: bytes, context: bytes) -> bytes: ...
    def unwrap(self, tenant_id: str, wrapped: bytes, context: bytes) -> bytes: ...
    def platform_key(self, purpose: str) -> bytes: ...


class LocalKeyProvider:
    def __init__(self, master: bytes):
        if len(master) != 32:
            raise ValueError("master key must be 32 bytes")
        self._master = master

    def _derive(self, info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(self._master)

    def wrap(self, tenant_id: str, data_key: bytes, context: bytes) -> bytes:
        return seal(self._derive(b"tenant-kek:" + tenant_id.encode()), data_key, context)

    def unwrap(self, tenant_id: str, wrapped: bytes, context: bytes) -> bytes:
        return open_sealed(self._derive(b"tenant-kek:" + tenant_id.encode()), wrapped, context)

    def platform_key(self, purpose: str) -> bytes:
        return self._derive(b"platform:" + purpose.encode())


def seal(key: bytes, plaintext: bytes, context: bytes) -> bytes:
    nonce = os.urandom(NONCE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, context)


def open_sealed(key: bytes, blob: bytes, context: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:NONCE], blob[NONCE:], context)
    except (InvalidTag, ValueError) as exc:
        raise DecryptionError("ciphertext failed authentication") from exc


def envelope_encrypt(tenant_id: str, object_id: str, plaintext: bytes) -> tuple[bytes, bytes]:
    """Returns (ciphertext, wrapped_data_key). A fresh data key is used for every object."""
    context = f"{tenant_id}|{object_id}".encode()
    data_key = AESGCM.generate_key(256)
    try:
        return seal(data_key, plaintext, context), key_provider().wrap(tenant_id, data_key, context)
    finally:
        del data_key


def envelope_decrypt(tenant_id: str, object_id: str, ciphertext: bytes, wrapped: bytes) -> bytes:
    context = f"{tenant_id}|{object_id}".encode()
    data_key = key_provider().unwrap(tenant_id, wrapped, context)
    return open_sealed(data_key, ciphertext, context)


_provider: KeyProvider | None = None


def key_provider() -> KeyProvider:
    global _provider
    if _provider is None:
        _provider = LocalKeyProvider(_load_master())
    return _provider


def reset_key_provider() -> None:
    global _provider
    _provider = None


def _load_master() -> bytes:
    settings = get_settings()
    if settings.master_key_b64:
        return base64.b64decode(settings.master_key_b64)
    if settings.is_production:
        raise RuntimeError("CP_MASTER_KEY_B64 is required in production")
    path = settings.data_dir / "dev-master.key"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(base64.b64encode(os.urandom(32)))
    return base64.b64decode(path.read_bytes())


# ---- tokens and keyed hashes -------------------------------------------------------------

def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def keyed_hash(purpose: str, value: str) -> bytes:
    """HMAC-SHA-256 with a platform key, for session ids, API keys and one-time tokens."""
    return hmac.new(key_provider().platform_key(purpose), value.encode(), hashlib.sha256).digest()


def constant_time_equal(a: bytes | str, b: bytes | str) -> bool:
    a_b = a.encode() if isinstance(a, str) else a
    b_b = b.encode() if isinstance(b, str) else b
    return hmac.compare_digest(a_b, b_b)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
