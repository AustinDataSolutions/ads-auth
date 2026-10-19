"""About and statistics pages rendered against a small fake Streamlit object."""
from datetime import date, datetime, timezone

import pytest

from ads_auth import db
from ads_auth.models import User
from ads_auth.ui import about, reports
from ads_auth.usage import events


class Stop(Exception):
    pass


class FakeSt:
    def __init__(self, dates=None):
        self.calls = []
        self.dates = dates or {}
        self.session_state = {}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self
        return record

    def stop(self):
        self.calls.append(("stop", (), {}))
        raise Stop()

    def columns(self, n):
        return [self] * (n if isinstance(n, int) else len(n))

    def date_input(self, label, value=None, key=None):
        self.calls.append(("date_input", (label,), {}))
        return self.dates.get(key, value)

    def multiselect(self, label, options=None, default=None, key=None, help=None):
        self.calls.append(("multiselect", (label, list(options or [])), {"default": list(default or [])}))
        return self.session_state.get(key, list(default or []))

    def of(self, name):
        return [c for c in self.calls if c[0] == name]


def _user(role):
    return User(1, "m", None, role, True, False, None, 1, "2026-01-01T00:00:00+00:00", None)


def test_about_renders_the_apps_disclosure_file_and_links_back(settings):
    st = FakeSt()
    settings.pages["app"] = "APP_PAGE"
    about.render(st, settings)
    assert st.of("page_link")[0][1][0] == "APP_PAGE"
    text = st.of("markdown")[0][1][0]
    assert text.startswith("# How this app handles your data")


def test_link_is_silent_for_unregistered_pages(settings):
    st = FakeSt()
    about.render(st, settings)
    assert not st.of("page_link")


def test_reports_denied_for_plain_user(settings):
    st = FakeSt()
    with pytest.raises(Stop):
        reports.render(st, settings, _user("user"))
    assert st.of("error") and not st.of("dataframe") and not st.of("download_button")


def test_reports_for_manager_show_tables_and_three_downloads(settings):
    with db.connection(settings.db_path) as conn:
        events.log_event(conn, "login", user=_user("manager"), now=datetime(2026, 10, 1, 15, tzinfo=timezone.utc))
        events.log_event(conn, "search", user=_user("manager"), now=datetime(2026, 10, 1, 15, 1, tzinfo=timezone.utc), allowed=settings.events)
    st = FakeSt(dates={"reports_from": date(2026, 9, 1), "reports_to": date(2026, 10, 31)})
    reports.render(st, settings, _user("manager"))
    assert st.of("caption")[0][1][0] == reports.DEFINITIONS            # no app-specific wording declared
    assert len(st.of("dataframe")) == 3
    downloads = st.of("download_button")
    assert len(downloads) == 3
    stamp = date.today().isoformat()
    assert [d[1][2] for d in downloads] == [f"demo-app-usage-aggregates-{stamp}.csv",
                                            f"demo-app-usage-per-user-{stamp}.csv",
                                            f"demo-app-usage-events-{stamp}.csv"]
    all_events = downloads[2][1][1]
    assert all_events.startswith("ts_utc,app,username,event\n2026-10-01T15:00:00+00:00,demo-app,m,login\n")
    monthly_frame = st.of("dataframe")[0][1][0]
    assert list(monthly_frame["Active users"]) == [1, 0]       # newest first: October one, September zero
    assert list(monthly_frame.columns) == ["Period start", "Period end", "Active users", "Logins", "Searches", "Complete"]
    assert list(monthly_frame["Searches"]) == [1, 0]
    assert list(st.of("dataframe")[2][1][0].columns) == ["User", "Logins", "Searches"]
    assert downloads[0][1][1].startswith("period_type,period_start,period_end,active_users,logins,search,complete\n")
    assert downloads[1][1][1] == "username,logins,search\nm,1,1\n"
    exclude_call = st.of("multiselect")[0]
    assert "ops-test" in exclude_call[1][1] and exclude_call[2]["default"] == ["ops-test"]


def test_reports_exclude_box_removes_an_account_everywhere(settings):
    dev = User(2, "ops-test", None, "admin", True, False, None, 1, "2026-01-01T00:00:00+00:00", None)
    with db.connection(settings.db_path) as conn:
        events.log_event(conn, "login", user=_user("manager"), now=datetime(2026, 10, 1, 15, tzinfo=timezone.utc))
        events.log_event(conn, "login", user=dev, now=datetime(2026, 10, 1, 15, tzinfo=timezone.utc))
    st = FakeSt(dates={"reports_from": date(2026, 10, 1), "reports_to": date(2026, 10, 31)})
    reports.render(st, settings, _user("manager"))
    monthly_frame = st.of("dataframe")[0][1][0]
    assert list(monthly_frame["Active users"]) == [1]                     # ops-test excluded by default
    assert "ops-test" not in st.of("download_button")[2][1][1]
    st = FakeSt(dates={"reports_from": date(2026, 10, 1), "reports_to": date(2026, 10, 31)})
    st.session_state["reports_exclude"] = []                              # manager clears the box
    reports.render(st, settings, _user("manager"))
    assert list(st.of("dataframe")[0][1][0]["Active users"]) == [2]


def test_reports_columns_follow_the_declared_vocabulary(tmp_path):
    from ads_auth import AppSettings
    s = AppSettings(app_name="demo-app", title="Demo App", data_dir=tmp_path, disclosure_path=tmp_path / "d.md",
                    events=("upload", "process", "library_refresh"), activity_events=("upload", "process"),
                    definitions="**Process**: one per run, including reruns.")
    db.migrate(s.db_path)
    with db.connection(s.db_path) as conn:
        events.log_event(conn, "process", user=_user("manager"), now=datetime(2026, 10, 1, 15, tzinfo=timezone.utc), allowed=s.events)
        events.log_event(conn, "library_refresh", user=_user("manager"), now=datetime(2026, 10, 1, 16, tzinfo=timezone.utc), allowed=s.events)
    st = FakeSt(dates={"reports_from": date(2026, 10, 1), "reports_to": date(2026, 10, 31)})
    reports.render(st, s, _user("manager"))
    assert st.of("caption")[0][1][0] == "**Process**: one per run, including reruns."
    monthly = st.of("dataframe")[0][1][0]
    assert list(monthly.columns) == ["Period start", "Period end", "Active users", "Logins", "Uploads", "Processes", "Library refreshes", "Complete"]
    assert list(monthly["Processes"]) == [1] and list(monthly["Library refreshes"]) == [1]
    assert list(monthly["Active users"]) == [1]
    assert list(st.of("dataframe")[2][1][0].columns) == ["User", "Logins", "Uploads", "Processes", "Library refreshes"]
    assert st.of("download_button")[1][1][1] == "username,logins,upload,process,library_refresh\nm,0,0,1,1\n"


def test_column_labels():
    assert [reports.column_label(n) for n in ("search", "upload", "library_refresh", "download", "process",
                                               "generate_labels", "tag", "run_audit", "email_results", "summary")] == [
        "Searches", "Uploads", "Library refreshes", "Downloads", "Processes",
        "Generate labels", "Tags", "Run audits", "Email results", "Summaries"]


def test_reports_rejects_inverted_range(settings):
    st = FakeSt(dates={"reports_from": date(2026, 10, 31), "reports_to": date(2026, 10, 1)})
    reports.render(st, settings, _user("admin"))
    assert st.of("warning") and not st.of("dataframe")
