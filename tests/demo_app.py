"""The smallest consuming app: what every ADS app's router looks like with ads_auth.

Used by the AppTest-based gate tests. Settings come from the environment the test sets.
"""
import os
from pathlib import Path

import streamlit as st

from ads_auth import AppSettings, auth_mode_from_environ, cookie_secret_from_environ, db
from ads_auth.ui import about, reports
from ads_auth.ui.gate import confirm_navigation, log, render_sidebar, require_login

settings = AppSettings(
    app_name="demo-app",
    title="Demo App",
    data_dir=Path(os.environ["DATA_DIR"]),
    disclosure_path=Path(__file__).parent / "fixtures" / "disclosure.md",
    auth_mode=auth_mode_from_environ(os.environ),
    is_render=os.environ.get("RENDER", "").lower() == "true",
    events=("search",),
    cookie_secret=cookie_secret_from_environ(os.environ),
)


LEAVE_MESSAGE = "Leaving this page clears the search box."


def main_page():
    user = require_login(st, settings)
    # A typed search is the demo's "work in progress": leaving the page would clear it.
    render_sidebar(st, settings, user, show_reports=user.has_role("manager"),
                   confirm_leave=lambda: bool(st.session_state.get("search")))
    confirm_navigation(st, settings, LEAVE_MESSAGE)
    st.title("Demo App")
    if st.text_input("🔍 Search", key="search"):
        log(settings, "search", user)


def about_page():
    about.render(st, settings)


def reports_page():
    user = require_login(st, settings)
    reports.render(st, settings, user)


def run():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(settings.db_path)
    settings.pages["app"] = st.Page(main_page, title="Demo App", url_path="app", default=True)
    settings.pages["about"] = st.Page(about_page, title="About this app and data handling", url_path="about")
    settings.pages["reports"] = st.Page(reports_page, title="Usage statistics", url_path="reports")
    st.navigation(list(settings.pages.values()), position="hidden").run()


if __name__ == "__main__":
    run()
