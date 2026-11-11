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


def _sheets(data: bytes) -> dict[str, list[tuple]]:
    """Every sheet of a downloaded workbook as rows of cell values."""
    import io

    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(data))
    return {sheet.title: [tuple(row) for row in sheet.iter_rows(values_only=True)] for sheet in book.worksheets}


def _about(data: bytes) -> dict[str, str]:
    return dict(_sheets(data)["About this report"][1:])


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
    captions = [c[1][0] for c in st.of("caption")]
    assert reports.DEFINITIONS in captions                               # no app-specific wording declared
    assert len(st.of("dataframe")) == 4                                  # monthly, weekly, daily, per user
    assert [call[1][0] for call in st.of("subheader")][:3] == [
        "Monthly active users", "Weekly active users", "Daily active users"]
    daily_frame = st.of("dataframe")[2][1][0]
    assert len(daily_frame) == 61 and list(daily_frame["Period start"])[-1] == date(2026, 9, 1)
    assert daily_frame.loc[daily_frame["Period start"] == date(2026, 10, 1), "Active users"].tolist() == [1]
    # Outside a running server no one else has the app open; a manager gets a caption, nothing loud
    assert "No one else has the app open right now." in captions and not st.of("warning") and not st.of("success")
    # Streamlit drops use_container_width after 2025-12-31; the tables fill the page with width="stretch"
    assert all(call[2].get("width") == "stretch" and "use_container_width" not in call[2] for call in st.of("dataframe"))
    downloads = st.of("download_button")
    assert len(downloads) == 3
    assert [d[1][0] for d in downloads] == ["Aggregates (Excel)", "Per user (Excel)", "All events (Excel)"]
    assert all(d[1][3] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" for d in downloads)
    from ads_auth.usage.reports import DISPLAY_TZ
    stamp = datetime.now(DISPLAY_TZ).date().isoformat()
    assert [d[1][2] for d in downloads] == [f"demo-app-usage-aggregates-{stamp}.xlsx",
                                            f"demo-app-usage-per-user-{stamp}.xlsx",
                                            f"demo-app-usage-events-{stamp}.xlsx"]
    workbooks = [_sheets(d[1][1]) for d in downloads]
    assert all(list(book) == ["Data", "About this report"] for book in workbooks)
    all_events = workbooks[2]["Data"]
    assert all_events[:2] == [("ts_utc", "app", "username", "event"), ("2026-10-01T15:00:00+00:00", "demo-app", "m", "login")]
    monthly_frame = st.of("dataframe")[0][1][0]
    assert list(monthly_frame["Active users"]) == [1, 0]       # newest first: October one, September zero
    assert list(monthly_frame.columns) == ["Period start", "Period end", "Active users", "Logins", "Searches", "Complete"]
    assert list(monthly_frame["Searches"]) == [1, 0]
    assert list(st.of("dataframe")[3][1][0].columns) == ["User", "Logins", "Searches"]
    aggregates = workbooks[0]["Data"]
    assert aggregates[0] == ("period_type", "period_start", "period_end", "active_users", "logins", "search", "complete")
    assert ("day", datetime(2026, 10, 1), datetime(2026, 10, 1), 1, 1, 1, "yes") in aggregates   # real dates in Excel
    assert workbooks[1]["Data"] == [("username", "logins", "search"), ("m", 1, 1)]
    # The second sheet says where the figures came from and what shaped them
    about = _about(downloads[0][1][1])
    assert about["Report"].startswith("Aggregates")
    assert about["Source app"] == "Demo App (demo-app)"
    assert about["Generated by"] == "m"
    assert about["Generated at (UTC)"].startswith(datetime.now(timezone.utc).date().isoformat())
    assert about["Generated at (Eastern)"].endswith(("EST", "EDT"))
    assert about["Date range"] == "2026-09-01 to 2026-10-31, both days included, in Eastern time"
    assert about["Excluded accounts"] == "ops-test"
    assert about["Events counted"] == "login, search"
    assert about["ads_auth version"] == reports.__version__
    assert _about(downloads[1][1][1])["Report"].startswith("Per user")
    events_about = _about(downloads[2][1][1])
    assert events_about["Date range"].startswith("All stored events") and "Events counted" not in events_about
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
    assert all(row[2] != "ops-test" for row in _sheets(st.of("download_button")[2][1][1])["Data"])
    st = FakeSt(dates={"reports_from": date(2026, 10, 1), "reports_to": date(2026, 10, 31)})
    st.session_state["reports_exclude"] = []                              # manager clears the box
    reports.render(st, settings, _user("manager"))
    assert list(st.of("dataframe")[0][1][0]["Active users"]) == [2]
    assert _about(st.of("download_button")[0][1][1])["Excluded accounts"] == "None"


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
    assert "**Process**: one per run, including reruns." in [c[1][0] for c in st.of("caption")]
    monthly = st.of("dataframe")[0][1][0]
    assert list(monthly.columns) == ["Period start", "Period end", "Active users", "Logins", "Uploads", "Processes", "Library refreshes", "Complete"]
    assert list(monthly["Processes"]) == [1] and list(monthly["Library refreshes"]) == [1]
    assert list(monthly["Active users"]) == [1]
    assert list(st.of("dataframe")[3][1][0].columns) == ["User", "Logins", "Uploads", "Processes", "Library refreshes"]
    assert _sheets(st.of("download_button")[1][1][1])["Data"] == [
        ("username", "logins", "upload", "process", "library_refresh"), ("m", 0, 0, 1, 1)]


def test_column_labels():
    assert [reports.column_label(n) for n in ("search", "upload", "library_refresh", "download", "process",
                                               "generate_labels", "tag", "run_audit", "email_results", "summary")] == [
        "Searches", "Uploads", "Library refreshes", "Downloads", "Processes",
        "Generate labels", "Tags", "Run audits", "Email results", "Summaries"]


def test_reports_rejects_inverted_range(settings):
    st = FakeSt(dates={"reports_from": date(2026, 10, 31), "reports_to": date(2026, 10, 1)})
    reports.render(st, settings, _user("admin"))
    assert st.of("warning") and not st.of("dataframe")


def test_default_range_is_twelve_months_but_never_before_usage_since():
    assert reports._default_range(today=date(2026, 11, 5)) == (date(2025, 11, 1), date(2026, 11, 5))
    assert reports._default_range(date(2026, 10, 1), today=date(2026, 11, 5)) == (date(2026, 10, 1), date(2026, 11, 5))
    # Once twelve months reach past the floor, the floor stops mattering
    assert reports._default_range(date(2026, 10, 1), today=date(2027, 12, 2)) == (date(2026, 12, 1), date(2027, 12, 2))
    assert reports._default_range(date(2027, 1, 1), today=date(2026, 11, 5))[0] == date(2026, 11, 5)


def test_reports_start_the_default_range_on_usage_since(tmp_path):
    from ads_auth import AppSettings
    s = AppSettings(app_name="demo-app", title="Demo App", data_dir=tmp_path, disclosure_path=tmp_path / "d.md",
                    usage_since=date(2026, 10, 1))
    db.migrate(s.db_path)
    st = FakeSt()
    reports.render(st, s, _user("manager"))
    starts = [call for call in st.of("date_input")]
    assert starts and st.of("dataframe")
    assert st.of("dataframe")[0][1][0]["Period start"].min() >= date(2026, 10, 1)


def test_open_sessions_are_loud_and_name_each_person():
    from ads_auth.ui.live_sessions import OpenSession
    sessions = [
        OpenSession("Ana Diaz", "ana", connected=True, running=True, current=False),
        OpenSession("Ana Diaz", "ana", connected=False, running=False, current=False),
        OpenSession(None, None, connected=True, running=False, current=False),
        OpenSession("Mo", "mo", connected=True, running=False, current=True),
    ]
    st = FakeSt()
    reports.render_open_sessions(st, sessions, hold_minutes=60, detailed=True)
    (call,) = st.of("warning")
    text = call[1][0]
    assert text.startswith("**3 pages are open on this server right now, by 1 person.**")
    assert "- **Ana Diaz** (ana): page open, working right now" in text
    assert "- **Ana Diaz** (ana): page closed or connection lost; their work is kept for up to 60 minutes" in text
    assert "- Someone on the sign-in screen: page open" in text
    assert "Mo" not in text                                              # the viewer's own page
    st = FakeSt()
    reports.render_open_sessions(st, sessions[3:], detailed=True)
    assert st.of("success") and not st.of("warning")


def test_open_sessions_give_managers_a_count_and_no_names():
    from ads_auth.ui.live_sessions import OpenSession
    sessions = [
        OpenSession("Ana Diaz", "ana", connected=True, running=True, current=False),
        OpenSession("Ana Diaz", "ana", connected=False, running=False, current=False),
        OpenSession("Bo", "bo", connected=True, running=False, current=False),
        OpenSession(None, None, connected=True, running=False, current=False),
        OpenSession("Mo", "mo", connected=True, running=False, current=True),
    ]
    st = FakeSt()
    reports.render_open_sessions(st, sessions, hold_minutes=60)
    (call,) = st.of("info")
    assert call[1][0] == "2 other people have the app open right now; 1 page is on the sign-in screen."
    assert not st.of("warning")
    st = FakeSt()
    reports.render_open_sessions(st, sessions[:1], hold_minutes=60)
    assert st.of("info")[0][1][0] == "1 other person has the app open right now."
    st = FakeSt()
    reports.render_open_sessions(st, sessions[4:])
    assert st.of("caption")[0][1][0] == "No one else has the app open right now." and not st.of("info")


def test_reports_show_the_detailed_notice_to_admins_only(settings):
    for role, loud in (("manager", False), ("admin", True)):
        st = FakeSt(dates={"reports_from": date(2026, 9, 1), "reports_to": date(2026, 10, 31)})
        reports.render(st, settings, _user(role))
        assert bool(st.of("success")) is loud   # no one else is on outside a server: green banner for admins only


def test_open_sessions_read_each_sessions_signed_in_account(settings):
    from types import SimpleNamespace

    from ads_auth.store import UserStore
    from ads_auth.ui import live_sessions
    from ads_auth.ui.gate import SESSION_USER_ID

    store = UserStore(settings.db_path)
    ana, _ = store.create_user("ana", full_name="Ana Diaz", temporary=True)

    def session(session_id, state, run_state="APP_NOT_RUNNING"):
        return SimpleNamespace(id=session_id, session_state=state, _state=SimpleNamespace(name=run_state))

    found = live_sessions.open_sessions(settings, sessions=[
        (session("held", {SESSION_USER_ID: ana.id}), False),
        (session("signin", {}), True),
        (session("busy", {SESSION_USER_ID: ana.id}, "APP_IS_RUNNING"), True),
        (session("me", {SESSION_USER_ID: ana.id}), True),
    ], current_id="me")
    assert [(s.name, s.connected, s.running, s.current) for s in found] == [
        ("Ana Diaz", True, True, False), ("Ana Diaz", True, False, True),
        (None, True, False, False), ("Ana Diaz", False, False, False)]
    # Outside a running Streamlit server there is nothing to list
    assert live_sessions.open_sessions(settings) == []


def test_report_about_names_the_person_and_the_filters(settings):
    named = User(3, "ana", "Ana Diaz", "manager", True, False, None, 1, "2026-01-01T00:00:00+00:00", None)
    now = datetime(2026, 11, 6, 19, 5, 9, tzinfo=timezone.utc)
    about = dict(reports.report_about(settings, named, "per_user", start=date(2026, 10, 1), end=date(2026, 10, 31),
                                      exclude={"zed", "ops-test"}, now=now))
    assert about["Generated by"] == "Ana Diaz (ana)"
    assert about["Generated at (Eastern)"] == "2026-11-06 14:05:09 EST"
    assert about["Generated at (UTC)"] == "2026-11-06T19:05:09Z"
    assert about["Excluded accounts"] == "ops-test, zed"
    assert "Periods" not in about
