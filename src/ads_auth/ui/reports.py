"""Usage statistics page for managers and admins."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from ads_auth import db
from ads_auth.models import User
from ads_auth.store import UserStore
from ads_auth.ui import link, live_sessions
from ads_auth.usage import reports

# Generic caption. An app with more to say (what one of its events means, how repeats are counted)
# sets `AppSettings.definitions`, which replaces this text.
DEFINITIONS = (
    "**Active user**: an account with at least one sign-in or recorded app activity in the period. "
    "Failed sign-ins are not activity. Weeks start on Monday, newest period first; periods use Eastern time. "
    "Accounts listed under \"Exclude accounts\" are left out of every table and download on this page. "
    "Search terms and file contents are never recorded."
)


def csv_prefix(settings) -> str:
    return f"{settings.app_name}-"


def column_label(event: str) -> str:
    """Column heading for a count of `event`: words, capitalised, plural (`library_refresh` -> `Library refreshes`)."""
    words = event.replace("_", " ")
    if words.endswith("ss") or words.endswith(("sh", "ch", "x")):
        words += "es"
    elif words.endswith("s"):
        pass
    elif words.endswith("y") and words[-2:-1] not in "aeiou":
        words = words[:-1] + "ies"
    else:
        words += "s"
    return words[:1].upper() + words[1:]


def _default_range(since: date | None = None, today: date | None = None) -> tuple[date, date]:
    """The last twelve months, from the first of a month, but starting no earlier than `since`."""
    today = today or date.today()
    start = (today.replace(day=1) - timedelta(days=365)).replace(day=1)
    if since is not None and since > start:
        start = min(since, today)
    return start, today


def _hold_minutes() -> int | None:
    """How long the server keeps a session whose page closed (`server.disconnectedSessionTTL`)."""
    try:
        from streamlit import config
        return int(config.get_option("server.disconnectedSessionTTL")) // 60
    except Exception:
        return None


def _session_line(session, hold_minutes) -> str:
    if session.name is None:
        who = "Someone on the sign-in screen"
    elif session.username and session.name != session.username:
        who = f"**{session.name}** ({session.username})"
    else:
        who = f"**{session.name}**"
    if not session.connected:
        kept = f"for up to {hold_minutes} minutes" if hold_minutes else "for a while"
        state = f"page closed or connection lost; their work is kept {kept} in case the page comes back"
    elif session.running:
        state = "page open, working right now"
    else:
        state = "page open"
    return f"- {who}: {state}"


def render_open_sessions(st_module, sessions, hold_minutes=None) -> None:
    """Who else has the app open on this server, loudly, so a restart (a deploy, for example) isn't
    a surprise to them. The viewer's own page is left out."""
    others = [session for session in sessions if not session.current]
    if not others:
        st_module.success("No one else has the app open on this server right now.", icon=":material/check_circle:")
        return
    people = {session.username for session in others if session.username}
    pages = f"{len(others)} {'page is' if len(others) == 1 else 'pages are'}"
    by = f", by {len(people)} {'person' if len(people) == 1 else 'people'}" if people else ""
    st_module.warning(
        f"**{pages} open on this server right now{by}.** Restarting the server (a deploy, for example) "
        f"would end {'it' if len(others) == 1 else 'them'}, and anything not saved would be lost.\n\n"
        + "\n".join(_session_line(session, hold_minutes) for session in others),
        icon=":material/group:",
    )


def render(st_module, settings, user: User) -> None:
    link(st_module, settings, "app", "Back to the app", "⬅️")
    st_module.title("Usage statistics")
    if not user.has_role("manager"):
        st_module.error("This page is available to managers and administrators.")
        st_module.stop()
        return
    render_open_sessions(st_module, live_sessions.open_sessions(settings), _hold_minutes())
    st_module.caption(settings.definitions or DEFINITIONS)

    d_start, d_end = _default_range(settings.usage_since)
    col1, col2 = st_module.columns(2)
    start = col1.date_input("From", value=d_start, key="reports_from")
    end = col2.date_input("To", value=d_end, key="reports_to")
    if start > end:
        st_module.warning("The start date must be on or before the end date.")
        return

    known = sorted({u.username for u in UserStore(settings.db_path).list_users()} | set(settings.usage_exclude_users))
    exclude = st_module.multiselect(
        "Exclude accounts", options=known, default=[n for n in settings.usage_exclude_users if n in known],
        key="reports_exclude", help="Test and operator accounts to leave out. The default list is set by the administrator.",
    )
    exclude = set(exclude or ())

    names = settings.events
    labels = {name: column_label(name) for name in names}
    with db.connection(settings.db_path) as conn:
        weekly = reports.active_users_by_period(conn, start, end, "week", exclude=exclude, events=names, activity=settings.activity_events)
        monthly = reports.active_users_by_period(conn, start, end, "month", exclude=exclude, events=names, activity=settings.activity_events)
        daily = reports.active_users_by_period(conn, start, end, "day", exclude=exclude, events=names, activity=settings.activity_events)
        per_user = reports.per_user_totals(conn, start, end, exclude=exclude, events=names)
        combined_csv = reports.combined_events_csv(conn, settings.app_name, exclude=exclude) if user.has_role("manager") else ""

    def frame(rows):
        return pd.DataFrame([{
            "Period start": r.period_start, "Period end": r.period_end, "Active users": r.active_users,
            "Logins": r.logins, **{labels[name]: r.counts[name] for name in names},
            "Complete": "yes" if r.complete else "in progress",
        } for r in reversed(rows)])                     # newest period first

    st_module.subheader("Monthly active users")
    st_module.dataframe(frame(monthly), hide_index=True, width="stretch")
    st_module.subheader("Weekly active users")
    st_module.dataframe(frame(weekly), hide_index=True, width="stretch")
    st_module.subheader("Daily active users")
    st_module.dataframe(frame(daily), hide_index=True, width="stretch")
    st_module.subheader("Per user")
    st_module.dataframe(pd.DataFrame([{
        "User": r.username, "Logins": r.logins, **{labels[name]: r.counts[name] for name in names},
    } for r in per_user]), hide_index=True, width="stretch")

    st_module.subheader("Downloads")
    stamp = date.today().isoformat()
    prefix = csv_prefix(settings)
    c1, c2, c3 = st_module.columns(3)
    c1.download_button("Aggregates (CSV)", reports.aggregate_csv(monthly + weekly + daily, names), f"{prefix}usage-aggregates-{stamp}.csv", "text/csv")
    c2.download_button("Per user (CSV)", reports.per_user_csv(per_user, names), f"{prefix}usage-per-user-{stamp}.csv", "text/csv")
    c3.download_button("All events (CSV)", combined_csv, f"{prefix}usage-events-{stamp}.csv", "text/csv",
                       help="Same columns as the other ADS apps' exports, so the files can be combined. Excluded accounts are left out.")
