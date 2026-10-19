from datetime import datetime, timezone

import pytest

from ads_auth import db
from ads_auth.models import User
from ads_auth.usage import events

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
U = User(7, "pat@example.org", "Pat", "manager", True, False, None, 1, db.to_iso(T0), None)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "app.db"
    db.migrate(path)
    with db.connection(path) as c:
        yield c


def test_log_event_known_and_anonymous(conn):
    events.log_event(conn, "login", user=U, now=T0)
    events.log_event(conn, "login_failed", now=T0)       # unknown account: nothing identifying
    rows = conn.execute("SELECT ts_utc, user_id, username, event FROM usage_events ORDER BY id").fetchall()
    assert tuple(rows[0]) == ("2026-10-15T12:00:00+00:00", 7, "pat@example.org", "login")
    assert tuple(rows[1]) == ("2026-10-15T12:00:00+00:00", None, None, "login_failed")


def test_log_event_rejects_unknown_type(conn):
    with pytest.raises(ValueError):
        events.log_event(conn, "query_text", user=U)


def test_retention_cutoff_month_arithmetic():
    assert events.retention_cutoff(datetime(2026, 10, 15, tzinfo=timezone.utc)) == datetime(2025, 9, 15, tzinfo=timezone.utc)
    assert events.retention_cutoff(datetime(2026, 3, 31, tzinfo=timezone.utc)) == datetime(2025, 2, 28, tzinfo=timezone.utc)
    assert events.retention_cutoff(datetime(2026, 1, 10, tzinfo=timezone.utc)) == datetime(2024, 12, 10, tzinfo=timezone.utc)


def test_prune_events_boundary(conn):
    old = datetime(2025, 9, 14, 23, 59, 59, tzinfo=timezone.utc)
    keep = datetime(2025, 9, 15, 0, 0, 0, tzinfo=timezone.utc)
    events.log_event(conn, "login", user=U, now=old)
    events.log_event(conn, "login", user=U, now=keep)
    assert events.prune_events(conn, now=T0) == 1
    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1


def test_search_signature_changes_only_with_inputs():
    a = events.search_signature("refund", "", (True, True, False, False), "upload:abc")
    assert a == events.search_signature(" refund ", "", (True, True, False, False), "upload:abc")
    assert a != events.search_signature("refund", "fee", (True, True, False, False), "upload:abc")
    assert a != events.search_signature("refund", "", (False, True, False, False), "upload:abc")
    assert a != events.search_signature("refund", "", (True, True, False, False), "upload:xyz")
