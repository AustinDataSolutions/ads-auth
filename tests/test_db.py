import sqlite3
from datetime import datetime, timezone

from ads_auth import db


def test_migrate_is_idempotent_and_versioned(tmp_path):
    path = tmp_path / "app.db"
    assert db.migrate(path) == db.SCHEMA_VERSION
    assert db.migrate(path) == db.SCHEMA_VERSION
    with db.connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == db.SCHEMA_VERSION
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "login_attempts", "usage_events", "schema_version"} <= names


def test_connection_pragmas(tmp_path):
    path = tmp_path / "app.db"
    db.migrate(path)
    with db.connection(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_iso_format_matches_the_shared_export():
    moment = datetime(2026, 10, 12, 21, 28, 16, 123456, tzinfo=timezone.utc)
    assert db.to_iso(moment) == "2026-10-12T21:28:16+00:00"
    assert db.from_iso("2026-10-12T21:28:16+00:00") == moment.replace(microsecond=0)
    eastern = datetime.fromisoformat("2026-10-12T17:28:16-04:00")
    assert db.to_iso(eastern) == "2026-10-12T21:28:16+00:00"


def test_to_iso_rejects_naive():
    import pytest
    with pytest.raises(ValueError):
        db.to_iso(datetime(2026, 1, 1))


def test_backup_is_consistent_copy(tmp_path):
    path = tmp_path / "app.db"
    db.migrate(path)
    with db.connection(path) as conn, conn:
        conn.execute("INSERT INTO usage_events(ts_utc, event) VALUES ('2026-01-01T00:00:00+00:00', 'login')")
    dest = db.backup(path, tmp_path / "backups" / "copy.db")
    with sqlite3.connect(dest) as copy:
        assert copy.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
