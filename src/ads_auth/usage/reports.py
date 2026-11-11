"""Daily, weekly and monthly active users, per-user totals, and CSV and Excel exports.

Storage is UTC; display periods are computed in America/New_York. Events are
few (thousands per year), so aggregation happens in Python after one query.

Counts are driven by the app's declared event names (`AppSettings.events`): every
function that counts takes `events`, the names to count in order, and the active-user
functions take `activity`, the subset that makes an account active (None: all of
`events`). `login` is always counted and always activity; `login_failed` and `logout`
are stored but never counted here.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from ads_auth.db import from_iso, to_iso, utc_now
from ads_auth.usage.events import CORE_ACTIVITY_EVENTS

DISPLAY_TZ = ZoneInfo("America/New_York")

COMBINED_HEADER = ["ts_utc", "app", "username", "event"]


def aggregate_header(events: Sequence[str] = ()) -> list[str]:
    return ["period_type", "period_start", "period_end", "active_users", "logins", *events, "complete"]


def per_user_header(events: Sequence[str] = ()) -> list[str]:
    return ["username", "logins", *events]


@dataclass(frozen=True)
class PeriodRow:
    period_type: str      # "day" | "week" | "month"
    period_start: date    # inclusive, local
    period_end: date      # inclusive, local
    active_users: int
    logins: int
    counts: Mapping[str, int]   # one entry per declared app event, in declared order
    complete: bool


@dataclass(frozen=True)
class UserRow:
    username: str
    logins: int
    counts: Mapping[str, int]   # one entry per declared app event, in declared order


def _count(rows, events: Sequence[str]) -> Mapping[str, int]:
    tally = {name: 0 for name in events}
    for r in rows:
        if r["event"] in tally:
            tally[r["event"]] += 1
    return MappingProxyType(tally)


def _activity(events: Sequence[str], activity: Sequence[str] | None) -> frozenset[str]:
    return CORE_ACTIVITY_EVENTS | frozenset(events if activity is None else activity)


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
    cur = {"day": start, "week": week_start(start), "month": month_start(start)}[kind]
    while cur <= end:
        nxt = {"day": cur + timedelta(days=1), "week": cur + timedelta(days=7), "month": next_month(cur)}[kind]
        yield cur, nxt - timedelta(days=1)
        cur = nxt


def active_users_by_period(
    conn: sqlite3.Connection, start: date, end: date, kind: str, now: datetime | None = None, exclude=None,
    *, events: Sequence[str] = (), activity: Sequence[str] | None = None,
) -> list[PeriodRow]:
    """One row per period in [start, end], oldest first, including zero-count periods; the current period is marked incomplete.

    `events` are the app's declared names to count; `activity` the subset that makes an account active
    (None: all of `events`). Consuming apps pass `settings.events` and `settings.activity_events`.
    """
    if kind not in ("day", "week", "month"):
        raise ValueError("kind must be 'day', 'week' or 'month'")
    today = (now or utc_now()).astimezone(DISPLAY_TZ).date()
    active_names = _activity(events, activity)
    rows = _fetch(conn, start, end, exclude)
    out = []
    for p_start, p_end in _periods(start, end, kind):
        in_period = [r for r in rows if p_start <= local_date(r["ts_utc"]) <= p_end]
        active = {r["user_id"] for r in in_period if r["event"] in active_names and r["user_id"] is not None}
        out.append(PeriodRow(
            period_type=kind,
            period_start=p_start,
            period_end=p_end,
            active_users=len(active),
            logins=sum(1 for r in in_period if r["event"] == "login"),
            counts=_count(in_period, events),
            complete=p_end < today,
        ))
    return out


def logins_by_user_by_week(
    conn: sqlite3.Connection, start: date, end: date, exclude=None, *, weeks: int = 52,
) -> tuple[list[date], dict[str, list[int]]]:
    """Sign-ins per account for each week in [start, end], the most recent `weeks` weeks at most.

    Returns the week starts (Mondays, oldest first) and, for every account with a sign-in in those
    weeks, its count per week in the same order, by username.
    """
    periods = list(_periods(start, end, "week"))[-weeks:]
    index = {p_start: i for i, (p_start, _) in enumerate(periods)}
    series: dict[str, list[int]] = {}
    for r in _fetch(conn, start, end, exclude):
        if r["event"] != "login" or r["username"] is None:
            continue
        week = week_start(local_date(r["ts_utc"]))
        if week in index:
            series.setdefault(r["username"], [0] * len(periods))[index[week]] += 1
    return [p_start for p_start, _ in periods], dict(sorted(series.items()))


def per_user_totals(conn: sqlite3.Connection, start: date, end: date, exclude=None, *, events: Sequence[str] = ()) -> list[UserRow]:
    """Logins and one count per declared event for every named account with events in [start, end], sorted by username."""
    by_user: dict[str, list] = {}
    for r in _fetch(conn, start, end, exclude):
        if r["username"] is None:
            continue
        by_user.setdefault(r["username"], []).append(r)
    return [
        UserRow(u, sum(1 for r in rs if r["event"] == "login"), _count(rs, events))
        for u, rs in sorted(by_user.items())
    ]


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def combined_events_rows(conn: sqlite3.Connection, app_name: str, exclude=None) -> list[tuple]:
    """(ts_utc, app, username, event) for every stored event, oldest first, excluded accounts left out."""
    excluded = set(exclude or ())
    rows = conn.execute("SELECT ts_utc, username, event FROM usage_events ORDER BY ts_utc, id").fetchall()
    return [(r["ts_utc"], app_name, r["username"] or "", r["event"]) for r in rows if r["username"] not in excluded]


def aggregate_rows(rows: list[PeriodRow], events: Sequence[str] = ()) -> list[tuple]:
    """One tuple per period in `aggregate_header(events)` order; dates stay `date`s."""
    return [
        (r.period_type, r.period_start, r.period_end, r.active_users, r.logins,
         *(r.counts.get(name, 0) for name in events), "yes" if r.complete else "no")
        for r in rows
    ]


def per_user_rows(rows: list[UserRow], events: Sequence[str] = ()) -> list[tuple]:
    """One tuple per account in `per_user_header(events)` order."""
    return [(r.username, r.logins, *(r.counts.get(name, 0) for name in events)) for r in rows]


def _iso(rows):
    return ([v.isoformat() if isinstance(v, date) else v for v in row] for row in rows)


def combined_events_csv(conn: sqlite3.Connection, app_name: str, exclude=None) -> str:
    """Exactly ts_utc,app,username,event so exports from every ADS app concatenate.

    `app_name` fills the `app` column; consuming apps pass `settings.app_name`.
    """
    return _csv(COMBINED_HEADER, combined_events_rows(conn, app_name, exclude))


def aggregate_csv(rows: list[PeriodRow], events: Sequence[str] = ()) -> str:
    """Header `period_type,period_start,period_end,active_users,logins,<one column per event>,complete`."""
    return _csv(aggregate_header(events), _iso(aggregate_rows(rows, events)))


def per_user_csv(rows: list[UserRow], events: Sequence[str] = ()) -> str:
    """Header `username,logins,<one column per event>`."""
    return _csv(per_user_header(events), per_user_rows(rows, events))


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DATA_SHEET = "Data"
ABOUT_SHEET = "About this report"


def _cell(sheet, row: int, column: int, value):
    cell = sheet.cell(row=row, column=column, value=value)
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"                  # text from accounts or settings, never a formula
    elif isinstance(value, date):
        cell.number_format = "yyyy-mm-dd"
    return cell


def _fill(sheet, header: Sequence[str], rows) -> None:
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    widths = [len(str(h)) for h in header]
    for c, h in enumerate(header, start=1):
        _cell(sheet, 1, c, h).font = Font(bold=True)
    for r, row in enumerate(rows, start=2):
        for c, value in enumerate(row, start=1):
            _cell(sheet, r, c, value)
            shown = 10 if isinstance(value, date) else len(str(value))
            widths[c - 1] = max(widths[c - 1], shown)
    for c, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(c)].width = min(max(width + 2, 8), 60)
    sheet.freeze_panes = "A2"


def xlsx_workbook(header: Sequence[str], rows, about: Sequence[tuple[str, str]]) -> bytes:
    """A two-sheet workbook: `Data` holds `header` and `rows` (the same columns as the CSV), and
    `About this report` holds the `about` (field, value) pairs that say where the figures came from."""
    from openpyxl import Workbook

    book = Workbook()
    data = book.active
    data.title = DATA_SHEET
    _fill(data, list(header), list(rows))
    if data.max_row > 1:
        data.auto_filter.ref = data.dimensions
    _fill(book.create_sheet(ABOUT_SHEET), ["Field", "Value"], list(about))
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()
