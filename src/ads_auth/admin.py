"""Account changes an administrator makes from the app, and the admin log that records them.

Each operation here changes the account through `UserStore` and then writes one `admin_actions`
row: when, which administrator, what was done, to which account, and for a role change the old and
new role. A row never holds a password, temporary or otherwise. The server log gets one INFO line
per action with the administrator and the action, but not the account acted on, so that name is
kept only in the database, where it is deleted after `events.RETENTION_MONTHS`.

The log is separate from `usage_events` on purpose: managers see usage events (the Usage statistics
page and its downloads) and they feed the activity counts; this log is for administrators only.

Framework-free: no Streamlit, no I/O at import time.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ads_auth.db import connection, to_iso, utc_now
from ads_auth.models import ROLES, User, normalize_email
from ads_auth.store import StoreError, UserStore
from ads_auth.usage.events import retention_cutoff

LOGGER = logging.getLogger(__name__)

CREATED = "created"
PASSWORD_RESET = "password_reset"
ROLE_CHANGED = "role_changed"
DEACTIVATED = "deactivated"
REACTIVATED = "reactivated"
DELETED = "deleted"
NAME_CHANGED = "name_changed"
EMAIL_CHANGED = "email_changed"
ACTIONS = (CREATED, PASSWORD_RESET, ROLE_CHANGED, DEACTIVATED, REACTIVATED, DELETED, NAME_CHANGED, EMAIL_CHANGED)

ACTION_LABELS = {
    CREATED: "Account created",
    PASSWORD_RESET: "Password reset",
    ROLE_CHANGED: "Role changed",
    DEACTIVATED: "Deactivated",
    REACTIVATED: "Reactivated",
    DELETED: "Deleted",
    NAME_CHANGED: "Name changed",
    EMAIL_CHANGED: "Email changed",
}


@dataclass(frozen=True)
class AdminAction:
    ts_utc: str
    admin: str
    action: str
    account: str
    detail: str | None


def record(conn: sqlite3.Connection, admin: User, action: str, account: str, *,
           detail: str | None = None, now: datetime | None = None) -> None:
    """Append one row. `detail` holds the old and new role for a role change ("user → manager"), whether
    an email was set or removed, and the role a new account was given. Never a name or an address."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown admin action: {action!r}")
    with conn:
        conn.execute(
            "INSERT INTO admin_actions(ts_utc, admin, action, account, detail) VALUES (?, ?, ?, ?, ?)",
            (to_iso(now or utc_now()), admin.username, action, account, detail),
        )
    LOGGER.info("admin action: %s by %s", action, admin.username)


def recent(conn: sqlite3.Connection, limit: int = 500) -> list[AdminAction]:
    """Newest first."""
    rows = conn.execute(
        "SELECT ts_utc, admin, action, account, detail FROM admin_actions ORDER BY ts_utc DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [AdminAction(r["ts_utc"], r["admin"], r["action"], r["account"], r["detail"]) for r in rows]


def prune(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Delete rows older than the usage retention period (13 months)."""
    cutoff = to_iso(retention_cutoff(now or utc_now()))
    with conn:
        cur = conn.execute("DELETE FROM admin_actions WHERE ts_utc < ?", (cutoff,))
    return cur.rowcount


# ---- the operations ----------------------------------------------------------------------------
#
# Each takes the acting administrator. An administrator cannot act on their own account here (their
# own password is changed with the Change password form; a role change or deactivation would end
# their own session), and the store refuses to remove the last active admin.

def _check(admin: User, username: str) -> None:
    if not admin.has_role("admin"):
        raise StoreError("Only an administrator can manage users.")
    if admin.id == 0:
        raise StoreError("Managing users is not available while sign-in is disabled.")
    if admin.username == username:
        raise StoreError("You can't change your own account here.")


def _existing(store: UserStore, username: str) -> User:
    user = store.get_by_username(username)
    if user is None:
        raise StoreError("User not found.")
    return user


def _log(db_path, admin: User, action: str, account: str, detail: str | None = None) -> None:
    with connection(db_path) as conn:
        record(conn, admin, action, account, detail=detail)


def create_account(db_path, admin: User, username: str, full_name: str, role: str,
                   email: str | None = None) -> tuple[User, str]:
    """New account with a temporary password, returned once. A blank `email` takes the username when
    the username is an address."""
    full_name = (full_name or "").strip()
    if not full_name:
        raise StoreError("Name is required.")
    _check(admin, "")
    user, temp_password = UserStore(db_path).create_user(username, full_name=full_name, role=role, temporary=True,
                                                         email=(email or "").strip() or None)
    _log(db_path, admin, CREATED, user.username, f"as {role}")
    return user, temp_password


def reset_password(db_path, admin: User, username: str) -> str:
    """Temporary password, forced change at next sign-in, sessions signed out, lockout cleared."""
    _check(admin, username)
    store = UserStore(db_path)
    user = _existing(store, username)
    if not user.is_active:
        raise StoreError("This account is deactivated. Reactivate it instead.")
    temp_password = store.set_temporary_password(user.username)
    _log(db_path, admin, PASSWORD_RESET, user.username)
    return temp_password


def change_role(db_path, admin: User, username: str, role: str) -> User:
    _check(admin, username)
    store = UserStore(db_path)
    before = _existing(store, username)
    if before.role == role:
        return before
    after = store.set_role(before.username, role)
    _log(db_path, admin, ROLE_CHANGED, after.username, f"{before.role} → {role}")
    return after


def edit_account(db_path, admin: User, username: str, *, full_name: str, email: str | None, role: str) -> User:
    """Name, email and role in one save. Only what changed is written and logged; a role change signs
    the account out, a name or email change doesn't. Everything is checked before anything is written."""
    _check(admin, username)
    store = UserStore(db_path)
    before = _existing(store, username)
    full_name = (full_name or "").strip()
    if not full_name:
        raise StoreError("Name is required.")
    address = normalize_email(email)            # raises ValueError before anything changes
    if role not in ROLES:
        raise StoreError(f"Role must be one of {', '.join(ROLES)}.")
    user = before
    if full_name != (before.full_name or ""):
        user = store.set_full_name(before.username, full_name)
        _log(db_path, admin, NAME_CHANGED, before.username)
    if address != before.email:
        user = store.set_email(before.username, address)
        _log(db_path, admin, EMAIL_CHANGED, before.username, "removed" if address is None else "set")
    if role != before.role:
        user = store.set_role(before.username, role)
        _log(db_path, admin, ROLE_CHANGED, before.username, f"{before.role} → {role}")
    return user


def deactivate(db_path, admin: User, username: str) -> User:
    _check(admin, username)
    user = UserStore(db_path).set_active(username, False)
    _log(db_path, admin, DEACTIVATED, user.username)
    return user


def reactivate(db_path, admin: User, username: str) -> str:
    """Turn the account back on with a fresh temporary password, so an old password does not
    come back to life. Returns the temporary password, once."""
    _check(admin, username)
    store = UserStore(db_path)
    user = store.set_active(username, True)
    temp_password = store.set_temporary_password(user.username)
    _log(db_path, admin, REACTIVATED, user.username)
    return temp_password


def delete_account(db_path, admin: User, username: str) -> User:
    _check(admin, username)
    user = UserStore(db_path).delete_user(username)
    _log(db_path, admin, DELETED, user.username)
    return user
