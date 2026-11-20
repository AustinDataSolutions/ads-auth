"""Self-service password reset screens through AppTest against the demo app, with mail captured."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ads_auth import db, mail, resets
from ads_auth.store import LoginOutcome, UserStore
from ads_auth.ui import reset as reset_ui

DEMO = Path(__file__).resolve().parent / "demo_app.py"
PW = "a-perfectly-adequate-password"
NEW = "another-adequate-password"
MAIL_ENV = {"SMTP_HOST": "smtp.example", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
            "EMAIL_FROM": "no-reply@example.org", "APP_URL": "https://demo.example"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("AUTH_MODE", "required")
    data = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data))
    for name, value in MAIL_ENV.items():
        monkeypatch.setenv(name, value)
    db_path = data / "app.db"
    db.migrate(db_path)
    UserStore(db_path).create_user("maria@aarp.org", password=PW, full_name="Maria Lopez")
    UserStore(db_path).create_user("jt", password=PW)
    return db_path


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send_later", lambda m, to, subject, body: sent.append((to, subject, body)))
    return sent


def _ok(at):
    at.run()
    assert not at.exception, at.exception
    return at


def _app(token=None):
    at = AppTest.from_file(str(DEMO), default_timeout=30)
    if token:
        at.query_params[resets.QUERY_PARAM] = token
    return _ok(at)


def _button(at, label):
    return next(b for b in at.button if b.label == label)


def _events(db_path):
    with db.connection(db_path) as conn:
        return [tuple(r) for r in conn.execute("SELECT username, event FROM usage_events ORDER BY id")]


def _request(at, who):
    _button(at, "Forgot your password?").click()
    _ok(at)
    at.text_input(key="auth_reset_who").input(who)
    _button(at, "Send reset link").click()
    return _ok(at)


def _token(body):
    return body.split("?reset=")[1].split()[0]


def test_the_link_appears_only_when_email_and_the_app_address_are_set(env, outbox, monkeypatch):
    assert any(b.label == "Forgot your password?" for b in _app().button)
    monkeypatch.delenv("SMTP_HOST")
    assert not any(b.label == "Forgot your password?" for b in _app().button)


def test_request_sends_the_link_and_shows_the_same_message_either_way(env, outbox):
    at = _request(_app(), "Maria@AARP.org")
    assert any("Check your email" in s.value for s in at.success)
    ((to, subject, body),) = outbox
    assert to == "maria@aarp.org" and subject == "Reset your Demo App password"
    assert "https://demo.example/?reset=" in body
    assert _events(env) == [("maria@aarp.org", "reset_link_sent")]
    for who in ("nobody", "jt"):                       # unknown, and an account with no email
        at = _request(_app(), who)
        assert any("Check your email" in s.value for s in at.success)
    assert len(outbox) == 1 and len(_events(env)) == 1
    _button(at, "Back to sign in").click()
    _ok(at)
    assert {t.key for t in at.text_input} == {"login_username", "login_password"}


def test_the_link_sets_a_new_password_once(env, outbox):
    _request(_app(), "maria@aarp.org")
    token = _token(outbox[0][2])
    at = _app(token)
    assert resets.QUERY_PARAM not in at.query_params                  # taken out of the address bar
    assert at.session_state[reset_ui.TOKEN] == token
    assert any("maria@aarp.org" in m.value for m in at.markdown)
    at.text_input(key="auth_reset_new").input(NEW)
    at.text_input(key="auth_reset_confirm").input("something-else-entirely")
    _button(at, "Set new password").click()
    _ok(at)
    assert any("don't match" in e.value for e in at.error)
    at.text_input(key="auth_reset_new").input(NEW)
    at.text_input(key="auth_reset_confirm").input(NEW)
    _button(at, "Set new password").click()
    _ok(at)
    assert any("Password changed" in s.value for s in at.success)
    assert UserStore(env).verify_login("maria@aarp.org", NEW)[1] is LoginOutcome.SUCCESS
    assert _events(env)[-1] == ("maria@aarp.org", "password_reset")
    _button(at, "Sign in").click()
    _ok(at)
    assert {t.key for t in at.text_input} == {"login_username", "login_password"}
    again = _app(token)                                                # the same link a second time
    assert any("expired or was already used" in w.value for w in again.warning)
    _button(again, "Send a new link").click()
    _ok(again)
    assert any(t.key == "auth_reset_who" for t in again.text_input)
