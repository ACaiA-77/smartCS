"""Argon2id passwords with random salts; callers offload CPU work from the event loop."""

import secrets

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError


_passwords = PasswordHash.recommended()
_dummy_hash = _passwords.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        raise ValueError("password must contain 8 to 1024 characters")
    return _passwords.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        return False
    try:
        # Unknown accounts take the same expensive path without a fixed dummy password.
        valid = _passwords.verify(password, password_hash or _dummy_hash)
        return bool(password_hash) and valid
    except (UnknownHashError, ValueError, TypeError):
        return False
