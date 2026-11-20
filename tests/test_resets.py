"""Self-service password reset: emails on accounts, reset links, the mail settings, and the CLI."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ads_auth import db, mail, maintenance, resets
from ads_auth.models import normalize_email
from ads_auth.store import LoginOutcome, StoreError, UserStore

PW = "a-perfectly-adequate-password"
NEW = "another-adequate-password"
T0 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "app.db"
    db.migrate(path)
    return UserStore(path)


def _rows(store):
    with db.connection(store.db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM password_resets ORDER BY id")]


# ---- emails on accounts ----------------------------------------------------------------------------

def test_migration_3_fills_email_only_from_address_usernames(tmp_path):
    p = tmp_path / "old.db"
    with sqlite3.connect(p) as conn:
        conn.executescript(db.MIGRATIONS[1])
        conn.executescript(db.MIGRATIONS[2])
        conn.execute("INSERT INTO schema_version(version, applied_utc) VALUES (2, '2026-01-01T00:00:00+00:00')")
        for name in ("maria.lopez@aarp.org", "jt", "ads-dev", "odd@name", "two@at@x.org"):
            conn.execute("INSERT INTO users(username, password_hash, created_utc) VALUES (?, 'h', '2026-01-01T00:00:00+00:00')", (name,))
    assert db.migrate(p) == db.SCHEMA_VERSION
    with db.connection(p) as conn:
        emails = dict(conn.execute("SELECT username, email FROM users").fetchall())
    assert emails == {"maria.lopez@aarp.org": "maria.lopez@aarp.org", "jt": None, "ads-dev": None,
                      "odd@name": None, "two@at@x.org": None}


def test_create_set_and_validate_email(store):
    assert store.create_user("Maria@AARP.org", password=PW)[0].email == "maria@aarp.org"
    assert store.create_user("jt", password=PW)[0].email is None
    assert store.create_user("dev", password=PW, email=" JT@Example.com ")[0].email == "jt@example.com"
    assert store.set_email("jt", "jt@example.com").email == "jt@example.com"
    assert store.set_email("jt", "  ").email is None
    with pytest.raises(ValueError, match="Email must be an address"):
        store.set_email("jt", "not an address")
    assert normalize_email(None) is None


def test_a_renamed_address_username_takes_its_email_along(store):
    store.create_user("maria@old.org", password=PW)
    store.create_user("jt", password=PW, email="jt@example.com")
    assert store.rename_user("maria@old.org", "maria@new.org").email == "maria@new.org"
    assert store.rename_user("maria@new.org", "maria").email is None
    assert store.rename_user("jt", "jt2").email == "jt@example.com"      # a separate email stays


# ---- reset links -------------------------------------------------------------------------------------

def test_request_finds_by_username_or_unique_email_and_only_active_accounts_with_email(store):
    store.create_user("maria@aarp.org", password=PW)
    store.create_user("jt", password=PW, email="shared@example.com")
    store.create_user("ads-dev", password=PW, email="shared@example.com")
    store.create_user("noemail", password=PW)
    store.create_user("gone@aarp.org", password=PW)
    store.set_active("gone@aarp.org", False)
    path = store.db_path
    assert resets.request(path, " MARIA@aarp.org ", now=T0)[0].username == "maria@aarp.org"
    assert resets.request(path, "jt", now=T0)[0].username == "jt"                # by username
    assert resets.request(path, "shared@example.com", now=T0) is None            # two accounts share it
    assert resets.request(path, "noemail", now=T0) is None
    assert resets.request(path, "gone@aarp.org", now=T0) is None
    assert resets.request(path, "nobody", now=T0) is None and resets.request(path, "", now=T0) is None


def test_link_works_once_and_sets_the_password_and_signs_out_and_unlocks(store):
    user, _ = store.create_user("maria@aarp.org", password=PW)
    for _ in range(5):
        store.verify_login("maria@aarp.org", "wrong-password-x")
    assert store.is_locked("maria@aarp.org")
    _, token = resets.request(store.db_path, "maria@aarp.org", now=T0)
    assert token not in repr(_rows(store))                                      # only the hash is kept
    assert resets.check(store.db_path, token, now=T0 + timedelta(minutes=29)).id == user.id
    with pytest.raises(ValueError):
        resets.complete(store.db_path, token, "short", now=T0)                  # refused password keeps the link
    after = resets.complete(store.db_path, token, NEW, now=T0 + timedelta(minutes=5))
    assert after.session_version == user.session_version + 1 and not after.must_change_password
    assert not store.is_locked("maria@aarp.org")
    assert store.verify_login("maria@aarp.org", NEW)[1] is LoginOutcome.SUCCESS
    with pytest.raises(StoreError, match="expired or was already used"):
        resets.complete(store.db_path, token, NEW, now=T0 + timedelta(minutes=6))


def test_link_expires_and_is_ended_by_a_newer_link_or_any_account_change(store):
    store.create_user("maria@aarp.org", password=PW)
    path = store.db_path
    _, token = resets.request(path, "maria@aarp.org", now=T0)
    assert resets.check(path, token, now=T0 + resets.LINK_LIFETIME) is None    # 30 minutes exactly: gone
    _, first = resets.request(path, "maria@aarp.org", now=T0)
    _, second = resets.request(path, "maria@aarp.org", now=T0)
    assert resets.check(path, first, now=T0) is None and resets.check(path, second, now=T0) is not None
    store.set_temporary_password("maria@aarp.org")                               # an admin reset meanwhile
    assert resets.check(path, second, now=T0) is None
    store.create_user("lee@aarp.org", password=PW)
    _, token = resets.request(path, "lee@aarp.org", now=T0 + timedelta(hours=2))
    store.set_email("lee@aarp.org", "lee@elsewhere.org")                        # the address changed
    assert resets.check(path, token, now=T0 + timedelta(hours=2)) is None
    assert resets.check(path, "made-up", now=T0) is None and resets.check(path, "", now=T0) is None


def test_at_most_three_links_an_hour_per_account(store):
    store.create_user("maria@aarp.org", password=PW)
    path = store.db_path
    sent = [resets.request(path, "maria@aarp.org", now=T0 + timedelta(minutes=m)) for m in range(5)]
    assert [s is not None for s in sent] == [True, True, True, False, False]
    assert resets.request(path, "maria@aarp.org", now=T0 + timedelta(minutes=61)) is not None


def test_rows_are_pruned_a_day_after_expiry(store):
    store.create_user("maria@aarp.org", password=PW)
    resets.request(store.db_path, "maria@aarp.org", now=T0)
    done = maintenance.run_once(store.db_path.parent, store.db_path, now=T0 + timedelta(hours=24))
    assert done["resets_pruned"] == 0
    done = maintenance.run_once(store.db_path.parent, store.db_path, now=T0 + timedelta(hours=25))
    assert done["resets_pruned"] == 1 and _rows(store) == []


def test_email_text_names_the_app_the_account_and_the_link(store):
    user, _ = store.create_user("maria@aarp.org", password=PW, full_name="Maria Lopez")
    url = resets.link("https://example.onrender.com/", "TOKEN")
    assert url == "https://example.onrender.com/?reset=TOKEN"
    subject, body = resets.email_text("Model Rules Finder", user, url)
    assert subject == "Reset your Model Rules Finder password"
    assert body.startswith("Hello Maria,") and "(maria@aarp.org)" in body and url in body
    assert "30 minutes" in body and "ignore this email" in body


# ---- mail settings ------------------------------------------------------------------------------------

def test_mail_settings_from_the_environment():
    assert mail.mail_from_environ({}) is None and mail.mail_from_environ({"SMTP_HOST": " "}) is None
    full = {"SMTP_HOST": "smtp-relay.brevo.com", "SMTP_USER": "u", "SMTP_PASSWORD": "secret",
            "EMAIL_FROM": "no-reply@austindatasolutions.com"}
    m = mail.mail_from_environ(full)
    assert (m.host, m.port, m.user) == ("smtp-relay.brevo.com", 587, "u") and "secret" not in repr(m)
    assert m.from_header == "Austin Data Solutions <no-reply@austindatasolutions.com>"
    assert mail.mail_from_environ({**full, "EMAIL_FROM": "ADS <x@y.org>"}).from_header == "ADS <x@y.org>"
    with pytest.raises(ValueError, match="SMTP_PASSWORD"):
        mail.mail_from_environ({**full, "SMTP_PASSWORD": ""})
    with pytest.raises(ValueError, match="SMTP_PORT"):
        mail.mail_from_environ({**full, "SMTP_PORT": "x"})


def test_send_uses_starttls_and_login_and_a_failure_in_the_background_is_only_logged(monkeypatch, caplog):
    seen = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            seen.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context):
            seen.append(("starttls",))

        def login(self, user, password):
            seen.append(("login", user))

        def send_message(self, message):
            seen.append(("send", message["To"], message["From"], message["Subject"]))

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    m = mail.MailSettings("h", 587, "u", "p", "no-reply@x.org")
    mail.send_later(m, "maria@aarp.org", "Subject", "Body").join(5)
    assert seen == [("connect", "h", 587), ("starttls",), ("login", "u"),
                    ("send", "maria@aarp.org", "Austin Data Solutions <no-reply@x.org>", "Subject")]

    def broken(*args, **kwargs):
        raise OSError("no route")
    monkeypatch.setattr(mail.smtplib, "SMTP", broken)
    mail.send_later(m, "maria@aarp.org", "Subject", "Body").join(5)
    assert "An email could not be sent (OSError)." in caplog.text and "maria@aarp.org" not in caplog.text


# ---- command line ---------------------------------------------------------------------------------------

def test_cli_set_email_create_with_email_and_list_users(tmp_path):
    from tests.test_cli import Out, load_settings
    from ads_auth import cli
    env = {"DATA_DIR": str(tmp_path / "data"), "AUTH_MODE": "required"}

    def run(argv):
        out = Out()
        return cli.main(argv, load_settings=load_settings, environ=env, out=out, prompt=lambda _: ""), out
    assert run(["create-user", "jt", "--temporary", "--email", "JT@Example.com"])[0] == 0
    code, out = run(["set-email", "jt", "jt@ads.example"])
    assert code == 0 and "email set to jt@ads.example" in out.text
    code, out = run(["list-users"])
    assert out.lines[0].endswith("email") and out.lines[1].endswith("jt@ads.example")
    code, out = run(["set-email", "jt"])
    assert code == 0 and "email removed" in out.text
    code, out = run(["set-email", "jt", "nope"])
    assert code == 1 and "Email must be an address" in out.text
