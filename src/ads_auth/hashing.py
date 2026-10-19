"""Werkzeug-compatible password hashing, so accounts can be imported from other ADS apps."""
from __future__ import annotations

import secrets

from werkzeug.security import check_password_hash, generate_password_hash

MIN_NEW_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024
SUPPORTED_HASH_PREFIXES = ("scrypt:", "pbkdf2:")


def verify(password: str, stored_hash: str) -> bool:
    if not password or not stored_hash:
        return False
    try:
        return check_password_hash(stored_hash, password)
    except (ValueError, TypeError):
        return False


def generate(password: str) -> str:
    """scrypt via Werkzeug defaults; same format the other ADS apps produce."""
    return generate_password_hash(password)


def is_supported_hash(stored_hash: str | None) -> bool:
    return bool(stored_hash) and stored_hash.startswith(SUPPORTED_HASH_PREFIXES)


def validate_new_password(password: str) -> None:
    if password is None or len(password) < MIN_NEW_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_NEW_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("Password is too long.")


def generate_temporary_password() -> str:
    """Random, URL-safe, 24 characters: comfortably above the minimum."""
    return secrets.token_urlsafe(18)
