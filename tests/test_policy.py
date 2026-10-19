from datetime import datetime, timedelta, timezone

from ads_auth import db, policy
from ads_auth.models import User

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)


def _user(active=True, version=1):
    return User(1, "a", None, "user", active, False, None, version, db.to_iso(T0), None)


def test_session_status_paths():
    S = policy.SessionStatus
    assert policy.session_status(None, 1, db.to_iso(T0), now=T0) is S.MISSING
    assert policy.session_status(_user(), None, db.to_iso(T0), now=T0) is S.MISSING
    assert policy.session_status(_user(), 1, db.to_iso(T0), now=T0 + timedelta(hours=7, minutes=59)) is S.OK
    assert policy.session_status(_user(), 1, db.to_iso(T0), now=T0 + timedelta(hours=8)) is S.EXPIRED
    assert policy.session_status(_user(active=False), 1, db.to_iso(T0), now=T0) is S.INACTIVE
    assert policy.session_status(_user(version=2), 1, db.to_iso(T0), now=T0) is S.REVOKED


def test_failures_prune_and_lock(tmp_path):
    path = tmp_path / "app.db"
    db.migrate(path)
    with db.connection(path) as conn:
        for i in range(4):
            policy.record_failure(conn, "a", T0 + timedelta(seconds=i))
        assert not policy.is_locked(conn, "a", T0 + timedelta(seconds=5))
        policy.record_failure(conn, "a", T0 + timedelta(seconds=5))
        assert policy.is_locked(conn, "a", T0 + timedelta(seconds=6))
        assert not policy.is_locked(conn, "b", T0)
        # old rows are pruned on the next write
        policy.record_failure(conn, "b", T0 + timedelta(minutes=20))
        assert conn.execute("SELECT COUNT(*) FROM login_attempts WHERE username='a'").fetchone()[0] == 0
