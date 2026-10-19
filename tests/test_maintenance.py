import time
from datetime import datetime, timedelta, timezone

from ads_auth import db, maintenance

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    path = tmp_path / "app.db"
    db.migrate(path)
    return path


def test_run_once_backs_up_daily_and_prunes(tmp_path):
    path = _db(tmp_path)
    with db.connection(path) as conn:
        conn.execute("INSERT INTO usage_events(ts_utc, username, event) VALUES ('2020-01-01T00:00:00+00:00', 'u', 'login')")
        conn.commit()
    first = maintenance.run_once(tmp_path, path, now=T0)
    assert first["backup"] is not None and first["pruned"] == 1
    again = maintenance.run_once(tmp_path, path, now=T0 + timedelta(hours=23))
    assert again["backup"] is None
    later = maintenance.run_once(tmp_path, path, now=T0 + timedelta(hours=24))
    assert later["backup"] is not None
    assert len(maintenance.list_backups(tmp_path)) == 2


def test_keeps_only_seven_backups(tmp_path):
    path = _db(tmp_path)
    for i in range(10):
        maintenance.make_backup(tmp_path, path, now=T0 + timedelta(days=i))
    names = [p.name for p in maintenance.list_backups(tmp_path)]
    assert len(names) == 7 and names[0] == "app-20261018T120000Z.db"


def test_background_thread_runs_and_is_daemon(tmp_path):
    path = _db(tmp_path)
    thread = maintenance.start_background(tmp_path, path, interval_seconds=3600)
    assert thread.daemon
    for _ in range(50):
        if maintenance.list_backups(tmp_path):
            break
        time.sleep(0.05)
    assert len(maintenance.list_backups(tmp_path)) == 1


def test_extra_task_runs_after_the_generic_pass_and_merges_its_result(tmp_path):
    path = _db(tmp_path)
    seen = []

    def extra(now):
        seen.append(now)
        return {"library": "ok", "backups_seen": len(maintenance.list_backups(tmp_path))}

    done = maintenance.run_once(tmp_path, path, now=T0, extra=extra)
    assert seen == [T0] and done["library"] == "ok" and done["backups_seen"] == 1
    assert maintenance.run_once(tmp_path, path, now=T0, extra=lambda now: None)["pruned"] == 0


def test_background_thread_passes_extra_and_survives_its_errors(tmp_path):
    path = _db(tmp_path)
    calls = []

    def extra(now):
        calls.append(now)
        raise RuntimeError("boom")

    thread = maintenance.start_background(tmp_path, path, interval_seconds=3600, extra=extra)
    for _ in range(50):
        if calls:
            break
        time.sleep(0.05)
    assert calls and thread.is_alive()
