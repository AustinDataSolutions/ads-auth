"""Startup checks run before Streamlit. Returns non-zero to refuse to serve.

Never prints secrets. Safe to run repeatedly. An app wraps this in its own `scripts/preflight.py`:

    sys.exit(preflight.run(lambda: load_settings(os.environ)))
"""
from __future__ import annotations

import os
from collections.abc import Callable

from ads_auth import db
from ads_auth.settings import AppSettings

LOCK_NAME = "serving.lock"


def run(load_settings: Callable[[], AppSettings], *, out=print) -> int:
    try:
        settings = load_settings()
    except ValueError as exc:
        out(f"preflight: {exc}")
        return 2
    if settings.is_render and settings.auth_disabled:
        out("preflight: AUTH_MODE=disabled is not allowed on Render. Refusing to start.")
        return 3
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        out(f"preflight: DATA_DIR {settings.data_dir} is not writable ({exc.__class__.__name__}).")
        return 4
    try:
        version = db.migrate(settings.db_path)
    except Exception as exc:  # noqa: BLE001
        out(f"preflight: database migration failed ({exc.__class__.__name__}).")
        return 5
    (settings.data_dir / LOCK_NAME).write_text(str(os.getpid()))
    out(f"preflight: ok (auth={settings.auth_mode}, schema v{version}, data_dir={settings.data_dir})")
    return 0
