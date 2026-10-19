"""User persistence and login verification."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from ads_auth import hashing, policy
from ads_auth.db import connection, from_iso, to_iso, utc_now
from ads_auth.models import ROLES, User, normalize_username


class LoginOutcome(Enum):
    """Internal detail only. The UI shows one message for every non-success case."""

    SUCCESS = "success"
    UNKNOWN = "unknown"
    INACTIVE = "inactive"
    LOCKED = "locked"
    TEMP_EXPIRED = "temp_expired"
    WRONG_PASSWORD = "wrong_password"
    INVALID_INPUT = "invalid_input"


class StoreError(Exception):
    """Operational error with a message safe to show an operator."""


@dataclass
class ImportReport:
    planned: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    unsupported_hash: list[str] = field(default_factory=list)
    invalid_name: list[str] = field(default_factory=list)
    collisions: list[tuple[str, str]] = field(default_factory=list)
    applied: bool = False

    @property
    def ok(self) -> bool:
        return not (self.unsupported_hash or self.invalid_name or self.collisions)


class UserStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)

    # ---- reads ---------------------------------------------------------------

    def get_by_username(self, username: str) -> User | None:
        try:
            name = normalize_username(username)
        except ValueError:
            return None
        with connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
        return User.from_row(row) if row else None

    def get_by_id(self, user_id: int) -> User | None:
        with connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User.from_row(row) if row else None

    def list_users(self) -> list[User]:
        with connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [User.from_row(r) for r in rows]

    def count_users(self) -> int:
        with connection(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def count_active_admins(self, exclude_id: int | None = None) -> int:
        with connection(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1 AND id != ?",
                (exclude_id if exclude_id is not None else -1,),
            ).fetchone()[0]

    # ---- login -------------------------------------------------------------

    def verify_login(self, username: str, password: str, now: datetime | None = None) -> tuple[User | None, LoginOutcome]:
        now = now or utc_now()
        try:
            name = normalize_username(username)
        except ValueError:
            return None, LoginOutcome.INVALID_INPUT
        with connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
            if row is None:
                return None, LoginOutcome.UNKNOWN          # nothing recorded for unknown names
            user = User.from_row(row)
            if not user.is_active:
                return user, LoginOutcome.INACTIVE
            if policy.is_locked(conn, name, now):
                policy.record_failure(conn, name, now)
                return user, LoginOutcome.LOCKED
            if policy.temp_password_expired(user, now):
                return user, LoginOutcome.TEMP_EXPIRED
            if not hashing.verify(password, row["password_hash"]):
                policy.record_failure(conn, name, now)
                return user, LoginOutcome.WRONG_PASSWORD
            policy.clear_failures(conn, name)
            with conn:
                conn.execute("UPDATE users SET last_login_utc = ? WHERE id = ?", (to_iso(now), user.id))
            return self.get_by_id(user.id), LoginOutcome.SUCCESS

    # ---- writes ------------------------------------------------------------

    def create_user(
        self,
        username: str,
        *,
        password: str | None = None,
        full_name: str | None = None,
        role: str = "user",
        temporary: bool = False,
        now: datetime | None = None,
    ) -> tuple[User, str | None]:
        """Create a user. With `temporary=True` a random password is generated, returned once,
        and must be changed within 90 days. Otherwise `password` must satisfy the policy."""
        now = now or utc_now()
        name = normalize_username(username)
        if role not in ROLES:
            raise StoreError(f"Role must be one of {', '.join(ROLES)}.")
        temp_password = None
        if temporary:
            temp_password = hashing.generate_temporary_password()
            password = temp_password
        else:
            hashing.validate_new_password(password or "")
        expires = to_iso(now + policy.TEMP_PASSWORD_LIFETIME) if temporary else None
        with connection(self.db_path) as conn:
            try:
                with conn:
                    conn.execute(
                        """INSERT INTO users(username, password_hash, full_name, role, is_active,
                           must_change_password, temp_password_expires_utc, session_version, created_utc)
                           VALUES (?, ?, ?, ?, 1, ?, ?, 1, ?)""",
                        (name, hashing.generate(password), full_name, role, int(temporary), expires, to_iso(now)),
                    )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"User '{name}' already exists.") from exc
        return self.get_by_username(name), temp_password

    def set_temporary_password(self, username: str, now: datetime | None = None) -> str:
        """Admin reset: random temporary password, forced change, sessions revoked."""
        now = now or utc_now()
        user = self._require(username)
        temp_password = hashing.generate_temporary_password()
        with connection(self.db_path) as conn:
            with conn:
                conn.execute(
                    """UPDATE users SET password_hash=?, must_change_password=1,
                       temp_password_expires_utc=?, session_version=session_version+1 WHERE id=?""",
                    (hashing.generate(temp_password), to_iso(now + policy.TEMP_PASSWORD_LIFETIME), user.id),
                )
            policy.clear_failures(conn, user.username)
        return temp_password

    def unlock(self, username: str) -> User:
        """Clear lockout state for an account without changing anything else."""
        user = self._require(username)
        with connection(self.db_path) as conn:
            policy.clear_failures(conn, user.username)
        return user

    def change_password(self, user_id: int, current_password: str, new_password: str) -> int:
        """Self-service change. Returns the new session_version so the caller can keep its session."""
        hashing.validate_new_password(new_password)
        with connection(self.db_path) as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None or not hashing.verify(current_password, row["password_hash"]):
                raise StoreError("Current password is incorrect.")
            with conn:
                conn.execute(
                    """UPDATE users SET password_hash=?, must_change_password=0,
                       temp_password_expires_utc=NULL, session_version=session_version+1 WHERE id=?""",
                    (hashing.generate(new_password), user_id),
                )
            return conn.execute("SELECT session_version FROM users WHERE id = ?", (user_id,)).fetchone()[0]

    def set_role(self, username: str, role: str) -> User:
        if role not in ROLES:
            raise StoreError(f"Role must be one of {', '.join(ROLES)}.")
        user = self._require(username)
        if user.role == "admin" and role != "admin" and self.count_active_admins(exclude_id=user.id) == 0:
            raise StoreError("Cannot demote the last active admin.")
        with connection(self.db_path) as conn, conn:
            conn.execute("UPDATE users SET role=?, session_version=session_version+1 WHERE id=?", (role, user.id))
        return self.get_by_id(user.id)

    def set_active(self, username: str, active: bool) -> User:
        user = self._require(username)
        if not active and user.role == "admin" and self.count_active_admins(exclude_id=user.id) == 0:
            raise StoreError("Cannot deactivate the last active admin.")
        with connection(self.db_path) as conn, conn:
            conn.execute(
                "UPDATE users SET is_active=?, session_version=session_version+1 WHERE id=?",
                (int(active), user.id),
            )
        return self.get_by_id(user.id)

    def delete_user(self, username: str) -> User:
        """Remove the account row and its lockout state. Usage events keep their username snapshot
        (history survives, as the strategy requires). The last active admin cannot be deleted."""
        user = self._require(username)
        if user.role == "admin" and user.is_active and self.count_active_admins(exclude_id=user.id) == 0:
            raise StoreError("Cannot delete the last active admin.")
        with connection(self.db_path) as conn, conn:
            conn.execute("DELETE FROM login_attempts WHERE username = ?", (user.username,))
            conn.execute("DELETE FROM users WHERE id = ?", (user.id,))
        return user

    def set_full_name(self, username: str, full_name: str | None) -> User:
        user = self._require(username)
        with connection(self.db_path) as conn, conn:
            conn.execute("UPDATE users SET full_name=? WHERE id=?", (full_name, user.id))
        return self.get_by_id(user.id)

    # ---- import from another app's database ---------------------------------

    #: Source tables tried in order: this package's own `users` table first, then the `user`
    #: table of apps that store accounts with Flask-style models. Both carry these columns.
    IMPORT_SOURCE_TABLES = ("users", "user")

    def import_from_sqlite(self, source_path, allow: set[str], *, apply: bool = False, now: datetime | None = None) -> ImportReport:
        """Copy allow-listed users (username, password_hash, full_name) from another app's SQLite database.

        The source is read from a `users` table (another app using this package) when one exists,
        otherwise from a `user` table (an app storing accounts with Flask-style models). Either
        way only those three columns are read; roles, activity flags and other state stay behind.

        Dry run unless `apply=True`. Never overwrites, never prints hashes, never inherits roles.
        """
        now = now or utc_now()
        report = ImportReport()
        source = Path(source_path)
        if not source.is_file():
            raise StoreError("Source database not found.")
        wanted: dict[str, str] = {}
        for raw in allow:
            try:
                wanted[normalize_username(raw)] = raw
            except ValueError:
                report.invalid_name.append(raw)

        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        try:
            rows = self._read_import_rows(src)
        finally:
            src.close()

        by_name: dict[str, sqlite3.Row] = {}
        for row in rows:
            try:
                name = normalize_username(row["username"])
            except ValueError:
                continue
            if name in by_name and name in wanted:
                report.collisions.append((by_name[name]["username"], row["username"]))
            by_name.setdefault(name, row)

        to_insert = []
        for name, raw in sorted(wanted.items()):
            row = by_name.get(name)
            if row is None:
                report.not_found.append(raw)
            elif self.get_by_username(name) is not None:
                report.skipped_existing.append(raw)
            elif not hashing.is_supported_hash(row["password_hash"]):
                report.unsupported_hash.append(raw)
            else:
                report.planned.append(raw)
                to_insert.append((name, row["password_hash"], row["full_name"]))

        if apply and report.ok and to_insert:
            with connection(self.db_path) as conn, conn:
                conn.executemany(
                    """INSERT INTO users(username, password_hash, full_name, role, is_active,
                       must_change_password, session_version, created_utc)
                       VALUES (?, ?, ?, 'user', 1, 0, 1, ?)""",
                    [(n, h, f, to_iso(now)) for n, h, f in to_insert],
                )
            report.applied = True
        return report

    # ---- helpers -----------------------------------------------------------

    @classmethod
    def _read_import_rows(cls, src: sqlite3.Connection) -> list[sqlite3.Row]:
        """Rows from the first source table in IMPORT_SOURCE_TABLES that has the three columns."""
        for table in cls.IMPORT_SOURCE_TABLES:
            try:
                return src.execute(f"SELECT username, password_hash, full_name FROM {table}").fetchall()
            except sqlite3.Error:
                continue
        names = " or ".join(f"'{t}'" for t in cls.IMPORT_SOURCE_TABLES)
        raise StoreError(f"Source database has no readable {names} table.")

    def _require(self, username: str) -> User:
        user = self.get_by_username(username)
        if user is None:
            raise StoreError("User not found.")
        return user
