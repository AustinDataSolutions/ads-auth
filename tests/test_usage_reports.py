from datetime import date, datetime, timezone

import pytest

from ads_auth import db
from ads_auth.models import User
from ads_auth.usage import events, reports

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def _user(i, name):
    return User(i, name, None, "user", True, False, None, 1, db.to_iso(T0), None)


A, B = _user(1, "a@example.org"), _user(2, "b@example.org")
EVENTS = ("search", "upload")


def log(conn, event, **kw):
    events.log_event(conn, event, allowed=EVENTS, **kw)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "app.db"
    db.migrate(path)
    with db.connection(path) as c:
        yield c


def test_week_and_month_helpers():
    assert reports.week_start(date(2026, 10, 15)) == date(2026, 10, 12)      # Thursday -> Monday
    assert reports.next_month(date(2026, 12, 3)) == date(2027, 1, 1)
    assert reports.local_date("2026-03-09T03:30:00+00:00") == date(2026, 3, 8)  # DST starts 2026-03-08; 03:30Z is 23:30 EDT the day before


def test_weekly_active_users_with_zero_periods_and_incomplete_flag(conn):
    log(conn, "login", user=A, now=datetime(2026, 9, 29, 15, tzinfo=timezone.utc))   # week of Sep 28
    log(conn, "search", user=A, now=datetime(2026, 9, 29, 15, 5, tzinfo=timezone.utc))
    log(conn, "login_failed", user=B, now=datetime(2026, 10, 6, 15, tzinfo=timezone.utc))  # not activity
    log(conn, "upload", user=B, now=datetime(2026, 10, 13, 15, tzinfo=timezone.utc))  # current week
    rows = reports.active_users_by_period(conn, date(2026, 9, 28), date(2026, 10, 15), "week", now=T0, events=EVENTS)
    assert [(r.period_start, r.active_users, r.logins, r.counts["search"], r.counts["upload"], r.complete) for r in rows] == [
        (date(2026, 9, 28), 1, 1, 1, 0, True),
        (date(2026, 10, 5), 0, 0, 0, 0, True),
        (date(2026, 10, 12), 1, 0, 0, 1, False),
    ]
    assert list(rows[0].counts) == ["search", "upload"]                   # declared order


def test_activity_subset_and_core_rules(conn):
    log(conn, "login", user=A, now=T0)                  # always activity
    log(conn, "logout", user=A, now=T0)                 # never activity, never counted
    log(conn, "upload", user=B, now=T0)
    log(conn, "search", user=_user(3, "c@example.org"), now=T0)
    month = (date(2026, 10, 1), date(2026, 10, 31))
    everything = reports.active_users_by_period(conn, *month, "month", now=T0, events=EVENTS)[0]
    assert everything.active_users == 3 and everything.logins == 1 and dict(everything.counts) == {"search": 1, "upload": 1}
    only_uploads = reports.active_users_by_period(conn, *month, "month", now=T0, events=EVENTS, activity=("upload",))[0]
    assert only_uploads.active_users == 2 and dict(only_uploads.counts) == {"search": 1, "upload": 1}
    none = reports.active_users_by_period(conn, *month, "month", now=T0, events=EVENTS, activity=())[0]
    assert none.active_users == 1                        # the login alone
    undeclared = reports.active_users_by_period(conn, *month, "month", now=T0)[0]
    assert undeclared.active_users == 1 and dict(undeclared.counts) == {}


def test_monthly_counts_use_eastern_time_boundaries(conn):
    # 2026-10-01 02:30 UTC is 2026-09-30 22:30 EDT -> belongs to September
    events.log_event(conn, "login", user=A, now=datetime(2026, 10, 1, 2, 30, tzinfo=timezone.utc))
    events.log_event(conn, "login", user=B, now=datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc))  # 01:30 EDT Oct 1
    rows = reports.active_users_by_period(conn, date(2026, 9, 1), date(2026, 10, 31), "month", now=T0)
    assert [(r.period_start, r.active_users, r.complete) for r in rows] == [
        (date(2026, 9, 1), 1, True),
        (date(2026, 10, 1), 1, False),
    ]


def test_per_user_totals_and_csv(conn):
    log(conn, "login", user=A, now=T0)
    log(conn, "search", user=A, now=T0)
    log(conn, "search", user=A, now=T0)
    log(conn, "upload", user=B, now=T0)
    log(conn, "login_failed", now=T0)
    rows = reports.per_user_totals(conn, date(2026, 10, 1), date(2026, 10, 31), events=EVENTS)
    assert [(r.username, r.logins, dict(r.counts)) for r in rows] == [
        ("a@example.org", 1, {"search": 2, "upload": 0}), ("b@example.org", 0, {"search": 0, "upload": 1}),
    ]
    assert reports.per_user_csv(rows, EVENTS) == "username,logins,search,upload\na@example.org,1,2,0\nb@example.org,0,0,1\n"
    assert reports.per_user_csv(rows) == "username,logins\na@example.org,1\nb@example.org,0\n"


def test_combined_csv_matches_the_shared_export_contract(conn):
    events.log_event(conn, "login", user=A, now=datetime(2026, 10, 12, 21, 28, 16, tzinfo=timezone.utc))
    events.log_event(conn, "login_failed", now=datetime(2026, 10, 12, 21, 29, 0, tzinfo=timezone.utc))
    events.log_event(conn, "logout", user=A, now=datetime(2026, 10, 12, 22, 0, 0, tzinfo=timezone.utc))
    assert reports.combined_events_csv(conn, "example-app") == (
        "ts_utc,app,username,event\n"
        "2026-10-12T21:28:16+00:00,example-app,a@example.org,login\n"
        "2026-10-12T21:29:00+00:00,example-app,,login_failed\n"
        "2026-10-12T22:00:00+00:00,example-app,a@example.org,logout\n"
    )


def test_aggregate_csv_shape():
    row = reports.PeriodRow("week", date(2026, 10, 12), date(2026, 10, 18), 3, 4, {"search": 10, "upload": 1}, False)
    assert reports.aggregate_csv([row], EVENTS) == (
        "period_type,period_start,period_end,active_users,logins,search,upload,complete\n"
        "week,2026-10-12,2026-10-18,3,4,10,1,no\n"
    )
    assert reports.aggregate_csv([row], ("upload",)) == (
        "period_type,period_start,period_end,active_users,logins,upload,complete\nweek,2026-10-12,2026-10-18,3,4,1,no\n"
    )
    assert reports.aggregate_csv([]) == "period_type,period_start,period_end,active_users,logins,complete\n"


def test_invalid_kind(conn):
    with pytest.raises(ValueError):
        reports.active_users_by_period(conn, date(2026, 1, 1), date(2026, 1, 2), "day")


def test_exclusions_apply_to_periods_totals_and_combined_csv(tmp_path):
    from datetime import date, datetime, timezone
    from ads_auth import db as _db
    from ads_auth.models import User
    from ads_auth.usage import events, reports as _reports
    path = tmp_path / "app.db"
    _db.migrate(path)
    pat = User(1, "pat", None, "manager", True, False, None, 1, "2026-01-01T00:00:00+00:00", None)
    dev = User(2, "ops-test", None, "admin", True, False, None, 1, "2026-01-01T00:00:00+00:00", None)
    with _db.connection(path) as conn:
        for who in (pat, dev):
            events.log_event(conn, "login", user=who, now=datetime(2026, 10, 1, 15, tzinfo=timezone.utc))
            events.log_event(conn, "search", user=who, now=datetime(2026, 10, 1, 15, 1, tzinfo=timezone.utc), allowed=("search",))
        monthly = _reports.active_users_by_period(conn, date(2026, 10, 1), date(2026, 10, 31), "month", exclude={"ops-test"}, events=("search",))
        assert monthly[0].active_users == 1 and monthly[0].logins == 1 and monthly[0].counts["search"] == 1
        assert [u.username for u in _reports.per_user_totals(conn, date(2026, 10, 1), date(2026, 10, 31), exclude={"ops-test"})] == ["pat"]
        csv_text = _reports.combined_events_csv(conn, "demo-app", exclude={"ops-test"})
        assert "ops-test" not in csv_text and csv_text.count("pat") == 2
        assert _reports.active_users_by_period(conn, date(2026, 10, 1), date(2026, 10, 31), "month")[0].active_users == 2
