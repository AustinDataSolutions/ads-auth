from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from ads_auth.db import to_iso, utc_now
from ads_auth.models import User

# The package owns these three names; every other event name is declared by the app on AppSettings.events.
CORE_EVENTS = frozenset({"login", "login_failed", "logout"})
CORE_ACTIVITY_EVENTS = frozenset({"login"})
RETENTION_MONTHS = 13


def log_event(
    conn: sqlite3.Connection,
    event: str,
    *,
    allowed: Iterable[str] = (),
    user: User | None = None,
    now: datetime | None = None,
    outcome: str | None = None,
    job_id: str | None = None,
) -> None:
    """Append one event. `user=None` records an anonymous row (used for unknown-account failures).

    `event` must be a core name or one of `allowed`, the app's declared names (`ads_auth.ui.gate.log`
    passes `settings.events`). Rows hold timestamp, account, event type, and the optional outcome
    and job id; never content.
    """
    if event not in CORE_EVENTS and event not in set(allowed):
        raise ValueError(f"Unknown event type: {event!r}")
    with conn:
        conn.execute(
            "INSERT INTO usage_events(ts_utc, user_id, username, event, outcome, job_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                to_iso(now or utc_now()),
                user.id if user else None,
                user.username if user else None,
                event,
                outcome,
                job_id,
            ),
        )


def retention_cutoff(now: datetime, months: int = RETENTION_MONTHS) -> datetime:
    """Midnight UTC on the same day-of-month `months` earlier (clamped to that month's end).

    Events timestamped before this moment are deleted; events on the cutoff day are kept.
    """
    year = now.year
    month = now.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0)


def prune_events(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    cutoff = to_iso(retention_cutoff(now or utc_now()))
    with conn:
        cur = conn.execute("DELETE FROM usage_events WHERE ts_utc < ?", (cutoff,))
    return cur.rowcount


def search_signature(query: str, exclude: str, flags: tuple, data_source: str) -> str:
    """Stable key for 'one search per changed combination'. Kept only in session memory."""
    payload = json.dumps([query.strip(), exclude.strip(), list(flags), data_source], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
