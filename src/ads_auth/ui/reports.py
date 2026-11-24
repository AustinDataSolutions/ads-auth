"""Usage statistics page for managers and admins."""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from ads_auth import __version__, admin, db
from ads_auth.models import User
from ads_auth.store import UserStore
from ads_auth.ui import link, live_sessions
from ads_auth.usage import reports
from ads_auth.usage.events import RETENTION_MONTHS

# Generic caption. An app with more to say (what one of its events means, how repeats are counted)
# sets `AppSettings.definitions`, which replaces this text.
DEFINITIONS = (
    "**Active user**: an account with at least one sign-in or recorded app activity in the period. "
    "Failed sign-ins are not activity. Weeks start on Monday, newest period first; periods use Eastern time. "
    "Accounts listed under \"Exclude accounts\" are left out of every table and download on this page. "
    "Search terms and file contents are never recorded."
)


# Line colours for the weekly activity chart, one per account in legend order (the busiest account
# in the range gets the first colour). Eight colourblind-safe hues; a page with more accounts than
# that uses the twenty-colour set instead, since cycling the same eight would make two accounts look alike.
SERIES_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
MANY_COLORS = ("#4c78a8", "#9ecae9", "#f58518", "#ffbf79", "#54a24b", "#88d27a", "#b79a20", "#f2cf5b", "#439894", "#83bcb6",
               "#e45756", "#ff9d98", "#79706e", "#bab0ac", "#d67195", "#fcbfd2", "#b279a2", "#d6a5c9", "#9e765f", "#d8b5a5")


def series_colors(count: int) -> list[str]:
    """One colour per line, in legend order."""
    palette = SERIES_COLORS if count <= len(SERIES_COLORS) else MANY_COLORS
    return [palette[i % len(palette)] for i in range(count)]


def legend_html(accounts) -> str:
    """The chart's legend as a centred, wrapping row of names under the plot, each in full with its
    line's colour. Drawn here because the chart's own legend can only hug the left edge."""
    items = "".join(
        '<span style="white-space:nowrap"><span style="display:inline-block;width:0.65em;height:0.65em;border-radius:50%;'
        f'background:{color};margin-right:0.4em"></span>{html.escape(name)}</span>'
        for name, color in zip(accounts, series_colors(len(accounts))))
    return ('<div style="display:flex;flex-wrap:wrap;justify-content:center;gap:0.25rem 1.25rem;'
            f'font-size:0.875rem;margin:-0.5rem 0 0.5rem">{items}</div>')
CHART_WEEKS = 52


def csv_prefix(settings) -> str:
    """Start of every download's file name, so exports from different apps can be told apart."""
    return f"{settings.app_name}-"


# What each download holds. The keys are internal; the labels are what people see on the buttons.
REPORT_LABELS = {
    "aggregates": "Activity by period",
    "per_user": "Activity by user",
    "events": "Event log",
}
REPORT_DESCRIPTIONS = {
    "aggregates": "Activity by period: active users, logins and event counts for each day, week and month in the date range",
    "per_user": "Activity by user: logins and event counts for each account in the date range, busiest first, "
                "with its most recent login and activity on record",
    "events": "Event log: one row per recorded event in the date range (time, app, account, event)",
}
REPORT_HELP = {
    "aggregates": "Three sheets (Daily, Weekly, Monthly), one row per period in the date range: how many "
                  "accounts were active, and how many logins and other counted actions there were.",
    "per_user": "One row per account: its logins and other counted actions in the date range, "
                "and when it last logged in and was last active.",
    "events": "One row per recorded event in the date range: time, app, account and event type. "
              "Same columns as the other ADS apps' exports, so the files can be combined.",
}
DATE_FORMAT = "MM/DD/YYYY"
DATE_HELP = "Pick a day from the calendar, or type the date as month/day/year (11/24/2026) and press Enter."


def today_css(today: date) -> str:
    """A ring around today in the From and To calendars, which Streamlit leaves unmarked. The
    calendar labels each day for screen readers ("... November 24th 2026 ..."); the rule finds
    today by that label, so if a Streamlit upgrade rewords it the ring disappears and nothing breaks."""
    suffix = "th" if 11 <= today.day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(today.day % 10, "th")
    label = f"{today:%B} {today.day}{suffix} {today.year}"
    return ('<style>div[data-baseweb="calendar"] [role="gridcell"][aria-label*="' + label + '"]'
            "{box-shadow: inset 0 0 0 2px currentColor; border-radius: 50%; font-weight: 700;}</style>")


def report_about(settings, user: User, report: str, *, start: date, end: date, exclude, now: datetime) -> list[tuple[str, str]]:
    """The `About this report` sheet: what the file holds, which app it came from, who downloaded it
    and when, and the filters that shaped it."""
    who = f"{user.full_name} ({user.username})" if user.full_name and user.full_name != user.username else user.username
    date_range = f"{start.isoformat()} to {end.isoformat()}, both days included, in Eastern time"
    if report == "events":
        date_range += "; ts_utc is in UTC"
    about = [
        ("Report", REPORT_DESCRIPTIONS[report]),
        ("Source app", f"{settings.title} ({settings.app_name})"),
        ("Generated by", who),
        ("Generated at (Eastern)", now.astimezone(reports.DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")),
        ("Generated at (UTC)", now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("Date range", date_range),
        ("Excluded accounts", ", ".join(sorted(exclude)) if exclude else "None"),
    ]
    if report != "events":
        about.append(("Events counted", ", ".join(["login", *settings.events])))
    if report == "per_user":
        about.append(("Last login and last activity", "The most recent on record for the account, whatever the date range. "
                      "Someone who stays signed in can be active without a new login"))
    if report == "aggregates":
        about.append(("Periods", "One sheet each for days, weeks (Monday to Sunday) and months, newest first; "
                                 "the current period is marked complete = no"))
    about.append(("ads_auth version", __version__))
    return about


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


def _count_line(others) -> str:
    people = {session.username for session in others if session.username}
    unnamed = sum(1 for session in others if not session.username)
    parts = []
    if people:
        parts.append(f"{len(people)} other {'person has' if len(people) == 1 else 'people have'} the app open right now")
    if unnamed:
        parts.append(f"{unnamed} {'page is' if unnamed == 1 else 'pages are'} on the sign-in screen")
    return "; ".join(parts) + "."


def render_open_sessions(st_module, sessions, hold_minutes=None, detailed=False) -> None:
    """Who else has the app open on this server. The viewer's own page is left out.

    `detailed` is for the people who restart the server: each page by name and state, the hold
    window, and the deploy warning, loudly. Otherwise (managers, who in some apps are most of the
    users) a quiet count with no names; the per-user table below says who did what."""
    others = [session for session in sessions if not session.current]
    if not detailed:
        if others:
            st_module.info(_count_line(others), icon=":material/group:")
        else:
            st_module.caption("No one else has the app open right now.")
        return
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


def render_activity_chart(st_module, weeks, activity_by_user) -> None:
    """One line per account: its activity (sign-ins plus the app's counted actions) in each week of
    the range, the most recent CHART_WEEKS at most."""
    _weekly_chart(st_module, weeks, activity_by_user, "Actions", "No activity in this range.",
                  "Each account's activity in each week of the range: sign-ins plus every action counted toward "
                  f"an active user (see above), the most recent {CHART_WEEKS} weeks at most. Weeks start on Monday. "
                  "The busiest account in the range is listed first. "
                  "Scroll over the chart to zoom the dates, drag to move, double-click to reset.")


def render_logins_chart(st_module, weeks, logins_by_user) -> None:
    """Sign-ins only. Kept for apps that call it; the page itself draws `render_activity_chart`."""
    _weekly_chart(st_module, weeks, logins_by_user, "Sign-ins", "No sign-ins in this range.",
                  f"Sign-ins per account in each week of the range, the most recent {CHART_WEEKS} weeks at most. "
                  "Weeks start on Monday. The busiest account in the range is listed first. "
                  "Scroll over the chart to zoom the dates, drag to move, double-click to reset.")


def weekly_chart(weeks, by_user, y_label: str):
    """The Altair line chart: one line per account, coloured in `by_user` order (busiest first).
    It has no legend of its own; `legend_html` draws one under it. Scrolling zooms the dates,
    dragging pans, double-clicking resets, like Streamlit's own line chart."""
    import altair as alt

    accounts = list(by_user)
    frame = pd.DataFrame([
        {"Week of": pd.Timestamp(week), "Account": name, y_label: counts[i]}
        for name, counts in by_user.items() for i, week in enumerate(weeks)
    ])
    scale = alt.Scale(domain=accounts, range=series_colors(len(accounts)))
    # A short range gets one tick and one dot per week (a single week would otherwise draw nothing);
    # a long one is left to the axis and drawn as plain lines
    short = len(weeks) <= 13
    axis = alt.Axis(format="%b %-d", values=[pd.Timestamp(w).isoformat() for w in weeks]) if short else alt.Axis()
    return alt.Chart(frame).mark_line(point=short).encode(
        x=alt.X("Week of:T", title="Week of", axis=axis),
        y=alt.Y(f"{y_label}:Q", title=y_label, axis=alt.Axis(tickMinStep=1)),
        color=alt.Color("Account:N", sort=accounts, scale=scale, legend=None),
        tooltip=[alt.Tooltip("Account:N"), alt.Tooltip("Week of:T", format="%b %-d, %Y"), alt.Tooltip(f"{y_label}:Q")],
    ).properties(height=320).interactive(bind_y=False)


def _weekly_chart(st_module, weeks, by_user, y_label: str, empty: str, caption: str) -> None:
    if not by_user:
        st_module.caption(empty)
        return
    st_module.altair_chart(weekly_chart(weeks, by_user, y_label), width="stretch")
    st_module.markdown(legend_html(list(by_user)), unsafe_allow_html=True)
    st_module.caption(caption)


def _day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def range_summary(start: date, end: date, exclude) -> str:
    """The line under the filters: which dates and accounts every figure below covers."""
    days = (end - start).days + 1
    left_out = f" Left out: {', '.join(sorted(exclude))}." if exclude else ""
    return (f"**Showing {_day(start)} to {_day(end)}** ({days} {'day' if days == 1 else 'days'}, Eastern time). "
            f"Every chart, table and download below covers these dates.{left_out}")


def render(st_module, settings, user: User) -> None:
    link(st_module, settings, "app", "Back to the app", "⬅️")
    st_module.title("Usage statistics")
    if not user.has_role("manager"):
        st_module.error("This page is available to managers and administrators.")
        st_module.stop()
        return
    render_open_sessions(st_module, live_sessions.open_sessions(settings), _hold_minutes(),
                         detailed=user.has_role("admin"))
    st_module.caption(settings.definitions or DEFINITIONS)

    d_start, d_end = _default_range(settings.usage_since)
    col1, col2 = st_module.columns(2)
    start = col1.date_input("From", value=d_start, key="reports_from", format=DATE_FORMAT, help=DATE_HELP)
    end = col2.date_input("To", value=d_end, key="reports_to", format=DATE_FORMAT, help=DATE_HELP)
    if start > end:
        st_module.warning("The start date must be on or before the end date.")
        return

    # Accounts deleted since still have history under their name, so they can be left out too
    with db.connection(settings.db_path) as conn:
        in_history = reports.event_usernames(conn)
    known = sorted({u.username for u in UserStore(settings.db_path).list_users()} | set(settings.usage_exclude_users) | set(in_history))
    exclude = st_module.multiselect(
        "Exclude accounts", options=known, default=[n for n in settings.usage_exclude_users if n in known],
        key="reports_exclude",
        help="Test and operator accounts to leave out. The default list is set by the administrator. "
             "Deleted accounts that still have history are listed too.",
    )
    exclude = set(exclude or ())
    st_module.markdown(range_summary(start, end, exclude))
    st_module.markdown(today_css(db.utc_now().astimezone(reports.DISPLAY_TZ).date()), unsafe_allow_html=True)

    names = settings.events
    labels = {name: column_label(name) for name in names}
    with db.connection(settings.db_path) as conn:
        weekly = reports.active_users_by_period(conn, start, end, "week", exclude=exclude, events=names, activity=settings.activity_events)
        monthly = reports.active_users_by_period(conn, start, end, "month", exclude=exclude, events=names, activity=settings.activity_events)
        daily = reports.active_users_by_period(conn, start, end, "day", exclude=exclude, events=names, activity=settings.activity_events)
        per_user = reports.per_user_totals(conn, start, end, exclude=exclude, events=names)
        weeks, activity_by_user = reports.activity_by_user_by_week(conn, start, end, exclude=exclude, events=names,
                                                                   activity=settings.activity_events, weeks=CHART_WEEKS)
        all_events = reports.combined_events_rows(conn, settings.app_name, exclude=exclude, start=start, end=end)

    def frame(rows):
        return pd.DataFrame([{
            "Period start": r.period_start, "Period end": r.period_end, "Active users": r.active_users,
            "Logins": r.logins, **{labels[name]: r.counts[name] for name in names},
            "Complete": "yes" if r.complete else "in progress",
        } for r in reversed(rows)])                     # newest period first

    st_module.subheader("Weekly activity by user")
    render_activity_chart(st_module, weeks, activity_by_user)
    st_module.subheader("Activity by user (whole period)")
    st_module.dataframe(pd.DataFrame([{
        "User": r.username, "Logins": r.logins, **{labels[name]: r.counts[name] for name in names},
        "Last activity": r.last_activity,
    } for r in per_user]), hide_index=True, width="stretch")
    st_module.caption("Busiest account first. Last activity is the account's most recent login or counted action "
                      "on record, whatever the date range.")
    st_module.subheader("Active users")
    tabs = st_module.tabs(["Daily", "Weekly", "Monthly"])
    for tab, rows in zip(tabs, (daily, weekly, monthly)):
        tab.dataframe(frame(rows), hide_index=True, width="stretch")

    st_module.subheader("Downloads")
    now = db.utc_now()
    stamp = now.astimezone(reports.DISPLAY_TZ).date().isoformat()
    prefix = csv_prefix(settings)

    def about(report):
        return report_about(settings, user, report, start=start, end=end, exclude=exclude, now=now)

    # One sheet per kind of period, newest first like the tables above, so each can be sorted and filtered alone
    period_sheets = [(title, reports.period_header(names), reports.period_rows(list(reversed(rows)), names))
                     for title, rows in (("Daily", daily), ("Weekly", weekly), ("Monthly", monthly))]
    files = (
        ("aggregates", "usage-by-period", reports.xlsx_sheets(period_sheets, about("aggregates"))),
        ("per_user", "usage-by-user",
         reports.xlsx_workbook(reports.per_user_header(names), reports.per_user_rows(per_user, names), about("per_user"))),
        ("events", "usage-events", reports.xlsx_workbook(reports.COMBINED_HEADER, all_events, about("events"))),
    )
    for column, (report, file_name, data) in zip(st_module.columns(3), files):
        column.download_button(f"{REPORT_LABELS[report]} (Excel)", data,
                               f"{prefix}{file_name}-{stamp}.xlsx", reports.XLSX_MIME, help=REPORT_HELP[report])
    st_module.caption(
        f"All three cover {_day(start)} to {_day(end)} and leave out the excluded accounts. "
        "**Activity by period**: active users, logins and counted actions for each day, week and month, one sheet for each. "
        "**Activity by user**: the same counts for each account, with its last login and last activity. "
        "**Event log**: one row per recorded event. Each file's second sheet says how it was made.")

    if user.has_role("admin"):
        render_admin_log(st_module, settings)


def render_admin_log(st_module, settings) -> None:
    """Administrators only: account changes made from the sidebar's Manage users section."""
    st_module.subheader("Admin actions")
    with db.connection(settings.db_path) as conn:
        rows = admin.recent(conn)
    st_module.caption("Account changes made from Manage users, newest first. Only administrators see this table, "
                      "and it is not in the downloads. The date range and excluded accounts above don't apply. "
                      f"Rows are deleted after {RETENTION_MONTHS} months.")
    if not rows:
        st_module.caption("No admin actions yet.")
        return
    st_module.dataframe(pd.DataFrame([{
        "When (Eastern)": db.from_iso(r.ts_utc).astimezone(reports.DISPLAY_TZ).strftime("%Y-%m-%d %H:%M"),
        "Admin": r.admin,
        "Action": admin.ACTION_LABELS.get(r.action, r.action),
        "Account": r.account,
        "Detail": r.detail or "",
    } for r in rows]), hide_index=True, width="stretch")
