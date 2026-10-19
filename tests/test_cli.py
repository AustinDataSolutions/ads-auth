import sqlite3
from pathlib import Path

import pytest

from ads_auth import AppSettings, cli, db
from ads_auth.store import LoginOutcome, UserStore

PW = "a-perfectly-adequate-password"


class Out:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


def load_settings(environ):
    """A minimal app loader: the contract `cli.main` expects."""
    return AppSettings(app_name="demo-app", title="Demo", data_dir=Path(environ["DATA_DIR"]),
                       disclosure_path=Path(environ["DATA_DIR"]) / "d.md", auth_mode=environ.get("AUTH_MODE", "required"))


@pytest.fixture
def env(tmp_path):
    return {"DATA_DIR": str(tmp_path / "data"), "AUTH_MODE": "required"}


def run(argv, env, prompts=(), commands=()):
    out = Out()
    answers = list(prompts)
    code = cli.main(argv, load_settings=load_settings, environ=env, out=out, prompt=lambda _: answers.pop(0), commands=commands)
    return code, out


def test_create_user_with_prompt_and_list(env):
    code, out = run(["create-user", "Alice@Example.org", "--full-name", "Alice", "--role", "admin"], env, [PW, PW])
    assert code == 0 and "Created alice@example.org (admin)" in out.text
    code, out = run(["list-users"], env)
    assert code == 0 and "alice@example.org" in out.text and "scrypt" not in out.text and PW not in out.text


def test_create_user_password_mismatch(env):
    code, out = run(["create-user", "a"], env, [PW, "different-password-123"])
    assert code == 1 and "do not match" in out.text


def test_create_temporary_and_reset_print_once_and_clear_lockout(env):
    code, out = run(["create-user", "t", "--temporary"], env)
    assert code == 0
    temp = out.lines[-1]
    store = UserStore(Path(env["DATA_DIR"]) / "app.db")
    assert store.verify_login("t", temp)[1] is LoginOutcome.SUCCESS
    for _ in range(5):
        store.verify_login("t", "wrong")
    assert store.verify_login("t", temp)[1] is LoginOutcome.LOCKED
    code, out = run(["set-password", "t"], env)
    assert code == 0 and "shown once" in out.text
    assert store.verify_login("t", out.lines[-1])[1] is LoginOutcome.SUCCESS   # reset also unlocked


def test_unlock_role_deactivate_reactivate(env):
    run(["create-user", "admin", "--role", "admin"], env, [PW, PW])
    run(["create-user", "u"], env, [PW, PW])
    store = UserStore(Path(env["DATA_DIR"]) / "app.db")
    for _ in range(5):
        store.verify_login("u", "wrong")
    assert store.verify_login("u", PW)[1] is LoginOutcome.LOCKED
    assert run(["unlock-user", "u"], env)[0] == 0
    assert store.verify_login("u", PW)[1] is LoginOutcome.SUCCESS
    code, out = run(["set-role", "u", "manager"], env)
    assert code == 0 and "now manager" in out.text
    assert run(["deactivate-user", "u"], env)[0] == 0
    assert store.verify_login("u", PW)[1] is LoginOutcome.INACTIVE
    assert run(["reactivate-user", "u"], env)[0] == 0
    code, out = run(["deactivate-user", "admin"], env)
    assert code == 1 and "last active admin" in out.text


def test_set_name(env):
    run(["create-user", "u"], env, [PW, PW])
    code, out = run(["set-name", "u", "Someone Else"], env)
    assert code == 0 and UserStore(Path(env["DATA_DIR"]) / "app.db").get_by_username("u").full_name == "Someone Else"


def test_errors_are_safe_messages(env):
    code, out = run(["set-role", "ghost", "manager"], env)
    assert code == 1 and out.text == "Error: User not found."
    code, out = run(["create-user", "x"], env, ["short", "short"])
    assert code == 1 and "at least 10 characters" in out.text


def test_import_users_dry_run_then_apply(env, tmp_path):
    from werkzeug.security import generate_password_hash
    src = tmp_path / "users.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, username VARCHAR(80), password_hash VARCHAR(256), full_name VARCHAR(200))")
    conn.execute("INSERT INTO user(username, password_hash, full_name) VALUES ('Pat@Example.org', ?, 'Pat')", (generate_password_hash("pat-password-123456"),))
    conn.commit(); conn.close()
    code, out = run(["import-users", str(src), "--allow", "pat@example.org", "--allow", "ghost"], env)
    assert code == 0 and "Will import: pat@example.org" in out.text and "Not found in source: ghost" in out.text and "Dry run" in out.text
    assert UserStore(Path(env["DATA_DIR"]) / "app.db").get_by_username("pat@example.org") is None
    code, out = run(["import-users", str(src), "--allow", "pat@example.org", "--apply"], env)
    assert code == 0 and "Imported: pat@example.org" in out.text
    assert "scrypt" not in out.text
    assert UserStore(Path(env["DATA_DIR"]) / "app.db").verify_login("pat@example.org", "pat-password-123456")[1] is LoginOutcome.SUCCESS


def test_import_users_from_package_database(env, tmp_path):
    """The source is another app that uses this package."""
    src = tmp_path / "other-app.db"
    db.migrate(src)
    UserStore(src).create_user("pat@example.org", password="pat-password-123456", full_name="Pat", role="admin")
    code, out = run(["import-users", str(src), "--allow", "pat@example.org"], env)
    assert code == 0 and "Will import: pat@example.org" in out.text and "Dry run" in out.text
    code, out = run(["import-users", str(src), "--allow", "pat@example.org", "--apply"], env)
    assert code == 0 and "Imported: pat@example.org" in out.text and "scrypt" not in out.text
    store = UserStore(Path(env["DATA_DIR"]) / "app.db")
    assert store.get_by_username("pat@example.org").role == "user"
    assert store.verify_login("pat@example.org", "pat-password-123456")[1] is LoginOutcome.SUCCESS


def test_import_users_source_without_known_table(env, tmp_path):
    src = tmp_path / "other.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE accounts (username TEXT)")
    conn.commit(); conn.close()
    code, out = run(["import-users", str(src), "--allow", "pat"], env)
    assert code == 1 and out.text == "Error: Source database has no readable 'users' or 'user' table."


def test_backup_restore_prune_export(env):
    data = Path(env["DATA_DIR"])
    run(["create-user", "u"], env, [PW, PW])
    code, out = run(["backup-db"], env)
    assert code == 0
    backup = Path(out.lines[-1])
    assert backup.exists() and backup.parent == data / "backups"

    run(["deactivate-user", "u"], env)            # state now differs from the backup
    (data / "serving.lock").write_text("1")
    code, out = run(["restore-db", str(backup)], env)
    assert code == 1 and "Refusing" in out.text
    code, out = run(["restore-db", str(backup), "--force"], env)
    assert code == 0 and "Restored. users=1" in out.text
    assert UserStore(data / "app.db").get_by_username("u").is_active

    with db.connection(data / "app.db") as conn:
        conn.execute("INSERT INTO usage_events(ts_utc, username, event) VALUES ('2020-01-01T00:00:00+00:00', 'u', 'login')")
        conn.execute("INSERT INTO usage_events(ts_utc, username, event) VALUES ('2099-01-01T00:00:00+00:00', 'u', 'login')")
        conn.commit()
    code, out = run(["prune-events"], env)
    assert code == 0 and "Pruned 1 events" in out.text
    code, out = run(["export-events"], env)
    lines = out.text.splitlines()
    assert code == 0 and lines[-2] == "ts_utc,app,username,event" and lines[-1] == "2099-01-01T00:00:00+00:00,demo-app,u,login"
    target = data / "events.csv"
    code, out = run(["export-events", "--out", str(target)], env)
    assert code == 0 and target.read_text().startswith("ts_utc,app,username,event\n")


def test_delete_user_requires_force(env):
    run(["create-user", "root", "--role", "admin"], env, [PW, PW])
    run(["create-user", "temp"], env, [PW, PW])
    code, out = run(["delete-user", "temp"], env)
    assert code == 1 and "Refusing" in out.text
    code, out = run(["delete-user", "temp", "--force"], env)
    assert code == 0 and "temp deleted" in out.text
    code, out = run(["list-users"], env)
    assert "temp" not in out.text and "root" in out.text


def test_app_commands_plug_in_and_share_the_lock_check(env):
    def wipe(c, a):
        if c.refuse_if_serving(a.force):
            return 1
        c.out(f"wiped {c.settings.app_name} in {c.settings.data_dir.name}")
        return 0

    commands = [cli.Command("wipe", wipe, help="remove app data", arguments=lambda p: p.add_argument("--force", action="store_true"))]
    (Path(env["DATA_DIR"])).mkdir(parents=True)
    (Path(env["DATA_DIR"]) / "serving.lock").write_text("1")
    code, out = run(["wipe"], env, commands=commands)
    assert code == 1 and "Refusing" in out.text
    code, out = run(["wipe", "--force"], env, commands=commands)
    assert code == 0 and out.text == "wiped demo-app in data"
    with pytest.raises(SystemExit):
        run(["wipe"], env)                                       # unknown without the app's command list
