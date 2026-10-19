"""Housekeeping run inside the app process: daily database backup and event pruning.

An app may pass one extra callable (`extra`) that runs after the generic pass, for example an
hourly check that removes expired app data even when nobody opens the app. It receives the
current time and may return a mapping that is merged into the pass result.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path

from ads_auth import db
from ads_auth.usage import events

BACKUP_PREFIX = "app-"
BACKUP_KEEP = 7
BACKUP_INTERVAL = timedelta(hours=24)
CHECK_INTERVAL_SECONDS = 3600

ExtraTask = Callable[[datetime], Mapping | None]

log = logging.getLogger(__name__)


def backups_dir(data_dir) -> Path:
    return Path(data_dir) / "backups"


def list_backups(data_dir) -> list[Path]:
    folder = backups_dir(data_dir)
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{BACKUP_PREFIX}*.db"))


def make_backup(data_dir, db_path, now: datetime | None = None) -> Path:
    """Consistent copy named by UTC time; keeps the newest BACKUP_KEEP files."""
    now = now or db.utc_now()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    dest = db.backup(db_path, backups_dir(data_dir) / f"{BACKUP_PREFIX}{stamp}.db")
    for old in list_backups(data_dir)[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return dest


def backup_due(data_dir, now: datetime | None = None) -> bool:
    files = list_backups(data_dir)
    if not files:
        return True
    newest = datetime.strptime(files[-1].name[len(BACKUP_PREFIX):-3], "%Y%m%dT%H%M%SZ").replace(tzinfo=db.utc_now().tzinfo)
    return (now or db.utc_now()) - newest >= BACKUP_INTERVAL


def run_once(data_dir, db_path, now: datetime | None = None, *, extra: ExtraTask | None = None) -> dict:
    """One maintenance pass. Returns what it did, for logs and tests."""
    now = now or db.utc_now()
    done: dict = {"backup": None, "pruned": 0}
    if backup_due(data_dir, now):
        done["backup"] = make_backup(data_dir, db_path, now)
    with db.connection(db_path) as conn:
        done["pruned"] = events.prune_events(conn, now)
    if extra is not None:
        done.update(extra(now) or {})
    return done


def start_background(data_dir, db_path, interval_seconds: int = CHECK_INTERVAL_SECONDS, *, extra: ExtraTask | None = None) -> threading.Thread:
    """Daemon thread: run_once immediately, then every `interval_seconds`. Errors are logged, never raised."""
    def loop():
        while True:
            try:
                run_once(data_dir, db_path, extra=extra)
            except Exception:  # noqa: BLE001 - housekeeping must never take the app down
                log.exception("maintenance pass failed")
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, name="maintenance", daemon=True)
    thread.start()
    return thread
