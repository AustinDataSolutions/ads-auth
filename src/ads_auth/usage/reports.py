"""Weekly and monthly active users, per-user totals, and CSV exports.

Storage is UTC; display periods are computed in America/New_York. Events are
few (thousands per year), so aggregation happens in Python after one query.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ads_auth.db import from_iso, to_iso, utc_now
from ads_auth.usage.events import ACTIVITY_EVENTS

DISPLAY_TZ = ZoneInfo("America/New_York")

COMBINED_HEADER = ["ts_utc", "app", "username", "event"]
AGGREGATE_HEADER = ["period_type", "period_start", "period_end", "active_users", "logins", "searches", "uploads", "complete"]
PER_USER_HEADER = ["username", "logins", "searches", "uploads"]


@dataclass(frozen=True)
class PeriodRow:
    period_type: str      # "week" | "month"
    period_start: date    # inclusive, local
    period_end: date      # inclusive, local
    active_users: int
    logins: int
    searches: int
    uploads: int
    complete: bool


@dataclass(frozen=True)
class UserRow:
    username: str
    logins: int
    searches: int
    uploads: int


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())            # ISO week: Monday


def month_start(d: date) -> date:
    return d.replace(day=1)


def next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def local_date(ts_utc: str) -> date:
    return from_iso(ts_utc).astimezone(DISPLAY_TZ).date()


def _fetch(conn: sqlite3.Connection, start: date, end: date, exclude=None) -> list[sqlite3.Row]:
    """Events whose local date falls in [start, end]; fetch with a one-day UTC margin and filter precisely.

    `exclude` is a set of usernames whose events are left out (test and operator accounts).
    """
    lo = to_iso(datetime.combine(start - timedelta(days=1), datetime.min.time(), tzinfo=DISPLAY_TZ))
    hi = to_iso(datetime.combine(end + timedelta(days=2), datetime.min.time(), tzinfo=DISPLAY_TZ))
    rows = conn.execute(
        "SELECT ts_utc, user_id, username, event FROM usage_events WHERE ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
        (lo, hi),
    ).fetchall()
    excluded = set(exclude or ())
    return [r for r in rows if start <= local_date(r["ts_utc"]) <= end and r["username"] not in excluded]


def _periods(start: date, end: date, kind: str):
    cur = week_start(start) if kind == "week" else month_start(start)
    while cur <= end:
        nxt = cur + timedelta(days=7) if kind == "week" else next_month(cur)
        yield cur, nxt - timedelta(days=1)
        cur = nxt


def active_users_by_period(conn: sqlite3.Connection, start: date, end: date, kind: str, now: datetime | None = None, exclude=None) -> list[PeriodRow]:
    """One row per period in [start, end], oldest first, including zero-count periods; the current period is marked incomplete."""
    if kind not in ("week", "month"):
        raise ValueError("kind must be 'week' or 'month'")
    today = (now or utc_now()).astimezone(DISPLAY_TZ).date()
    rows = _fetch(conn, start, end, exclude)
    out = []
    for p_start, p_end in _periods(start, end, kind):
        in_period = [r for r in rows if p_start <= local_date(r["ts_utc"]) <= p_end]
        active = {r["user_id"] for r in in_period if r["event"] in ACTIVITY_EVENTS and r["user_id"] is not None}
        out.append(PeriodRow(
            period_type=kind,
            period_start=p_start,
            period_end=p_end,
            active_users=len(active),
            logins=sum(1 for r in in_period if r["event"] == "login"),
            searches=sum(1 for r in in_period if r["event"] == "search"),
            uploads=sum(1 for r in in_period if r["event"] == "upload"),
            complete=p_end < today,
        ))
    return out


def per_user_totals(conn: sqlite3.Connection, start: date, end: date, exclude=None) -> list[UserRow]:
    totals: dict[str, dict[str, int]] = {}
    for r in _fetch(conn, start, end, exclude):
        if r["username"] is None:
            continue
        t = totals.setdefault(r["username"], {"login": 0, "search": 0, "upload": 0})
        if r["event"] in t:
            t[r["event"]] += 1
    return [UserRow(u, t["login"], t["search"], t["upload"]) for u, t in sorted(totals.items())]


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def combined_events_csv(conn: sqlite3.Connection, app_name: str, exclude=None) -> str:
    """Exactly ts_utc,app,username,event so exports from every ADS app concatenate.

    `app_name` fills the `app` column; consuming apps pass `settings.app_name`.
    """
    excluded = set(exclude or ())
    rows = conn.execute("SELECT ts_utc, username, event FROM usage_events ORDER BY ts_utc, id").fetchall()
    return _csv(COMBINED_HEADER, ((r["ts_utc"], app_name, r["username"] or "", r["event"]) for r in rows if r["username"] not in excluded))


def aggregate_csv(rows: list[PeriodRow]) -> str:
    return _csv(AGGREGATE_HEADER, (
        (r.period_type, r.period_start.isoformat(), r.period_end.isoformat(),
         r.active_users, r.logins, r.searches, r.uploads, "yes" if r.complete else "no")
        for r in rows
    ))


def per_user_csv(rows: list[UserRow]) -> str:
    return _csv(PER_USER_HEADER, ((r.username, r.logins, r.searches, r.uploads) for r in rows))
