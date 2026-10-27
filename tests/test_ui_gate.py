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


# --- staying signed in across a reload ------------------------------------------------------------
#
# AppTest has no browser, so the two touch points with it are replaced: the cookies the handshake
# carried (`cookie.request_cookies`) and the set/clear instruction the page would draw
# (`cookie.apply_action`), which is recorded instead.

from datetime import timedelta

from ads_auth import tokens
from ads_auth.db import to_iso, utc_now
from ads_auth.ui import cookie
from ads_auth.ui.gate import COOKIE_BLOCKED, SESSION_AUTHENTICATED_UTC

SECRET = "demo-cookie-secret-of-32-characters!"


@pytest.fixture
def remembered(env, monkeypatch):
    """The demo app with the cookie on; returns the recorded browser instructions and a cookie jar."""
    monkeypatch.setenv("ADS_AUTH_COOKIE_SECRET", SECRET)
    actions = []
    jar = {}
    monkeypatch.setattr(cookie, "apply_action", lambda st, settings, action: actions.append(action) or True)
    monkeypatch.setattr(cookie, "request_cookies", lambda st: dict(jar))
    return actions, jar


def _set_token(actions):
    sets = [a for a in actions if a[0] == "set"]
    assert sets, actions
    return sets[-1][1]


def test_sign_in_asks_the_browser_to_keep_a_signed_token(env, remembered):
    actions, _ = remembered
    user, _ = UserStore(env).create_user("pat", password=PW)
    at = _login(_app(), "pat", PW)
    assert _signed_in(at)
    assert [a[0] for a in actions] == ["set"]
    claims = tokens.verify(SECRET, _set_token(actions))
    assert claims.user_id == user.id and claims.session_version == user.session_version
    assert claims.authenticated_utc == at.session_state[SESSION_AUTHENTICATED_UTC]
    assert _events(env) == [("pat", "login")]


def test_a_reload_with_the_cookie_restores_the_session_without_a_login_event(env, remembered):
    actions, jar = remembered
    UserStore(env).create_user("pat", password=PW, full_name="Pat")
    first = _login(_app(), "pat", PW)
    jar[tokens.COOKIE_NAME] = _set_token(actions)
    actions.clear()
    again = _app()                                              # a new Streamlit session: the reload
    assert _signed_in(again)
    assert any("Signed in as **Pat**" in m.value for m in again.sidebar.markdown)
    assert again.session_state[SESSION_AUTHENTICATED_UTC] == first.session_state[SESSION_AUTHENTICATED_UTC]
    assert actions == []                                        # nothing re-issued, nothing cleared
    assert _events(env) == [("pat", "login")]                   # the restore is not a sign-in
    again.text_input(key="search").input("q").run()
    assert _events(env) == [("pat", "login"), ("pat", "search")]


def test_sign_out_clears_the_cookie_and_blocks_a_restore_on_this_connection(env, remembered):
    actions, jar = remembered
    UserStore(env).create_user("pat", password=PW)
    at = _login(_app(), "pat", PW)
    jar[tokens.COOKIE_NAME] = _set_token(actions)                # as if the browser had stored it
    actions.clear()
    at.button(key="logout_button").click().run()
    assert not at.exception and not _signed_in(at)
    assert actions == [("clear",)]
    assert at.session_state[COOKIE_BLOCKED] is True
    at.run()                                                    # the handshake still carries the old cookie
    assert not _signed_in(at) and actions == [("clear",)]
    assert _events(env) == [("pat", "login"), ("pat", "logout")]
    jar.clear()
    assert not _signed_in(_app())                               # a real reload after sign-out: sign-in screen


@pytest.mark.parametrize("revoke", [
    lambda store: store.set_temporary_password("pat"),          # password reset bumps session_version
    lambda store: store.set_active("pat", False),               # deactivated account
])
def test_a_cookie_from_before_a_reset_or_deactivation_is_rejected_and_cleared(env, remembered, revoke):
    actions, jar = remembered
    UserStore(env).create_user("pat", password=PW)
    _login(_app(), "pat", PW)
    jar[tokens.COOKIE_NAME] = _set_token(actions)
    actions.clear()
    revoke(UserStore(env))
    at = _app()
    assert not _signed_in(at)
    assert {t.key for t in at.text_input} == {"login_username", "login_password"}
    assert any("session ended" in i.value for i in at.info)
    assert actions == [("clear",)] and at.session_state[COOKIE_BLOCKED] is True
    assert _events(env) == [("pat", "login")]


def test_an_expired_cookie_shows_the_sign_in_screen_with_the_notice(env, remembered):
    actions, jar = remembered
    user, _ = UserStore(env).create_user("pat", password=PW)
    jar[tokens.COOKIE_NAME] = tokens.issue(SECRET, user.id, user.session_version, to_iso(utc_now() - timedelta(hours=9)))
    at = _app()
    assert not _signed_in(at) and any("session ended" in i.value for i in at.info)
    assert actions == [("clear",)]


def test_a_tampered_or_foreign_cookie_is_ignored_quietly_and_cleared(env, remembered):
    actions, jar = remembered
    user, _ = UserStore(env).create_user("pat", password=PW)
    jar[tokens.COOKIE_NAME] = tokens.issue("another-apps-secret-32-characters!!", user.id, user.session_version, to_iso(utc_now()))
    at = _app()
    assert not _signed_in(at) and not at.info and not at.error
    assert actions == [("clear",)] and _events(env) == []


def test_a_cookie_for_a_deleted_account_is_rejected(env, remembered):
    actions, jar = remembered
    jar[tokens.COOKIE_NAME] = tokens.issue(SECRET, 999, 1, to_iso(utc_now()))
    UserStore(env).create_user("pat", password=PW)
    at = _app()
    assert not _signed_in(at) and actions == [("clear",)]


def test_signing_in_again_after_a_rejected_cookie_issues_a_fresh_one(env, remembered):
    actions, jar = remembered
    user, _ = UserStore(env).create_user("pat", password=PW)
    jar[tokens.COOKIE_NAME] = "v1.1.1.1.nonsense"
    at = _login(_app(), "pat", PW)
    assert _signed_in(at)
    assert [a[0] for a in actions] == ["clear", "set"]
    assert tokens.verify(SECRET, _set_token(actions)).user_id == user.id


def test_changing_the_password_reissues_the_cookie_with_the_new_version(env, remembered):
    actions, _ = remembered
    user, _ = UserStore(env).create_user("pat", password=PW)
    at = _login(_app(), "pat", PW)
    actions.clear()
    form_inputs = [t for t in at.sidebar.text_input]
    form_inputs[0].input(PW); form_inputs[1].input("another-long-password-1"); form_inputs[2].input("another-long-password-1")
    at.button(key="FormSubmitter:change_password_form-Change password").click().run()
    assert any("Password changed" in s.value for s in at.sidebar.success)
    claims = tokens.verify(SECRET, _set_token(actions))
    assert claims.session_version == user.session_version + 1
    assert claims.authenticated_utc == at.session_state[SESSION_AUTHENTICATED_UTC]    # sign-in time unchanged


def test_the_forced_password_change_reissues_the_cookie_too(env, remembered):
    actions, _ = remembered
    user, temp = UserStore(env).create_user("t", temporary=True)
    at = _login(_app(), "t", temp)
    actions.clear()
    inputs = at.text_input
    inputs[0].input(temp); inputs[1].input("brand-new-password-ok"); inputs[2].input("brand-new-password-ok")
    at.button[0].click().run()
    assert _signed_in(at)
    assert tokens.verify(SECRET, _set_token(actions)).session_version == user.session_version + 1


def test_without_a_secret_nothing_is_set_and_a_cookie_is_ignored(env, monkeypatch):
    monkeypatch.delenv("ADS_AUTH_COOKIE_SECRET", raising=False)
    monkeypatch.delenv("STREAMLIT_SERVER_COOKIE_SECRET", raising=False)
    actions = []
    monkeypatch.setattr(cookie, "apply_action", lambda st, settings, action: actions.append(action) or True)
    user, _ = UserStore(env).create_user("pat", password=PW)
    monkeypatch.setattr(cookie, "request_cookies",
                        lambda st: {tokens.COOKIE_NAME: tokens.issue(SECRET, user.id, user.session_version, to_iso(utc_now()))})
    at = _app()
    assert not _signed_in(at)
    _login(at, "pat", PW)
    assert _signed_in(at) and actions == []
    at.button(key="logout_button").click().run()
    assert actions == [] and COOKIE_BLOCKED not in at.session_state


def test_the_real_browser_touch_points_are_harmless_under_apptest(env, monkeypatch):
    """Without a live server the instruction is simply not drawn, and the handshake has no cookies."""
    monkeypatch.setenv("ADS_AUTH_COOKIE_SECRET", SECRET)
    drawn = []
    real = cookie.apply_action
    monkeypatch.setattr(cookie, "apply_action", lambda st, s, a: drawn.append(real(st, s, a)) or drawn[-1])
    UserStore(env).create_user("pat", password=PW)
    at = _login(_app(), "pat", PW)
    assert _signed_in(at)
    at.button(key="logout_button").click().run()
    assert not at.exception and not _signed_in(at)
    assert drawn == [False, False]                           # set after sign-in, clear after sign-out: neither drawn
    assert _events(env) == [("pat", "login"), ("pat", "logout")]
