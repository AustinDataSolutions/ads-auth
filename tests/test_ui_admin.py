"""The sidebar's Manage users section through AppTest against the demo app, and the admin log table."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ads_auth import admin, db
from ads_auth.store import LoginOutcome, UserStore
from ads_auth.ui import admin as admin_ui
from ads_auth.ui import reports

DEMO = Path(__file__).resolve().parent / "demo_app.py"
PW = "a-perfectly-adequate-password"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("AUTH_MODE", "required")
    data = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data))
    db_path = data / "app.db"
    db.migrate(db_path)
    store = UserStore(db_path)
    store.create_user("boss", password=PW, full_name="Boss", role="admin")
    store.create_user("rita", password=PW, full_name="Rita Okafor")
    store.create_user("mgr", password=PW, full_name="Morgan", role="manager")
    return db_path


def _signed_in_as(username):
    at = AppTest.from_file(str(DEMO), default_timeout=30)
    at.run()
    at.text_input(key="login_username").input(username)
    at.text_input(key="login_password").input(PW)
    at.button[0].click().run()
    assert not at.exception, at.exception
    return at


def _ok(at):
    at.run()
    assert not at.exception, at.exception
    return at


def _expanders(at):
    return [e.label for e in at.sidebar.expander]


def _rows(db_path):
    with db.connection(db_path) as conn:
        return [tuple(r) for r in conn.execute("SELECT admin, action, account, detail FROM admin_actions ORDER BY id")]


def test_only_admins_see_the_section_and_it_starts_collapsed(env):
    assert "Manage users" not in _expanders(_signed_in_as("mgr"))
    assert "Manage users" not in _expanders(_signed_in_as("rita"))
    at = _signed_in_as("boss")
    section = next(e for e in at.sidebar.expander if e.label == "Manage users")
    assert section.proto.expanded is False


def test_own_account_is_not_in_the_picker(env):
    at = _signed_in_as("boss")
    options = at.selectbox(key="auth_admin_user_reset").options
    assert any("rita" in o for o in options) and not any("boss" in o for o in options)


def test_reset_shows_the_temporary_password_once_and_records_it(env):
    at = _signed_in_as("boss")
    at.selectbox(key="auth_admin_user_reset").set_value("rita")
    _ok(at)
    assert any("never signed in" in c.value for c in at.sidebar.caption)
    at.button(key="auth_admin_reset").click()
    _ok(at)
    temp = at.sidebar.code[0].value
    assert UserStore(env).verify_login("rita", temp)[1] is LoginOutcome.SUCCESS
    assert _rows(env) == [("boss", "password_reset", "rita", None)]
    at.button(key="auth_admin_done").click()
    _ok(at)
    assert not at.sidebar.code and admin_ui.ISSUED not in at.session_state


def test_add_user_creates_the_account_with_a_temporary_password(env):
    at = _signed_in_as("boss")
    at.radio(key=admin_ui.ACTION).set_value(admin_ui.ADD)
    _ok(at)
    at.text_input(key="auth_admin_add_name").input("New Person")
    at.text_input(key="auth_admin_add_username").input("New.Person")
    at.selectbox(key="auth_admin_add_role").set_value("manager")
    next(b for b in at.button if b.label == "Create account").click()
    _ok(at)
    user = UserStore(env).get_by_username("new.person")
    assert user.full_name == "New Person" and user.role == "manager" and user.must_change_password
    assert user.email is None                                    # "new.person" is not an address
    assert UserStore(env).verify_login("new.person", at.sidebar.code[0].value)[1] is LoginOutcome.SUCCESS
    assert _rows(env) == [("boss", "created", "new.person", "as manager")]


def test_add_user_reports_a_duplicate_without_recording(env):
    at = _signed_in_as("boss")
    at.radio(key=admin_ui.ACTION).set_value(admin_ui.ADD)
    _ok(at)
    at.text_input(key="auth_admin_add_name").input("Rita Again")
    at.text_input(key="auth_admin_add_username").input("rita")
    next(b for b in at.button if b.label == "Create account").click()
    _ok(at)
    assert any("already exists" in e.value for e in at.sidebar.error)
    assert _rows(env) == []


def _save(at):
    next(b for b in at.button if b.label == "Save changes").click()
    return _ok(at)


def test_edit_account_changes_role_name_and_email_and_logs_each(env):
    at = _signed_in_as("boss")
    at.radio(key=admin_ui.ACTION).set_value(admin_ui.EDIT)
    _ok(at)
    picker = at.selectbox(key="auth_admin_user_edit")
    assert "Rita Okafor (rita)" in picker.options                     # name, then the username in parentheses
    picker.set_value("rita")
    _ok(at)
    assert any("no email: can't use Forgot your password" in c.value for c in at.sidebar.caption)
    at.text_input(key="auth_admin_edit_name_rita").input("Rita O.")
    at.text_input(key="auth_admin_edit_email_rita").input("Rita@Example.org")
    at.radio(key="auth_admin_role_rita").set_value("manager")
    _save(at)
    rita = UserStore(env).get_by_username("rita")
    assert (rita.full_name, rita.email, rita.role) == ("Rita O.", "rita@example.org", "manager")
    assert _rows(env) == [("boss", "name_changed", "rita", None), ("boss", "email_changed", "rita", "set"),
                          ("boss", "role_changed", "rita", "user → manager")]


def test_edit_account_with_a_bad_email_changes_nothing(env):
    at = _signed_in_as("boss")
    at.radio(key=admin_ui.ACTION).set_value(admin_ui.EDIT)
    _ok(at)
    at.selectbox(key="auth_admin_user_edit").set_value("rita")
    _ok(at)
    at.text_input(key="auth_admin_edit_name_rita").input("Changed")
    at.text_input(key="auth_admin_edit_email_rita").input("not an address")
    _save(at)
    assert any("Email must be an address" in e.value for e in at.sidebar.error)
    assert UserStore(env).get_by_username("rita").full_name == "Rita Okafor" and _rows(env) == []


def test_a_deactivated_account_offers_reactivate(env):
    boss = UserStore(env).get_by_username("boss")
    admin.deactivate(env, boss, "rita")
    at = _signed_in_as("boss")
    picker = at.selectbox(key="auth_admin_user_reset")
    assert any(o.endswith("(deactivated)") for o in picker.options)
    picker.set_value("rita")
    _ok(at)
    assert not [b for b in at.button if b.key == "auth_admin_reset"]
    at.button(key="auth_admin_reactivate").click()
    _ok(at)
    temp = at.sidebar.code[0].value
    assert UserStore(env).verify_login("rita", temp)[1] is LoginOutcome.SUCCESS
    assert [r[1] for r in _rows(env)] == ["deactivated", "reactivated"]


def test_admin_log_table_is_for_admins_only(env):
    from tests.test_ui_pages import FakeSt, _user          # the small fake Streamlit used for the pages
    boss = UserStore(env).get_by_username("boss")
    admin.reset_password(env, boss, "rita")

    class Settings:
        db_path = env
    st = FakeSt()
    reports.render_admin_log(st, Settings)
    table = st.of("dataframe")[0][1][0]
    assert list(table.columns) == ["When (Eastern)", "Admin", "Action", "Account", "Detail"]
    assert table.iloc[0]["Action"] == "Password reset" and table.iloc[0]["Account"] == "rita"
    assert _user("admin").has_role("admin") and not _user("manager").has_role("admin")


def test_add_user_fills_the_email_from_an_address_username(env):
    at = _signed_in_as("boss")
    at.radio(key=admin_ui.ACTION).set_value(admin_ui.ADD)
    _ok(at)
    at.text_input(key="auth_admin_add_name").input("Maria Lopez")
    at.text_input(key="auth_admin_add_username").input("Maria.Lopez@AARP.org")
    next(b for b in at.button if b.label == "Create account").click()
    _ok(at)
    assert UserStore(env).get_by_username("maria.lopez@aarp.org").email == "maria.lopez@aarp.org"
