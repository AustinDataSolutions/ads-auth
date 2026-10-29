"""Give a browser tab that reconnects its own session back instead of an empty one.

A Streamlit page that loses its websocket reconnects and names the session it had. If the server
has already seen the old connection close, Streamlit reattaches that session. If it has not (a
proxy can keep the server's side of a dead connection open long after the browser's side is
gone, for example while a laptop sleeps), Streamlit logs "Session with id ... is already
connected! Connecting to a new session." and starts an empty one, leaving the old session and
everything in it unreachable in memory.

The browser keeps that session id only in the open page's memory: a reload or a duplicated tab
starts without one. So a reconnect naming a session that still looks connected is that same page
coming back, and `keep_sessions_on_reconnect()` hands the session to the new connection and
closes the old one. This reaches into Streamlit's session manager, which is not public API; when
the old connection is not one the package knows how to detach, Streamlit's own behavior stays.

Once the server has seen a connection close, Streamlit holds the session for
`server.disconnectedSessionTTL` seconds in case the page comes back. When that hold ends, Streamlit
only drops its record of the session: the uploaded files and prepared downloads stay in memory until
the server restarts. `keep_sessions_on_reconnect()` also shuts such a session down when its hold
ends, as closing it would, checking once a minute.
"""
from __future__ import annotations

from cachetools import TTLCache
from streamlit.logger import get_logger

# Streamlit's logger, so the line lands in the server log beside Streamlit's own reconnect lines.
LOGGER = get_logger(__name__)
_MARK = "_ads_auth_rejoins"
_RELEASING = "_ads_auth_releases_expired"
SWEEP_SECONDS = 60


def rejoin(manager, client, existing_session_id) -> str | None:
    """Move an active session to `client` and return its id, or None to leave it to Streamlit."""
    if not existing_session_id:
        return None
    sessions = getattr(manager, "_active_session_info_by_id", None)
    active = sessions.get(existing_session_id) if isinstance(sessions, dict) else None
    if active is None or active.client is client:
        return None
    stale = active.client
    # The old connection's close handler disconnects the session it names; once it names none,
    # its eventual close leaves the session with the new connection.
    if getattr(stale, "_session_id", None) != existing_session_id:
        return None
    stale._session_id = None
    sessions[existing_session_id] = type(active)(
        client, active.session, active.script_run_count
    )
    try:
        stale.close()
    except Exception:  # an already-dead connection may refuse a close frame
        LOGGER.debug("Closing the replaced connection failed", exc_info=True)
    LOGGER.info("A browser tab reconnected and rejoined its session.")
    return existing_session_id


def _patched(original):
    def connect_session(self, client, script_data, user_info, existing_session_id=None,
                        session_id_override=None):
        rejoined = None if session_id_override else rejoin(self, client, existing_session_id)
        if rejoined is not None:
            return rejoined
        return original(self, client, script_data, user_info, existing_session_id=existing_session_id,
                        session_id_override=session_id_override)

    setattr(connect_session, _MARK, True)
    connect_session.__wrapped__ = original
    return connect_session


def _shut_down(info) -> None:
    try:
        info.session.shutdown()
    except Exception:
        LOGGER.warning("A disconnected session could not be shut down.", exc_info=True)
    else:
        LOGGER.info("Released a disconnected session after its hold ended.")


class _ReleasingCache(TTLCache):
    """Streamlit's held-session cache, shutting each session down as it leaves."""

    def expire(self, time=None):
        expired = super().expire(time)
        for _session_id, info in expired:
            _shut_down(info)
        return expired

    def popitem(self):
        session_id, info = super().popitem()
        _shut_down(info)
        return session_id, info


def release_on_expiry(storage) -> bool:
    """Make a MemorySessionStorage shut sessions down when their hold ends. Run on the event loop."""
    cache = getattr(storage, "_cache", None)
    if isinstance(cache, _ReleasingCache):
        return True
    if not isinstance(cache, TTLCache):
        return False
    releasing = _ReleasingCache(maxsize=cache.maxsize, ttl=cache.ttl, timer=cache.timer)
    for session_id, info in list(cache.items()):
        releasing[session_id] = info
    storage._cache = releasing
    return True


def _start_releasing() -> None:
    """Once per server: swap in the releasing cache and check it every SWEEP_SECONDS."""
    from streamlit import runtime

    if not runtime.exists():
        return
    server = runtime.get_instance()
    storage = getattr(getattr(server, "_session_mgr", None), "_session_storage", None)
    loop = getattr(getattr(server, "_async_objs", None), "eventloop", None)
    if storage is None or loop is None:
        LOGGER.warning("Streamlit's session storage moved; disconnected sessions keep their files until a restart.")
        return
    if getattr(storage, _RELEASING, False):
        return
    setattr(storage, _RELEASING, True)

    def sweep():
        try:
            storage._cache.expire()
        except Exception:
            LOGGER.warning("Checking disconnected sessions failed.", exc_info=True)
        loop.call_later(SWEEP_SECONDS, sweep)

    def start():
        if release_on_expiry(storage):
            loop.call_later(SWEEP_SECONDS, sweep)
        else:
            LOGGER.warning("Streamlit's session storage changed; disconnected sessions keep their files until a restart.")

    loop.call_soon_threadsafe(start)


def keep_sessions_on_reconnect() -> bool:
    """Install the rejoin and the release of expired sessions into this process's Streamlit server
    once. Returns whether the rejoin is active."""
    try:
        from streamlit.runtime.websocket_session_manager import WebsocketSessionManager
    except ImportError:
        LOGGER.warning("Streamlit's session manager moved; reconnecting tabs will get new sessions.")
        return False
    _start_releasing()
    if not getattr(WebsocketSessionManager.connect_session, _MARK, False):
        WebsocketSessionManager.connect_session = _patched(WebsocketSessionManager.connect_session)
    return True
