"""A tab that reconnects while its old connection still looks open gets its own session back."""
from unittest.mock import MagicMock

import pytest
from streamlit.runtime.memory_session_storage import MemorySessionStorage
from streamlit.runtime.session_manager import ActiveSessionInfo
from streamlit.runtime.websocket_session_manager import WebsocketSessionManager

from ads_auth.ui import reconnect


class Connection:
    """Stands in for Streamlit's websocket handler: it names its session and can be closed."""

    def __init__(self, session_id=None):
        self._session_id = session_id
        self.closed = False

    def close(self):
        self.closed = True


class Session:
    def __init__(self, session_id):
        self.id = session_id


@pytest.fixture
def manager():
    yield WebsocketSessionManager(MemorySessionStorage(), MagicMock(), MagicMock(), MagicMock())


@pytest.fixture
def installed():
    original = WebsocketSessionManager.connect_session
    assert reconnect.keep_sessions_on_reconnect()
    yield
    WebsocketSessionManager.connect_session = original


def connected(manager, session_id="s-1"):
    old = Connection(session_id)
    session = Session(session_id)
    manager._active_session_info_by_id[session_id] = ActiveSessionInfo(old, session, script_run_count=4)
    return old, session


def test_a_reconnecting_tab_takes_over_its_session_and_the_old_connection_is_closed(manager):
    old, session = connected(manager)
    new = Connection()

    assert reconnect.rejoin(manager, new, "s-1") == "s-1"

    active = manager._active_session_info_by_id["s-1"]
    assert active.client is new and active.session is session and active.script_run_count == 4
    assert old.closed
    # When the old connection's close finally arrives, it no longer names the session.
    assert old._session_id is None


def test_streamlit_decides_when_the_session_is_not_connected_or_unnamed(manager):
    connected(manager)
    assert reconnect.rejoin(manager, Connection(), "someone-else") is None
    assert reconnect.rejoin(manager, Connection(), None) is None


def test_an_old_connection_the_package_cannot_detach_is_left_alone(manager):
    old, _session = connected(manager)
    old._session_id = "different"
    assert reconnect.rejoin(manager, Connection(), "s-1") is None
    assert manager._active_session_info_by_id["s-1"].client is old
    assert not old.closed


def test_the_installed_session_manager_rejoins_and_installing_twice_wraps_once(manager, installed):
    assert reconnect.keep_sessions_on_reconnect()
    assert not getattr(WebsocketSessionManager.connect_session.__wrapped__, reconnect._MARK, False)
    old, session = connected(manager)
    new = Connection()

    session_id = manager.connect_session(client=new, script_data=None, user_info={}, existing_session_id="s-1")

    assert session_id == "s-1"
    assert manager.get_active_session_info("s-1").client is new
    assert old.closed
