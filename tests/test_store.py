import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ads_auth import db, policy
from ads_auth.store import LoginOutcome, StoreError, UserStore

T0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
PW = "a-perfectly-adequate-password"


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "app.db"
    db.migrate(path)
    return UserStore(path)


def test_create_and_login(store):
    user, temp = store.create_user("Alice@Example.org", password=PW, full_name="Alice", role="admin", now=T0)
    assert temp is None and user.username == "alice@example.org" and user.role == "admin"
    got, outcome = store.verify_login(" ALICE@example.org ", PW, now=T0)
    assert outcome is LoginOutcome.SUCCESS and got.id == user.id
    assert got.last_login_utc == db.to_iso(T0)


def test_create_rejects_duplicates_and_bad_role_and_short_password(store):
    store.create_user("a", password=PW)
    with pytest.raises(StoreError):
        store.create_user("A", password=PW)
    with pytest.raises(StoreError):
        store.create_user("b", password=PW, role="root")
    with pytest.raises(ValueError):
        store.create_user("c", password="short")


def test_unknown_user_leaves_no_trace(store):
    user, outcome = store.verify_login("nobody", "whatever", now=T0)
    assert user is None and outcome is LoginOutcome.UNKNOWN
    with db.connection(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 0
    assert store.verify_login("", "x", now=T0)[1] is LoginOutcome.INVALID_INPUT


def test_lockout_after_five_failures_across_sessions(store):
    store.create_user("a", password=PW)
    other_session = UserStore(store.db_path)           # second session, same database
    for i in range(5):
        s = store if i % 2 == 0 else other_session
        assert s.verify_login("a", "wrong", now=T0 + timedelta(seconds=i))[1] is LoginOutcome.WRONG_PASSWORD
    assert store.verify_login("a", PW, now=T0 + timedelta(minutes=1))[1] is LoginOutcome.LOCKED
    # window passes, correct password works again and clears the counter
    assert store.verify_login("a", PW, now=T0 + timedelta(minutes=16))[1] is LoginOutcome.SUCCESS
    with db.connection(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 0


def test_inactive_user_cannot_login_and_last_admin_protected(store):
    store.create_user("admin", password=PW, role="admin")
    store.create_user("u", password=PW)
    with pytest.raises(StoreError):
        store.set_active("admin", False)
    with pytest.raises(StoreError):
        store.set_role("admin", "user")
    store.set_active("u", False)
    assert store.verify_login("u", PW, now=T0)[1] is LoginOutcome.INACTIVE
    store.create_user("admin2", password=PW, role="admin")
    store.set_active("admin", False)                   # now allowed
    assert store.count_active_admins() == 1


def test_temporary_password_flow(store):
    user, temp = store.create_user("t", temporary=True, now=T0)
    assert temp and user.must_change_password
    got, outcome = store.verify_login("t", temp, now=T0 + timedelta(hours=1))
    assert outcome is LoginOutcome.SUCCESS and got.must_change_password
    # Still valid a week later (emailed credentials are opened late); expired after 90 days.
    assert store.verify_login("t", temp, now=T0 + timedelta(days=8))[1] is LoginOutcome.SUCCESS
    assert store.verify_login("t", temp, now=T0 + timedelta(days=91))[1] is LoginOutcome.TEMP_EXPIRED


def test_admin_reset_revokes_sessions_and_self_change_clears_flag(store):
    user, _ = store.create_user("a", password=PW)
    v0 = user.session_version
    temp = store.set_temporary_password("a", now=T0)
    u1 = store.get_by_username("a")
    assert u1.session_version == v0 + 1 and u1.must_change_password
    with pytest.raises(StoreError):
        store.change_password(u1.id, "wrong-current-password", "brand-new-password-ok")
    with pytest.raises(ValueError):
        store.change_password(u1.id, temp, "short")
    v2 = store.change_password(u1.id, temp, "brand-new-password-ok")
    u2 = store.get_by_id(u1.id)
    assert v2 == v0 + 2 == u2.session_version and not u2.must_change_password
    assert store.verify_login("a", "brand-new-password-ok", now=T0)[1] is LoginOutcome.SUCCESS


def test_set_role_bumps_version(store):
    store.create_user("admin", password=PW, role="admin")
    u, _ = store.create_user("m", password=PW)
    assert store.set_role("m", "manager").session_version == u.session_version + 1


def test_list_users_has_no_hashes(store):
    store.create_user("a", password=PW)
    users = store.list_users()
    assert len(users) == 1 and not hasattr(users[0], "password_hash")


# ---- import from a another app's users.db --------------------------------

def _source_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE, password_hash VARCHAR(256) NOT NULL, full_name VARCHAR(200))")
    conn.executemany("INSERT INTO user(username, password_hash, full_name) VALUES (?, ?, ?)", rows)
    conn.commit(); conn.close()
    return path


def test_import_dry_run_then_apply(store, tmp_path):
    from werkzeug.security import generate_password_hash
    src = _source_db(tmp_path / "users.db", [
        ("Pat@Example.org", generate_password_hash("pat-password-123456"), "Pat"),
        ("lee", generate_password_hash("lee-password-1234567", method="pbkdf2:sha256"), None),
        ("old", "md5$deadbeef", "Legacy"),
        ("dev", generate_password_hash("dev"), "Dev User"),
    ])
    store.create_user("lee", password=PW)          # already present locally
    report = store.import_from_sqlite(src, {"pat@example.org", "lee", "old", "ghost"})
    assert report.planned == ["pat@example.org"]
    assert report.skipped_existing == ["lee"]
    assert report.unsupported_hash == ["old"]
    assert report.not_found == ["ghost"]
    assert not report.applied and not report.ok
    assert store.get_by_username("pat@example.org") is None   # dry run wrote nothing

    report = store.import_from_sqlite(src, {"pat@example.org"}, apply=True, now=T0)
    assert report.applied and report.ok
    pat = store.get_by_username("pat@example.org")
    assert pat.role == "user" and pat.full_name == "Pat" and not pat.must_change_password
    assert store.verify_login("PAT@example.org", "pat-password-123456", now=T0)[1] is LoginOutcome.SUCCESS
    assert store.get_by_username("dev") is None                  # not allow-listed


def test_import_detects_normalization_collision(store, tmp_path):
    from werkzeug.security import generate_password_hash
    src = _source_db(tmp_path / "users.db", [
        ("Alice", generate_password_hash("x" * 15), None),
        ("alice", generate_password_hash("y" * 15), None),
    ])
    report = store.import_from_sqlite(src, {"alice"}, apply=True)
    assert report.collisions == [("Alice", "alice")] and not report.applied


def test_import_missing_source(store, tmp_path):
    with pytest.raises(StoreError):
        store.import_from_sqlite(tmp_path / "nope.db", {"a"})


def test_delete_user_keeps_events_and_protects_last_admin(tmp_path):
    from ads_auth import db as _db
    from ads_auth.store import StoreError, UserStore
    from ads_auth.usage import events
    path = tmp_path / "app.db"
    _db.migrate(path)
    store = UserStore(path)
    admin, _ = store.create_user("root", password="a-perfectly-adequate-password", role="admin")
    user, _ = store.create_user("gone", password="a-perfectly-adequate-password")
    with _db.connection(path) as conn:
        events.log_event(conn, "login", user=user)
    store.verify_login("gone", "wrong")                                  # lockout state to clean up
    store.delete_user("gone")
    assert store.get_by_username("gone") is None
    with _db.connection(path) as conn:
        assert conn.execute("SELECT username FROM usage_events").fetchone()[0] == "gone"
        assert conn.execute("SELECT COUNT(*) FROM login_attempts WHERE username='gone'").fetchone()[0] == 0
    import pytest
    with pytest.raises(StoreError):
        store.delete_user("root")
