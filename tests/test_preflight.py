from ads_auth import AppSettings, preflight


def _run(settings_or_error, **kwargs):
    lines = []

    def load():
        if isinstance(settings_or_error, Exception):
            raise settings_or_error
        return settings_or_error

    code = preflight.run(load, out=lines.append)
    return code, "\n".join(lines)


def _settings(data_dir, **kw):
    return AppSettings(app_name="demo-app", title="Demo", data_dir=data_dir, disclosure_path=data_dir / "d.md", **kw)


def test_settings_error_is_reported(tmp_path):
    code, text = _run(ValueError("AUTH_MODE must be 'required' or 'disabled'."))
    assert code == 2 and "AUTH_MODE" in text


def test_disabled_refused_on_render(tmp_path):
    code, text = _run(_settings(tmp_path, auth_mode="disabled", is_render=True))
    assert code == 3 and "Refusing" in text


def test_unwritable_data_dir(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    code, text = _run(_settings(blocker / "sub"))
    assert code == 4 and "not writable" in text


def test_ok_migrates_and_writes_lock(tmp_path):
    data = tmp_path / "data"
    code, text = _run(_settings(data, is_render=True))
    assert code == 0 and "schema v1" in text and "auth=required" in text and "stay_signed_in=off" in text
    code, text = _run(_settings(data, cookie_secret="k" * 32))
    assert code == 0 and "stay_signed_in=on" in text
    assert (data / "app.db").exists() and (data / "serving.lock").exists()
    assert _run(_settings(data, is_render=True))[0] == 0   # idempotent
