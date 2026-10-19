from pathlib import Path

import pytest

from ads_auth import AppSettings, db

FIXTURES = Path(__file__).parent / "fixtures"
DISCLOSURE = FIXTURES / "disclosure.md"
APP_NAME = "demo-app"


@pytest.fixture
def settings(tmp_path) -> AppSettings:
    """A migrated, auth-required settings object for a throwaway app."""
    s = AppSettings(app_name=APP_NAME, title="Demo App", data_dir=tmp_path / "data",
                    disclosure_path=DISCLOSURE, usage_exclude_users=("ops-test",), events=("search",))
    db.migrate(s.db_path)
    return s
