"""Lockout and session rules. Pure functions over a connection and a clock."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from enum import Enum

from ads_auth.db import from_iso, to_iso, utc_now
from ads_auth.models import User

LOCKOUT_FAILURES = 5
LOCKOUT_WINDOW = timedelta(minutes=15)
SESSION_LIFETIME = timedelta(hours=8)
TEMP_PASSWORD_LIFETIME = timedelta(days=90)


def prune_failures(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    cutoff = to_iso((now or utc_now()) - LOCKOUT_WINDOW)
    conn.execute("DELETE FROM login_attempts WHERE failed_utc < ?", (cutoff,))


def record_failure(conn: sqlite3.Connection, username: str, now: datetime | None = None) -> None:
    """Record a failed attempt for an EXISTING account. Callers never pass unknown names."""
    now = now or utc_now()
    with conn:
        prune_failures(conn, now)
        conn.execute(
            "INSERT INTO login_attempts(username, failed_utc) VALUES (?, ?)",
            (username, to_iso(now)),
        )


def clear_failures(conn: sqlite3.Connection, username: str) -> None:
    with conn:
        conn.execute("DELETE FROM login_attempts WHERE username = ?", (username,))


def is_locked(conn: sqlite3.Connection, username: str, now: datetime | None = None) -> bool:
    """Locked while there are LOCKOUT_FAILURES or more failures inside the window."""
    cutoff = to_iso((now or utc_now()) - LOCKOUT_WINDOW)
    count = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE username = ? AND failed_utc >= ?",
        (username, cutoff),
    ).fetchone()[0]
    return count >= LOCKOUT_FAILURES


class SessionStatus(Enum):
    OK = "ok"
    MISSING = "missing"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INACTIVE = "inactive"


def session_status(
    user: User | None,
    session_version: int | None,
    authenticated_utc: str | None,
    now: datetime | None = None,
) -> SessionStatus:
    """Re-validate a session against the current user row on every protected action."""
    if user is None or session_version is None or not authenticated_utc:
        return SessionStatus.MISSING
    now = now or utc_now()
    if now - from_iso(authenticated_utc) >= SESSION_LIFETIME:
        return SessionStatus.EXPIRED
    if not user.is_active:
        return SessionStatus.INACTIVE
    if user.session_version != session_version:
        return SessionStatus.REVOKED
    return SessionStatus.OK


def temp_password_expired(user: User, now: datetime | None = None) -> bool:
    if not user.must_change_password or not user.temp_password_expires_utc:
        return False
    return (now or utc_now()) >= from_iso(user.temp_password_expires_utc)
