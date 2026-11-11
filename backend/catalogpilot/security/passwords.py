"""Password hashing and policy (SEC-IAM-02/03/04)."""

from __future__ import annotations

import hashlib
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .crypto import key_provider

_hasher = PasswordHasher(time_cost=3, memory_cost=19 * 1024, parallelism=1)
_DUMMY = _hasher.hash("timing-equalizer")

# A tiny built-in list; production adds a k-anonymity breached-password lookup behind this seam.
_COMMON = {
    "password1234", "123456789012", "qwertyuiop12", "letmein12345", "iloveyou1234",
    "administrator", "passwordpassword", "111111111111", "abcdefghijkl", "welcome12345",
}
_CONTEXT_WORDS = ("catalogpilot",)


class WeakPassword(ValueError):
    pass


def _peppered(password: str) -> str:
    # The pepper lives in the key provider (KMS), not in the database.
    pepper = key_provider().platform_key("password-pepper")
    return hmac.new(pepper, password.encode("utf-8"), hashlib.sha256).hexdigest()


def check_policy(password: str, email: str = "") -> None:
    if len(password) < 12:
        raise WeakPassword("Password must be at least 12 characters.")
    if len(password) > 256:
        raise WeakPassword("Password must be at most 256 characters.")
    lowered = password.lower()
    if lowered in _COMMON or len(set(password)) < 4:
        raise WeakPassword("This password is too common or too simple.")
    local = email.split("@")[0].lower() if email else ""
    if (local and len(local) >= 4 and local in lowered) or any(w in lowered for w in _CONTEXT_WORDS):
        raise WeakPassword("Password must not contain your email or the product name.")


def hash_password(password: str) -> str:
    return _hasher.hash(_peppered(password))


def verify_password(stored: str | None, password: str) -> bool:
    """Runs a real verification even for unknown users so timing does not reveal existence."""
    try:
        _hasher.verify(stored or _DUMMY, _peppered(password))
        return stored is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _hasher.check_needs_rehash(stored)
