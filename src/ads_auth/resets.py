"""Self-service password reset: one-time links sent to an account's email address.

A request makes a random token, stores only its SHA-256 with the account, the account's
`session_version`, and the times, and returns the token for the email. The link works once, for
`LINK_LIFETIME`, and only while the account's `session_version` is unchanged: any password change,
admin reset, role change, rename or deactivation ends it. Asking again replaces the previous link.
At most `MAX_PER_HOUR` links go out per account per hour; further requests send nothing.

Requesting a link changes nothing about the account, so no one can lock a colleague out by typing
their address. Rows are deleted a day after they expire. Framework-free: no Streamlit, no I/O at import.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from ads_auth.db import connection, from_iso, to_iso, utc_now
from ads_auth.models import User
from ads_auth.store import StoreError, UserStore

LINK_LIFETIME = timedelta(minutes=30)
MAX_PER_HOUR = 3
KEEP_AFTER_EXPIRY = timedelta(days=1)
QUERY_PARAM = "reset"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def request(db_path, identifier: str, now: datetime | None = None) -> tuple[User, str] | None:
    """The account and a new token when a link should be emailed; None when nothing should be sent
    (no such active account, no email on it, or the hourly limit reached). The caller shows the same
    message either way."""
    now = now or utc_now()
    user = UserStore(db_path).find_for_reset(identifier)
    if user is None:
        return None
    token = secrets.token_urlsafe(32)
    with connection(db_path) as conn:
        recent = conn.execute(
            "SELECT COUNT(*) FROM password_resets WHERE user_id = ? AND created_utc >= ?",
            (user.id, to_iso(now - timedelta(hours=1))),
        ).fetchone()[0]
        if recent >= MAX_PER_HOUR:
            return None
        with conn:
            # A new link replaces any earlier one; the rows stay (marked used) so the hourly limit counts them.
            conn.execute("UPDATE password_resets SET used_utc = ? WHERE user_id = ? AND used_utc IS NULL",
                         (to_iso(now), user.id))
            conn.execute(
                """INSERT INTO password_resets(user_id, token_hash, session_version, created_utc, expires_utc)
                   VALUES (?, ?, ?, ?, ?)""",
                (user.id, _hash(token), user.session_version, to_iso(now), to_iso(now + LINK_LIFETIME)),
            )
    return user, token


def check(db_path, token: str, now: datetime | None = None) -> User | None:
    """The account a token is good for right now, or None (unknown, used, expired, or overtaken by a
    change to the account)."""
    if not token:
        return None
    now = now or utc_now()
    with connection(db_path) as conn:
        row = conn.execute("SELECT * FROM password_resets WHERE token_hash = ?", (_hash(token),)).fetchone()
    if row is None or row["used_utc"] is not None or from_iso(row["expires_utc"]) <= now:
        return None
    user = UserStore(db_path).get_by_id(row["user_id"])
    if user is None or not user.is_active or user.session_version != row["session_version"]:
        return None
    return user


def complete(db_path, token: str, new_password: str, now: datetime | None = None) -> User:
    """Set the new password through a valid link and use the link up. Raises StoreError for a link
    that is no longer good, ValueError for a password the policy refuses (the link stays good)."""
    now = now or utc_now()
    user = check(db_path, token, now)
    if user is None:
        raise StoreError("This reset link has expired or was already used.")
    updated = UserStore(db_path).set_password(user.id, new_password)
    with connection(db_path) as conn, conn:
        conn.execute("UPDATE password_resets SET used_utc = ? WHERE token_hash = ?", (to_iso(now), _hash(token)))
    return updated


def prune(conn, now: datetime | None = None) -> int:
    """Delete rows a day past their expiry."""
    cutoff = to_iso((now or utc_now()) - KEEP_AFTER_EXPIRY)
    with conn:
        cur = conn.execute("DELETE FROM password_resets WHERE expires_utc < ?", (cutoff,))
    return cur.rowcount


def link(public_url: str, token: str) -> str:
    return f"{public_url.rstrip('/')}/?{QUERY_PARAM}={token}"


def email_text(app_title: str, user: User, url: str) -> tuple[str, str]:
    """Subject and plain-text body of the reset email."""
    first = (user.full_name or "").split()[0] if user.full_name else ""
    minutes = int(LINK_LIFETIME.total_seconds() // 60)
    subject = f"Reset your {app_title} password"
    body = (
        f"Hello{' ' + first if first else ''},\n\n"
        f"Someone asked to reset the password for your {app_title} account ({user.username}).\n\n"
        f"Choose a new password here. The link works once, for the next {minutes} minutes:\n"
        f"{url}\n\n"
        "If you didn't ask for this, you can ignore this email; your password stays the same.\n\n"
        "Austin Data Solutions\n"
        "This is an automated message; replies aren't read.\n"
    )
    return subject, body
