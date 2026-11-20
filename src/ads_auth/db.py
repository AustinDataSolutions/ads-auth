"""SQLite access and versioned, idempotent migrations.

Every operation opens its own short-lived connection (WAL mode, busy timeout,
foreign keys on). Nothing here is cached across threads.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_version (
      version     INTEGER NOT NULL,
      applied_utc TEXT    NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
      id                        INTEGER PRIMARY KEY,
      username                  TEXT    NOT NULL UNIQUE,
      password_hash             TEXT    NOT NULL,
      full_name                 TEXT,
      role                      TEXT    NOT NULL DEFAULT 'user',
      is_active                 INTEGER NOT NULL DEFAULT 1,
      must_change_password      INTEGER NOT NULL DEFAULT 0,
      temp_password_expires_utc TEXT,
      session_version           INTEGER NOT NULL DEFAULT 1,
      created_utc               TEXT    NOT NULL,
      last_login_utc            TEXT
    );
    CREATE TABLE IF NOT EXISTS login_attempts (
      username   TEXT NOT NULL,
      failed_utc TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_login_attempts ON login_attempts(username, failed_utc);
    CREATE TABLE IF NOT EXISTS usage_events (
      id       INTEGER PRIMARY KEY,
      ts_utc   TEXT NOT NULL,
      user_id  INTEGER,
      username TEXT,
      event    TEXT NOT NULL,
      outcome  TEXT,
      job_id   TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_usage_events_ts   ON usage_events(ts_utc);
    CREATE INDEX IF NOT EXISTS ix_usage_events_user ON usage_events(user_id, ts_utc);
    """,
    # Account changes made from the sidebar's Manage users section: who did what to which account.
    # Kept apart from usage_events so it never reaches managers, the downloads, or activity counts.
    2: """
    CREATE TABLE IF NOT EXISTS admin_actions (
      id       INTEGER PRIMARY KEY,
      ts_utc   TEXT NOT NULL,
      admin    TEXT NOT NULL,
      action   TEXT NOT NULL,
      account  TEXT NOT NULL,
      detail   TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_admin_actions_ts ON admin_actions(ts_utc);
    """,
    # Self-service password reset: an optional email per account (filled from the username where the
    # username is an address) and the outstanding reset links, kept only as a SHA-256 of the token.
    3: """
    ALTER TABLE users ADD COLUMN email TEXT;
    UPDATE users SET email = username
      WHERE email IS NULL AND username LIKE '%_@_%._%' AND username NOT LIKE '%@%@%';
    CREATE TABLE IF NOT EXISTS password_resets (
      id              INTEGER PRIMARY KEY,
      user_id         INTEGER NOT NULL,
      token_hash      TEXT    NOT NULL UNIQUE,
      session_version INTEGER NOT NULL,
      created_utc     TEXT    NOT NULL,
      expires_utc     TEXT    NOT NULL,
      used_utc        TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_password_resets_user ON password_resets(user_id, created_utc);
    """,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """ISO 8601 in UTC with an explicit +00:00 offset, second precision."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connection(db_path):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    value = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    return int(value or 0)


def migrate(db_path, *, now: datetime | None = None) -> int:
    """Apply any pending migrations in order. Safe to run on every startup."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connection(db_path) as conn:
        version = current_version(conn)
        for target in range(version + 1, SCHEMA_VERSION + 1):
            with conn:
                conn.executescript(MIGRATIONS[target])
                conn.execute(
                    "INSERT INTO schema_version(version, applied_utc) VALUES (?, ?)",
                    (target, to_iso(now or utc_now())),
                )
        return current_version(conn)


def backup(db_path, dest_path) -> Path:
    """Consistent online copy using SQLite's backup API."""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with connection(db_path) as src, sqlite3.connect(str(dest)) as dst:
        src.backup(dst)
    return dest
