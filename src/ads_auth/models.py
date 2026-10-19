from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ROLES = ("user", "manager", "admin")
ROLE_RANK = {role: i for i, role in enumerate(ROLES)}
MAX_USERNAME_LENGTH = 80

_WHITESPACE_RE = re.compile(r"\s")


def normalize_username(raw: str) -> str:
    """Trim and casefold. Reject empty, over-long, or names with whitespace/control characters."""
    if raw is None:
        raise ValueError("Username is required.")
    name = unicodedata.normalize("NFKC", str(raw)).strip().casefold()
    if not name:
        raise ValueError("Username is required.")
    if len(name) > MAX_USERNAME_LENGTH:
        raise ValueError(f"Username must be at most {MAX_USERNAME_LENGTH} characters.")
    if _WHITESPACE_RE.search(name) or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("Username must not contain spaces or control characters.")
    return name


@dataclass(frozen=True)
class User:
    """A user row without its password hash, so it can be logged or shown safely."""

    id: int
    username: str
    full_name: str | None
    role: str
    is_active: bool
    must_change_password: bool
    temp_password_expires_utc: str | None
    session_version: int
    created_utc: str
    last_login_utc: str | None

    def has_role(self, minimum: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[minimum]

    @classmethod
    def from_row(cls, row) -> "User":
        return cls(
            id=row["id"],
            username=row["username"],
            full_name=row["full_name"],
            role=row["role"],
            is_active=bool(row["is_active"]),
            must_change_password=bool(row["must_change_password"]),
            temp_password_expires_utc=row["temp_password_expires_utc"],
            session_version=row["session_version"],
            created_utc=row["created_utc"],
            last_login_utc=row["last_login_utc"],
        )
