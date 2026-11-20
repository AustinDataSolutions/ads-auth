"""Account changes made by an administrator, and the admin log that records them."""
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from ads_auth import admin, db, maintenance
from ads_auth.store import LoginOutcome, StoreError, UserStore
from ads_auth.ui.gate import LOCAL_DEV_USER

PW = "a-perfectly-adequate-password"
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "app.db"
    db.migrate(p)
    return p


@pytest.fixture
def boss(path):
    user, _ = UserStore(path).create_user("boss", password=PW, full_name="Boss", role="admin")
    return user


def _rows(path):
    with db.connection(path) as conn:
        return [tuple(r) for r in conn.execute("SELECT admin, action, account, detail FROM admin_actions ORDER BY id")]


def _all_text(path) -> str:
    """Every value in the admin log, to show no password ever lands there."""
    with db.connection(path) as conn:
        return repr([tuple(r) for r in conn.execute("SELECT * FROM admin_actions")])


def test_migration_2_adds_the_admin_log_to_an_existing_database(tmp_path):
    p = tmp_path / "old.db"
    with sqlite3.connect(p) as conn:
        conn.executescript(db.MIGRATIONS[1])
        conn.execute("INSERT INTO schema_version(version, applied_utc) VALUES (1, '2026-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO usage_events(ts_utc, username, event) VALUES ('2026-01-01T00:00:00+00:00', 'u', 'login')")
    assert db.migrate(p) == 2
    with db.connection(p) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM admin_actions").fetchone()[0] == 0


def test_create_gives_a_temporary_password_and_records_the_role(path, boss):
    user, temp = admin.create_account(path, boss, " New.Person ", "New Person", "manager")
    assert user.username == "new.person" and user.role == "manager" and user.must_change_password
    _, outcome = UserStore(path).verify_login("new.person", temp)
    assert outcome is LoginOutcome.SUCCESS
    assert _rows(path) == [("boss", "created", "new.person", "as manager")]
    assert temp not in _all_text(path)


def test_create_needs_a_name_and_a_new_username(path, boss):
    with pytest.raises(StoreError, match="Name is required"):
        admin.create_account(path, boss, "x", "  ", "user")
    with pytest.raises(StoreError, match="already exists"):
        admin.create_account(path, boss, "boss", "Another Boss", "user")
    with pytest.raises(ValueError):
        admin.create_account(path, boss, "has space", "Someone", "user")
    assert _rows(path) == []


def test_reset_signs_out_clears_lockout_and_records(path, boss):
    store = UserStore(path)
    rita, _ = store.create_user("rita", password=PW, full_name="Rita")
    for _ in range(5):
        store.verify_login("rita", "wrong-password-x")
    assert store.is_locked("rita")
    temp = admin.reset_password(path, boss, "rita")
    after = store.get_by_username("rita")
    assert not store.is_locked("rita") and after.must_change_password
    assert after.session_version == rita.session_version + 1
    assert _rows(path) == [("boss", "password_reset", "rita", None)]
    assert temp not in _all_text(path)


def test_role_change_records_old_and_new_and_skips_no_change(path, boss):
    UserStore(path).create_user("sam", password=PW)
    admin.change_role(path, boss, "sam", "user")
    assert _rows(path) == []
    assert admin.change_role(path, boss, "sam", "manager").role == "manager"
    assert _rows(path) == [("boss", "role_changed", "sam", "user → manager")]


def test_deactivate_then_reactivate_with_a_fresh_temporary_password(path, boss):
    store = UserStore(path)
    store.create_user("lee", password=PW)
    admin.deactivate(path, boss, "lee")
    assert store.verify_login("lee", PW)[1] is LoginOutcome.INACTIVE
    with pytest.raises(StoreError, match="deactivated"):
        admin.reset_password(path, boss, "lee")
    temp = admin.reactivate(path, boss, "lee")
    assert store.verify_login("lee", PW)[1] is LoginOutcome.WRONG_PASSWORD    # the old password stays dead
    assert store.verify_login("lee", temp)[1] is LoginOutcome.SUCCESS
    assert store.get_by_username("lee").must_change_password
    assert [r[1] for r in _rows(path)] == ["deactivated", "reactivated"]


def test_delete_removes_the_account_and_keeps_the_log_row(path, boss):
    UserStore(path).create_user("gone", password=PW)
    admin.delete_account(path, boss, "gone")
    assert UserStore(path).get_by_username("gone") is None
    assert _rows(path) == [("boss", "deleted", "gone", None)]


@pytest.mark.parametrize("op", [
    lambda p, who: admin.reset_password(p, who, who.username),
    lambda p, who: admin.change_role(p, who, who.username, "user"),
    lambda p, who: admin.deactivate(p, who, who.username),
    lambda p, who: admin.delete_account(p, who, who.username),
])
def test_an_admin_cannot_act_on_their_own_account(path, boss, op):
    with pytest.raises(StoreError, match="your own account"):
        op(path, boss)
    assert _rows(path) == []


def test_only_a_signed_in_admin_can_act(path, boss):
    store = UserStore(path)
    manager, _ = store.create_user("mgr", password=PW, role="manager")
    store.create_user("victim", password=PW)
    with pytest.raises(StoreError, match="Only an administrator"):
        admin.reset_password(path, manager, "victim")
    with pytest.raises(StoreError, match="sign-in is disabled"):
        admin.reset_password(path, LOCAL_DEV_USER, "victim")
    with pytest.raises(StoreError, match="User not found"):
        admin.reset_password(path, boss, "nobody")
    assert _rows(path) == []


def test_server_log_names_the_admin_and_action_but_not_the_account(path, boss, caplog):
    UserStore(path).create_user("private.person", password=PW)
    with caplog.at_level(logging.INFO, logger="ads_auth.admin"):
        temp = admin.reset_password(path, boss, "private.person")
    assert [r.getMessage() for r in caplog.records] == ["admin action: password_reset by boss"]
    assert "private.person" not in caplog.text and temp not in caplog.text


def test_recent_is_newest_first_and_prune_keeps_thirteen_months(path, boss):
    with db.connection(path) as conn:
        admin.record(conn, boss, "deleted", "old", now=datetime(2025, 1, 1, tzinfo=timezone.utc))
        admin.record(conn, boss, "created", "new", detail="as user", now=T0)
        assert [r.account for r in admin.recent(conn)] == ["new", "old"]
        with pytest.raises(ValueError):
            admin.record(conn, boss, "made_up", "x")
    done = maintenance.run_once(path.parent, path, now=T0)
    assert done["admin_pruned"] == 1
    assert _rows(path) == [("boss", "created", "new", "as user")]
