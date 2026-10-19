"""Operator CLI for accounts, backups, and usage events.

An app's `manage.py` wraps `main()` with its own settings loader and any extra commands:

    from ads_auth import cli
    from core.config import load_settings
    sys.exit(cli.main(load_settings=load_settings, commands=MY_COMMANDS))

Passwords are never accepted as arguments; they are prompted for or generated and shown once.
"""
from __future__ import annotations

import argparse
import getpass
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ads_auth import db, maintenance
from ads_auth.models import ROLES
from ads_auth.settings import AppSettings
from ads_auth.store import StoreError, UserStore
from ads_auth.usage import events, reports

LOCK_NAME = "serving.lock"


@dataclass(frozen=True)
class Command:
    """An app-specific subcommand. `handler(cli, args)` returns the exit code;
    `arguments(parser)` adds its options."""

    name: str
    handler: Callable[["Cli", argparse.Namespace], int]
    help: str = ""
    arguments: Callable[[argparse.ArgumentParser], None] | None = None


class Cli:
    def __init__(self, settings: AppSettings, out=print, prompt=getpass.getpass):
        self.settings = settings
        self.out = out
        self.prompt = prompt
        self.store = UserStore(settings.db_path)

    # ---- helpers for app commands ----------------------------------------

    def refuse_if_serving(self, force: bool) -> bool:
        """True (after printing why) when the app may be serving and `--force` was not given."""
        lock = self.settings.data_dir / LOCK_NAME
        if lock.exists() and not force:
            self.out(f"Refusing: {lock} exists, which means the app may be serving. Stop the service first, or pass --force if you are sure.")
            return True
        return False

    # ---- accounts ----------------------------------------------------------

    def create_user(self, a) -> int:
        if a.temporary:
            user, temp = self.store.create_user(a.username, full_name=a.full_name, role=a.role, temporary=True)
            self.out(f"Created {user.username} ({user.role}). Temporary password, shown once, expires in 90 days:")
            self.out(temp)
            return 0
        password = self.prompt("Password: ")
        if password != self.prompt("Confirm password: "):
            self.out("Passwords do not match.")
            return 1
        user, _ = self.store.create_user(a.username, password=password, full_name=a.full_name, role=a.role)
        self.out(f"Created {user.username} ({user.role}).")
        return 0

    def set_password(self, a) -> int:
        temp = self.store.set_temporary_password(a.username)
        self.out("Temporary password, shown once, expires in 90 days. Other sessions are signed out:")
        self.out(temp)
        return 0

    def set_role(self, a) -> int:
        user = self.store.set_role(a.username, a.role)
        self.out(f"{user.username} is now {user.role}.")
        return 0

    def set_name(self, a) -> int:
        user = self.store.set_full_name(a.username, a.full_name)
        self.out(f"{user.username}: display name set.")
        return 0

    def deactivate_user(self, a) -> int:
        user = self.store.set_active(a.username, False)
        self.out(f"{user.username} deactivated; sessions signed out.")
        return 0

    def delete_user(self, a) -> int:
        if not a.force:
            self.out("Refusing: deleting an account cannot be undone. Deactivate it instead, or pass --force.")
            return 1
        user = self.store.delete_user(a.username)
        self.out(f"{user.username} deleted. Usage history keeps the name.")
        return 0

    def reactivate_user(self, a) -> int:
        user = self.store.set_active(a.username, True)
        self.out(f"{user.username} reactivated.")
        return 0

    def unlock_user(self, a) -> int:
        user = self.store.unlock(a.username)
        self.out(f"{user.username} unlocked.")
        return 0

    def list_users(self, a) -> int:
        rows = self.store.list_users()
        if not rows:
            self.out("No users.")
            return 0
        self.out(f"{'username':<40} {'role':<8} {'active':<7} {'must_change':<12} last_login_utc")
        for u in rows:
            self.out(f"{u.username:<40} {u.role:<8} {'yes' if u.is_active else 'no':<7} {'yes' if u.must_change_password else 'no':<12} {u.last_login_utc or '-'}")
        return 0

    def import_users(self, a) -> int:
        report = self.store.import_from_sqlite(a.source, set(a.allow), apply=a.apply)
        for label, names in (
            ("Will import" if not report.applied else "Imported", report.planned),
            ("Already exist (skipped)", report.skipped_existing),
            ("Not found in source", report.not_found),
            ("Unsupported hash format", report.unsupported_hash),
            ("Invalid username", report.invalid_name),
        ):
            if names:
                self.out(f"{label}: {', '.join(names)}")
        for a_name, b_name in report.collisions:
            self.out(f"Collision after normalization: {a_name!r} and {b_name!r}")
        if not report.ok:
            self.out("Nothing applied: resolve the problems above first.")
            return 1
        if not a.apply:
            self.out("Dry run. Re-run with --apply to import.")
        return 0

    # ---- database ----------------------------------------------------------

    def backup_db(self, a) -> int:
        dest = maintenance.make_backup(self.settings.data_dir, self.settings.db_path)
        self.out(str(dest))
        return 0

    def restore_db(self, a) -> int:
        if self.refuse_if_serving(a.force):
            return 1
        source = Path(a.backup)
        if not source.is_file():
            self.out("Backup file not found.")
            return 1
        db.backup(source, self.settings.db_path)      # consistent copy in the other direction
        with db.connection(self.settings.db_path) as conn:
            pruned = events.prune_events(conn)
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        self.out(f"Restored. users={users} events={count} pruned={pruned}")
        return 0

    # ---- usage events ------------------------------------------------------

    def prune_events(self, a) -> int:
        with db.connection(self.settings.db_path) as conn:
            self.out(f"Pruned {events.prune_events(conn)} events older than {events.RETENTION_MONTHS} months.")
        return 0

    def export_events(self, a) -> int:
        with db.connection(self.settings.db_path) as conn:
            text = reports.combined_events_csv(conn, self.settings.app_name)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
            self.out(f"Wrote {a.out}")
        else:
            self.out(text.rstrip("\n"))
        return 0


def build_parser(prog: str = "manage.py", commands: Iterable[Command] = ()) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Operator CLI for accounts, backups, and usage events. Passwords are prompted for or generated and shown once, never passed as arguments.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("create-user"); s.add_argument("username"); s.add_argument("--full-name"); s.add_argument("--role", choices=ROLES, default="user"); s.add_argument("--temporary", action="store_true", help="generate a temporary password instead of prompting")
    s = sub.add_parser("set-password"); s.add_argument("username")
    s = sub.add_parser("set-role"); s.add_argument("username"); s.add_argument("role", choices=ROLES)
    s = sub.add_parser("set-name"); s.add_argument("username"); s.add_argument("full_name")
    s = sub.add_parser("deactivate-user"); s.add_argument("username")
    s = sub.add_parser("reactivate-user"); s.add_argument("username")
    s = sub.add_parser("delete-user"); s.add_argument("username"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("unlock-user"); s.add_argument("username")
    sub.add_parser("list-users")
    s = sub.add_parser("import-users"); s.add_argument("source"); s.add_argument("--allow", action="append", default=[], required=True); s.add_argument("--apply", action="store_true")
    sub.add_parser("backup-db")
    s = sub.add_parser("restore-db"); s.add_argument("backup"); s.add_argument("--force", action="store_true")
    sub.add_parser("prune-events")
    s = sub.add_parser("export-events"); s.add_argument("--out")
    for command in commands:
        s = sub.add_parser(command.name, help=command.help or None)
        if command.arguments is not None:
            command.arguments(s)
    return p


def main(
    argv=None,
    *,
    load_settings: Callable[..., AppSettings],
    environ=None,
    out=print,
    prompt=getpass.getpass,
    prog: str = "manage.py",
    commands: Iterable[Command] = (),
) -> int:
    """Parse `argv`, load settings with `load_settings(environ)`, migrate the database, run the command."""
    commands = tuple(commands)
    args = build_parser(prog, commands).parse_args(argv)
    environ = os.environ if environ is None else environ
    settings = load_settings(environ)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(settings.db_path)
    cli = Cli(settings, out=out, prompt=prompt)
    extra = {c.name: c.handler for c in commands}
    try:
        if args.command in extra:
            return extra[args.command](cli, args)
        return getattr(cli, args.command.replace("-", "_"))(args)
    except (StoreError, ValueError) as exc:
        out(f"Error: {exc}")
        return 1
