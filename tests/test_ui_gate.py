"""Login gate behavior through AppTest against the demo app (AUTH_MODE=required)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ads_auth import db
from ads_auth.store import UserStore
from ads_auth.ui.gate import INVALID_LOGIN_MESSAGE, LEAVE_QUESTION, PENDING_PAGE, SETUP_MESSAGE

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
    return db_path


def _signed_in(at) -> bool:
    """The search box is only built after the gate passes."""
    return any(ti.label.startswith("🔍") for ti in at.text_input)


def _app():
    at = AppTest.from_file(str(DEMO), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def _login(at, username, password):
    at.text_input(key="login_username").input(username)
    at.text_input(key="login_password").input(password)
    at.button[0].click().run()
    assert not at.exception, at.exception
    return at


def _events(db_path):
    with db.connection(db_path) as conn:
        return [tuple(r) for r in conn.execute("SELECT username, event FROM usage_events ORDER BY id")]


def test_no_accounts_shows_setup_message(env):
    at = _app()
    assert at.title[0].value == "Demo App"
    assert any(SETUP_MESSAGE in i.value for i in at.info)
    assert not at.text_input and not _signed_in(at)


def test_unauthenticated_shows_only_login_form(env):
    UserStore(env).create_user("u", password=PW)
    at = _app()
    assert {t.key for t in at.text_input} == {"login_username", "login_password"}
    assert at.button[0].label == "Sign in"
    assert not _signed_in(at)


def test_wrong_password_single_message_and_attributed_event(env):
    UserStore(env).create_user("u", password=PW)
    at = _login(_app(), "U", "nope")
    assert [e.value for e in at.error] == [INVALID_LOGIN_MESSAGE]
    assert not _signed_in(at)
    assert _events(env) == [("u", "login_failed")]


def test_unknown_user_same_message_and_anonymous_event(env):
    UserStore(env).create_user("u", password=PW)
    at = _login(_app(), "ghost", "whatever")
    assert [e.value for e in at.error] == [INVALID_LOGIN_MESSAGE]
    assert _events(env) == [(None, "login_failed")]


def test_successful_login_then_logout(env):
    UserStore(env).create_user("Pat@Example.org", password=PW, full_name="Pat", role="manager")
    at = _login(_app(), "pat@example.org", PW)
    assert _signed_in(at)
    assert any("Signed in as **Pat**" in m.value for m in at.sidebar.markdown)
    assert _events(env) == [("pat@example.org", "login")]
    at.button(key="logout_button").click().run()
    assert not at.exception
    assert {t.key for t in at.main.text_input} == {"login_username", "login_password"}
    assert not _signed_in(at)
    assert _events(env) == [("pat@example.org", "login"), ("pat@example.org", "logout")]


def test_lockout_blocks_correct_password(env):
    UserStore(env).create_user("u", password=PW)
    at = _app()
    for _ in range(5):
        _login(at, "u", "wrong")
    _login(at, "u", PW)
    assert [e.value for e in at.error] == [INVALID_LOGIN_MESSAGE] and not _signed_in(at)
    assert all(e == ("u", "login_failed") for e in _events(env)) and len(_events(env)) == 6


def test_temporary_password_forces_change_then_app_loads(env):
    _, temp = UserStore(env).create_user("t", temporary=True)
    at = _login(_app(), "t", temp)
    assert not _signed_in(at)
    assert at.title[0].value == "Demo App"
    assert any("temporary password" in w.value for w in at.warning)
    inputs = at.text_input
    inputs[0].input(temp); inputs[1].input("brand-new-password-ok"); inputs[2].input("brand-new-password-ok")
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert _signed_in(at)
    assert not UserStore(env).get_by_username("t").must_change_password


def test_self_service_password_change_keeps_session(env):
    user, _ = UserStore(env).create_user("u", password=PW)
    at = _login(_app(), "u", PW)
    form_inputs = [t for t in at.sidebar.text_input]
    form_inputs[0].input(PW); form_inputs[1].input("another-long-password-1"); form_inputs[2].input("another-long-password-1")
    at.button(key="FormSubmitter:change_password_form-Change password").click().run()
    assert not at.exception, at.exception
    assert any("Password changed" in s.value for s in at.sidebar.success)
    assert _signed_in(at)
    assert UserStore(env).get_by_username("u").session_version == user.session_version + 1


def test_revoked_session_is_logged_out_on_next_run(env):
    UserStore(env).create_user("u", password=PW)
    at = _login(_app(), "u", PW)
    assert _signed_in(at)
    UserStore(env).set_temporary_password("u")                # admin reset bumps session_version
    at.run()
    assert not _signed_in(at)
    assert any("session ended" in i.value for i in at.info)


def test_disabled_auth_refused_on_render(env, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("AUTH_MODE", "disabled")
    at = _app()
    assert any("Refusing to serve" in e.value for e in at.error)
    assert not _signed_in(at) and not at.text_input


def test_disabled_auth_locally_skips_the_gate_and_records_nothing(env, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    at = _app()
    assert any("authentication is disabled" in w.value for w in at.warning)
    assert _signed_in(at)
    at.text_input(key="search").input("anything").run()
    assert _events(env) == []


# --- leaving the page with work in progress ---------------------------------------------------------

def _sidebar_nav(at):
    """(page links, navigation buttons) the sidebar currently shows."""
    links = [p.label for p in at.sidebar.get("page_link")]
    buttons = [b.key for b in at.sidebar.button if b.key.startswith("auth_nav_")]
    return links, buttons


def test_links_navigate_at_once_while_there_is_nothing_to_lose(env):
    UserStore(env).create_user("pat", password=PW, role="manager")
    at = _login(_app(), "pat", PW)
    assert _sidebar_nav(at) == (["Usage statistics", "About this app and data handling"], [])
    assert PENDING_PAGE not in at.session_state
    assert not [b for b in at.button if b.key in ("auth_leave_back", "auth_leave_proceed")]


def test_a_typed_search_turns_the_links_into_a_confirmation(env):
    UserStore(env).create_user("pat", password=PW, role="manager")
    at = _login(_app(), "pat", PW)
    at.text_input(key="search").input("quarterly").run()
    assert not at.exception, at.exception
    assert _sidebar_nav(at) == ([], ["auth_nav_reports", "auth_nav_about"])
    assert [b.label for b in at.sidebar.button if b.key.startswith("auth_nav_")] == \
        ["Usage statistics", "About this app and data handling"]

    # Clicking a link now asks instead of leaving.
    at.button(key="auth_nav_about").click().run()
    assert not at.exception, at.exception
    assert at.session_state[PENDING_PAGE] == "about"
    assert "Leaving this page clears the search box." in [m.value for m in at.markdown]
    assert f"**{LEAVE_QUESTION}**" in [m.value for m in at.markdown]
    assert [b.label for b in at.button if b.key.startswith("auth_leave_")] == ["Go back", "Proceed"]
    assert [t.value for t in at.title] == ["Demo App"]

    # Go back: the dialog closes and the screen stays. (In a browser the typed search is kept too;
    # AppTest on Streamlit 1.52 resets keyed widgets after the full rerun Go back asks for from
    # inside the dialog, so that is not asserted here.)
    at.button(key="auth_leave_back").click().run()
    assert not at.exception, at.exception
    assert PENDING_PAGE not in at.session_state
    at.run()
    assert not [b for b in at.button if b.key.startswith("auth_leave_")]
    assert [t.value for t in at.title] == ["Demo App"]

    # Proceed: the chosen page opens.
    at.text_input(key="search").input("quarterly").run()         # see the note above
    at.button(key="auth_nav_reports").click().run()
    assert at.session_state[PENDING_PAGE] == "reports"
    at.button(key="auth_leave_proceed").click().run()
    assert not at.exception, at.exception
    assert PENDING_PAGE not in at.session_state
    assert [t.value for t in at.title] == ["Usage statistics"]


def test_the_confirmation_only_offers_pages_the_app_registered(env):
    UserStore(env).create_user("pat", password=PW)                   # not a manager: no Usage statistics
    at = _login(_app(), "pat", PW)
    at.text_input(key="search").input("quarterly").run()
    assert _sidebar_nav(at) == ([], ["auth_nav_about"])
