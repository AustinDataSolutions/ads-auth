"""Who has the app open right now: the sessions this server holds, read from Streamlit's memory.

Nothing here is stored or logged. The statistics page shows it so a manager can see who would lose
their work if the server restarted (a deploy, for example).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ads_auth.store import UserStore
from ads_auth.ui.gate import LOCAL_DEV_USER, SESSION_USER_ID

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenSession:
    """One session the server holds.

    name:      the account's full name, or its username; None when nobody has signed in on that page
    username:  None when nobody has signed in
    connected: False when the page was closed or lost its connection and the server is holding the
               session in case it comes back (`server.disconnectedSessionTTL`)
    running:   the page is running right now (processing, for example)
    current:   the page showing this list
    """

    name: str | None
    username: str | None
    connected: bool
    running: bool
    current: bool


def _sessions():
    """[(session, connected)] from Streamlit's session manager; [] outside a running server or when
    Streamlit has moved its internals."""
    try:
        from streamlit import runtime
    except ImportError:
        return []
    if not runtime.exists():
        return []
    manager = getattr(runtime.get_instance(), "_session_mgr", None)
    if manager is None or not hasattr(manager, "list_sessions"):
        LOGGER.warning("Streamlit's session manager moved; open sessions can't be listed.")
        return []
    for _attempt in range(3):
        try:
            # The server's own thread can change the held sessions while they are read
            return [(info.session, getattr(info, "client", None) is not None) for info in manager.list_sessions()]
        except RuntimeError:
            continue
    return []


def _current_session_id():
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except ImportError:
        return None
    ctx = get_script_run_ctx()
    return getattr(ctx, "session_id", None)


def _user_id(session):
    try:
        return session.session_state[SESSION_USER_ID]
    except (KeyError, AttributeError):
        return None


def open_sessions(settings, sessions=None, current_id=None) -> list[OpenSession]:
    """The sessions this server holds, connected ones first, then by name. `sessions` and
    `current_id` replace the live server's in tests."""
    sessions = _sessions() if sessions is None else sessions
    current_id = _current_session_id() if current_id is None else current_id
    store = None if settings.auth_disabled else UserStore(settings.db_path)
    found = []
    for session, connected in sessions:
        user = LOCAL_DEV_USER if settings.auth_disabled else None
        user_id = None if user is not None else _user_id(session)
        if user_id is not None:
            user = store.get_by_id(user_id)
        state = getattr(getattr(session, "_state", None), "name", "")
        found.append(OpenSession(
            name=(user.full_name or user.username) if user is not None else None,
            username=user.username if user is not None else None,
            connected=connected,
            running=state == "APP_IS_RUNNING",
            current=getattr(session, "id", None) == current_id,
        ))
    return sorted(found, key=lambda s: (not s.connected, s.name is None, (s.name or "").casefold()))
