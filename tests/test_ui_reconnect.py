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


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class HeldSession(Session):
    def __init__(self, session_id):
        super().__init__(session_id)
        self.shut_down = False

    def shutdown(self):
        self.shut_down = True


def held(storage, session_id):
    from streamlit.runtime.session_manager import SessionInfo

    info = SessionInfo(client=None, session=HeldSession(session_id))
    storage.save(info)
    return info.session


@pytest.fixture
def storage():
    from cachetools import TTLCache

    clock = Clock()
    storage = MemorySessionStorage()
    storage._cache = TTLCache(maxsize=4, ttl=3600, timer=clock)
    assert reconnect.release_on_expiry(storage)
    return storage, clock


def test_a_held_session_is_shut_down_when_its_hold_ends_and_not_before(storage):
    storage, clock = storage
    session = held(storage, "away")
    clock.now = 3599
    storage._cache.expire()
    assert not session.shut_down and storage.get("away").session is session
    clock.now = 3601
    storage._cache.expire()
    assert session.shut_down and storage.get("away") is None


def test_a_later_save_releases_sessions_whose_hold_ended(storage):
    storage, clock = storage
    first = held(storage, "first")
    clock.now = 4000
    held(storage, "second")
    assert first.shut_down


def test_a_session_that_reconnects_in_time_is_not_shut_down(storage):
    storage, clock = storage
    session = held(storage, "back")
    clock.now = 600
    assert storage.get("back").session is session
    storage.delete("back")
    clock.now = 5000
    storage._cache.expire()
    assert not session.shut_down


def test_a_full_store_shuts_down_the_session_it_evicts(storage):
    storage, _clock = storage
    sessions = [held(storage, f"s{i}") for i in range(5)]
    assert sessions[0].shut_down and not any(s.shut_down for s in sessions[1:])


def test_release_on_expiry_keeps_held_sessions_and_installs_once():
    storage = MemorySessionStorage(ttl_seconds=3600)
    session = held(storage, "kept")
    assert reconnect.release_on_expiry(storage)
    cache = storage._cache
    assert reconnect.release_on_expiry(storage) and storage._cache is cache
    assert storage.get("kept").session is session
