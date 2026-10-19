from dataclasses import dataclass
from pathlib import Path

import pytest

from ads_auth import AppSettings, auth_mode_from_environ, exclude_users_from_environ


def test_paths_and_derived_values(tmp_path):
    s = AppSettings(app_name="demo-app", title="Demo", data_dir=str(tmp_path), disclosure_path=str(tmp_path / "d.md"))
    assert s.data_dir == tmp_path and isinstance(s.disclosure_path, Path)
    assert s.db_path == tmp_path / "app.db"
    assert not s.auth_disabled and not s.is_render
    assert not tmp_path.joinpath("app.db").exists()        # constructing settings does no I/O
    assert s.pages == {}


def test_validation(tmp_path):
    with pytest.raises(ValueError):
        AppSettings(app_name=" ", title="Demo", data_dir=tmp_path, disclosure_path=tmp_path)
    with pytest.raises(ValueError):
        AppSettings(app_name="x", title="Demo", data_dir=tmp_path, disclosure_path=tmp_path, auth_mode="maybe")


def test_environ_helpers():
    assert auth_mode_from_environ({}) == "required"
    assert auth_mode_from_environ({"AUTH_MODE": " Disabled "}) == "disabled"
    with pytest.raises(ValueError):
        auth_mode_from_environ({"AUTH_MODE": "maybe"})
    assert exclude_users_from_environ({}, default="ops-test") == ("ops-test",)
    assert exclude_users_from_environ({"USAGE_EXCLUDE_USERS": " A , b,, "}) == ("a", "b")
    assert exclude_users_from_environ({"USAGE_EXCLUDE_USERS": ""}, default="ops-test") == ()


def test_apps_may_subclass_with_their_own_fields(tmp_path):
    @dataclass(frozen=True, kw_only=True)
    class MySettings(AppSettings):
        app_name: str = "my-app"
        title: str = "My App"
        disclosure_path: Path = tmp_path / "DATA_HANDLING.md"
        api_key: str = ""

    s = MySettings(data_dir=tmp_path, api_key="k")
    assert s.app_name == "my-app" and s.db_path == tmp_path / "app.db" and s.api_key == "k"
    s.pages["app"] = object()                                   # the registry is mutable even though settings are frozen
    assert "app" in s.pages
