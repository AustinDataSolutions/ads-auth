"""Sign-out, an ended session, and sign-in while a second script run of the same session is going.

Streamlit starts the next run of a session the moment the browser asks for one (`runner.fastReruns`,
the default) and tells the run in progress to stop. That run carries on until its next Streamlit
call, and every `st.session_state` read, write or delete is such a call, inside a widget callback
too. So two things can happen to code that empties the session:

- it is cut off: the run it belongs to is superseded, and its next session-state access raises
  Streamlit's stop exception, wherever it had got to;
- it is raced: a superseded run that has not reached a stop point yet (or an app that switches the
  check off for a long computation) reads and writes the same `SessionState` at the same time. Each
  run wraps it in its own `SafeSessionState` with its own lock, so nothing keeps them apart.

The first half of this file runs the demo app through AppTest and supersedes the run at chosen
points, the way `AppSession.request_rerun` does. The second half puts two views on one real
`SessionState`, the way two script runners do.
"""
import sys
import threading
from pathlib import Path

import pytest
import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx
from streamlit.runtime.state import session_state_proxy
from streamlit.runtime.state.safe_session_state import SafeSessionState
from streamlit.runtime.state.session_state import SessionState
from streamlit.testing.v1 import AppTest

from ads_auth import db, tokens
from ads_auth.store import UserStore
from ads_auth.ui import cookie, gate
from ads_auth.ui.gate import (
    COOKIE_BLOCKED, RESTORED, SESSION_AUTHENTICATED_UTC, SESSION_USER_ID, SESSION_VERSION,
)

DEMO = Path(__file__).resolve().parent / "demo_app.py"
PW = "a-perfectly-adequate-password"
SECRET = "demo-cookie-secret-of-32-characters!"
DATA = [f"data_{i}" for i in range(40)]          # stands for what a signed-in user uploaded and produced
SIGNED_IN = (SESSION_USER_ID, SESSION_VERSION, SESSION_AUTHENTICATED_UTC, RESTORED)


# --- the demo app, with a run superseded partway ------------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.delenv("ADS_AUTH_COOKIE_SECRET", raising=False)
    monkeypatch.delenv("STREAMLIT_SERVER_COOKIE_SECRET", raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data))
    db_path = data / "app.db"
    db.migrate(db_path)
    return db_path


@pytest.fixture
def remembered(env, monkeypatch):
    """The demo app with the cookie on; returns the recorded browser instructions and a cookie jar."""
    monkeypatch.setenv("ADS_AUTH_COOKIE_SECRET", SECRET)
    actions, jar = [], {}
    monkeypatch.setattr(cookie, "apply_action", lambda st_module, settings, action: actions.append(action) or True)
    monkeypatch.setattr(cookie, "request_cookies", lambda st_module: dict(jar))
    return actions, jar


class Hooks:
    """Lets a test act at a chosen step of whatever the app's script thread does to the session."""

    def __init__(self, monkeypatch):
        self.on_delete = None       # called with (state, key) after an entry is deleted
        self.on_set = None          # called with (state, key) just before an entry is written
        self.on_log = None          # called with (settings, event) after a usage event is written
        self.settings = None        # the app's settings object, once the gate has logged something
        real_delete, real_set, real_log = SessionState.__delitem__, SessionState.__setitem__, gate.log

        def delete(state, key):
            real_delete(state, key)
            if self.on_delete is not None:
                self.on_delete(state, key)

        def write(state, key, value):
            if self.on_set is not None:
                self.on_set(state, key)
            real_set(state, key, value)

        def log(settings, event, user, **kwargs):
            self.settings = settings
            real_log(settings, event, user, **kwargs)
            if self.on_log is not None:
                self.on_log(settings, event)

        monkeypatch.setattr(SessionState, "__delitem__", delete)
        monkeypatch.setattr(SessionState, "__setitem__", write)
        monkeypatch.setattr(gate, "log", log)

    def quiet(self):
        self.on_delete = self.on_set = self.on_log = None


@pytest.fixture
def hooks(monkeypatch):
    return Hooks(monkeypatch)


def _supersede():
    """What `AppSession.request_rerun` does to the run in progress when the browser asks for another."""
    get_script_run_ctx().script_requests.request_stop()


def _signed_in(at) -> bool:
    return any(ti.label.startswith("🔍") for ti in at.text_input)


def _sign_in(at, username):
    at.text_input(key="login_username").input(username)
    at.text_input(key="login_password").input(PW)
    at.button[0].click().run()
    assert not at.exception, at.exception
    return at


def _session_with_data(username="pat"):
    at = AppTest.from_file(str(DEMO), default_timeout=30)
    at.run()
    _sign_in(at, username)
    assert _signed_in(at)
    for key in DATA:
        at.session_state[key] = key
    return at


def _held(at, keys):
    return [key for key in keys if key in at.session_state]


def _events(db_path, event):
    with db.connection(db_path) as conn:
        return [r[0] for r in conn.execute("SELECT username FROM usage_events WHERE event = ? ORDER BY id", (event,))]


def _stop_points(hooks):
    """Named moments at which the run that is signing out gets superseded (once)."""
    fired = []

    def once(condition):
        if condition and not fired:
            fired.append(True)
            _supersede()

    deleted = []

    def after_three(state, key):
        deleted.append(key)
        once(len(deleted) == 3)

    return {
        "while the sign-out is being recorded": ("on_log", lambda settings, event: once(event == "logout")),
        "after three entries are gone": ("on_delete", after_three),
        "as the last of the data goes": ("on_delete", lambda state, key: once(not any(k in state for k in DATA))),
        "as the account is dropped": ("on_delete", lambda state, key: once(key == SESSION_USER_ID)),
        "as the cookie is blocked": ("on_set", lambda state, key: once(key == COOKIE_BLOCKED)),
    }, fired


@pytest.mark.parametrize("moment", [
    "while the sign-out is being recorded",
    "after three entries are gone",
    "as the last of the data goes",
    "as the account is dropped",
    "as the cookie is blocked",
])
def test_a_sign_out_cut_off_by_the_next_run_is_finished_by_that_run(env, remembered, hooks, moment):
    actions, jar = remembered
    UserStore(env).create_user("pat", password=PW)
    at = _session_with_data()
    jar[tokens.COOKIE_NAME] = [a for a in actions if a[0] == "set"][-1][1]     # the connection's cookie still names pat
    actions.clear()
    points, fired = _stop_points(hooks)
    name, hook = points[moment]
    setattr(hooks, name, hook)

    at.button(key="logout_button").click().run()            # superseded at `moment`: stops at its next stop point
    assert fired and not at.exception
    hooks.quiet()
    at.run()                                                # the run that superseded it

    assert not at.exception, at.exception
    assert not _signed_in(at)
    assert {t.key for t in at.main.text_input} == {"login_username", "login_password"}
    assert _held(at, DATA) == [] and _held(at, SIGNED_IN) == []
    assert at.session_state[COOKIE_BLOCKED] is True and gate.SIGN_OUT_PENDING not in at.session_state
    assert actions == [("clear",)]                          # the browser was told to drop the cookie, once
    assert _events(env, "logout") == ["pat"]
    at.run()                                                # and the old cookie on the connection stays refused
    assert not _signed_in(at) and actions == [("clear",)]


def test_a_sign_out_cut_off_without_the_cookie_is_finished_too(env, hooks):
    UserStore(env).create_user("pat", password=PW)
    at = _session_with_data()
    points, fired = _stop_points(hooks)
    hooks.on_delete = points["after three entries are gone"][1]
    at.button(key="logout_button").click().run()
    assert fired
    hooks.quiet()
    at.run()
    assert not at.exception and not _signed_in(at)
    assert _held(at, DATA) == [] and _held(at, SIGNED_IN) == []
    assert COOKIE_BLOCKED not in at.session_state
    assert _events(env, "logout") == ["pat"]


def test_a_sign_out_superseded_before_it_starts_changes_nothing(env, remembered, hooks, monkeypatch):
    """The click is lost, as any click is when the next request overtakes it; nothing is half done."""
    actions, _ = remembered
    UserStore(env).create_user("pat", password=PW)
    at = _session_with_data()
    actions.clear()
    real = UserStore.get_by_id

    def looked_up_then_superseded(self, user_id):
        _supersede()
        return real(self, user_id)

    monkeypatch.setattr(UserStore, "get_by_id", looked_up_then_superseded)
    at.button(key="logout_button").click().run()
    monkeypatch.setattr(UserStore, "get_by_id", real)
    at.run()
    assert _signed_in(at) and _held(at, DATA) == DATA
    assert _events(env, "logout") == [] and actions == []


def test_another_run_at_the_gate_during_a_sign_out_is_not_signed_back_in_by_the_cookie(env, remembered, hooks):
    """The connection's cookie is still valid while the session is being emptied. A second run that
    asks the gate who is signed in at that moment must not be restored from it."""
    actions, jar = remembered
    UserStore(env).create_user("pat", password=PW)
    at = _session_with_data()
    jar[tokens.COOKIE_NAME] = [a for a in actions if a[0] == "set"][-1][1]
    actions.clear()
    seen = []

    def another_run_reaches_the_gate(state, key):
        if key == COOKIE_BLOCKED and not seen:              # the instant before the block exists
            seen.append("asked")
            settings = hooks.settings
            user = gate.current_user(st, settings) or gate._restore_from_cookie(st, settings, UserStore(settings.db_path))
            seen.append(user)

    hooks.on_set = another_run_reaches_the_gate
    at.button(key="logout_button").click().run()
    hooks.quiet()
    assert not at.exception, at.exception
    assert seen == ["asked", None]
    assert not _signed_in(at) and _held(at, SIGNED_IN) == [] and _held(at, DATA) == []
    at.run()
    assert not _signed_in(at)
    assert _events(env, "logout") == ["pat"]


def test_an_ended_session_cut_off_while_it_is_emptied_does_not_keep_its_data(env, hooks):
    """`current_user` empties a revoked session. Superseded as the account goes, it must not leave a
    signed-out session that still holds what the account uploaded."""
    store = UserStore(env)
    store.create_user("pat", password=PW)
    at = _session_with_data()
    store.set_temporary_password("pat")                      # the reset revokes the session
    points, fired = _stop_points(hooks)
    hooks.on_delete = points["as the account is dropped"][1]
    at.run()
    assert fired
    hooks.quiet()
    at.run()
    assert not at.exception and not _signed_in(at)
    assert _held(at, DATA) == [] and SESSION_USER_ID not in at.session_state


def test_an_ended_session_cut_off_early_is_emptied_by_the_next_run(env, hooks):
    store = UserStore(env)
    store.create_user("pat", password=PW)
    at = _session_with_data()
    store.set_temporary_password("pat")
    points, fired = _stop_points(hooks)
    hooks.on_delete = points["after three entries are gone"][1]
    at.run()
    assert fired
    hooks.quiet()
    at.run()
    assert not at.exception and not _signed_in(at)
    assert _held(at, DATA) == [] and _held(at, SIGNED_IN) == []
    assert any("session ended" in i.value for i in at.info)


def test_a_cookie_instruction_survives_a_run_superseded_as_it_is_drawn(env, remembered, hooks, monkeypatch):
    """Drawing the instruction is a stop point. One taken off the session first and then never
    drawn would leave the browser holding the cookie after a sign-out."""
    actions, _ = remembered
    UserStore(env).create_user("pat", password=PW)
    at = _session_with_data()
    actions.clear()
    superseded = []

    def draw(st_module, settings, action):
        if not superseded:
            superseded.append(True)
            _supersede()
        st_module.empty()                                    # stands for the iframe: any element is a stop point
        actions.append(action)
        return True

    monkeypatch.setattr(cookie, "apply_action", draw)
    at.button(key="logout_button").click().run()            # signs out; superseded while drawing "clear"
    assert superseded and actions == []
    at.run()
    assert not _signed_in(at) and actions == [("clear",)]
    at.run()
    assert actions == [("clear",)]                          # drawn once it got through, not again


def test_the_block_on_a_connections_cookie_outlasts_the_next_sign_in(env, remembered):
    """Alex signs out and Blake signs in on the same page without reloading it. The connection still
    carries Alex's cookie, which is still valid. When Blake's session ends, the page must show the
    sign-in screen, not Alex's account."""
    actions, jar = remembered
    store = UserStore(env)
    store.create_user("alex", password=PW, full_name="Alex")
    store.create_user("blake", password=PW, full_name="Blake")
    at = AppTest.from_file(str(DEMO), default_timeout=30)
    at.run()
    _sign_in(at, "alex")
    jar[tokens.COOKIE_NAME] = [a for a in actions if a[0] == "set"][-1][1]
    at.button(key="logout_button").click().run()
    _sign_in(at, "blake")
    assert any("Signed in as **Blake**" in m.value for m in at.sidebar.markdown)
    assert at.session_state[COOKIE_BLOCKED] is True
    store.set_temporary_password("blake")                    # Blake's session is revoked
    at.run()
    assert not at.exception and not _signed_in(at)
    assert not any("Alex" in m.value for m in at.sidebar.markdown)
    assert {t.key for t in at.main.text_input} == {"login_username", "login_password"}
    assert any("session ended" in i.value for i in at.info)


# --- two views on one real SessionState ----------------------------------------------------------

@pytest.fixture
def shared(monkeypatch):
    """`st.session_state` backed by one real `SessionState`, wrapped separately for each thread:
    what two script runners of the same session have."""
    state = SessionState()
    local = threading.local()

    def view():
        if not hasattr(local, "safe"):
            local.safe = SafeSessionState(state, lambda: None)
        return local.safe

    monkeypatch.setattr(session_state_proxy, "get_session_state", view)
    return state


def _fill():
    for key in DATA:
        st.session_state[key] = key
    st.session_state[SESSION_USER_ID] = 7
    st.session_state[SESSION_VERSION] = 1
    st.session_state[SESSION_AUTHENTICATED_UTC] = "2026-01-01T00:00:00+00:00"


def _left(keys=(*DATA, *SIGNED_IN)):
    return [key for key in keys if key in st.session_state]


def test_an_entry_another_run_removed_first_does_not_end_the_clear(shared, monkeypatch):
    """`st.session_state.clear()` is `MutableMapping.clear`: it stops, silently, at the first key
    that is gone by the time it is deleted."""
    _fill()
    real = SessionState.__delitem__
    raced = []

    def delete(state, key):
        if not raced:
            raced.append(key)
            real(state, key)                                 # the other run got there first
        real(state, key)

    monkeypatch.setattr(SessionState, "__delitem__", delete)
    gate.clear_protected_state(st)
    assert raced and _left() == []


def test_the_clear_finishes_while_another_thread_changes_the_same_session(shared):
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(25):
            _fill()
            done = threading.Event()

            def other_run():
                for n in range(400):
                    if done.is_set():
                        break
                    st.session_state[f"result_{n}"] = n      # a new entry: the listing can fail mid-way
                    try:
                        del st.session_state[DATA[n % len(DATA)]]
                    except KeyError:
                        pass

            thread = threading.Thread(target=other_run)
            thread.start()
            try:
                gate.clear_protected_state(st)
            finally:
                done.set()
                thread.join()
            assert _left() == []
            shared.clear()
    finally:
        sys.setswitchinterval(old)


def test_the_clear_is_bounded_when_another_run_keeps_writing(shared, monkeypatch):
    """A run that puts an entry back for every one dropped must not keep the sign-out going for
    ever. The clear gives up on what that run writes, never on the keys that sign a session in."""
    _fill()
    real = SessionState.__delitem__
    written = []

    def delete(state, key):
        real(state, key)
        assert len(written) < 5000, "still emptying the session after 5000 entries were written back"
        written.append(key)
        state[f"again_{len(written)}"] = key

    monkeypatch.setattr(SessionState, "__delitem__", delete)
    gate.clear_protected_state(st)
    assert _left() == []
    assert 0 < len(written) < 5000


def test_the_clear_keeps_only_the_block_on_the_connections_cookie(shared):
    _fill()
    st.session_state[COOKIE_BLOCKED] = True
    st.session_state[gate.COOKIE_ACTION] = ("clear",)
    st.session_state[gate.EXPIRED_NOTICE] = True
    gate.clear_protected_state(st)
    assert dict(st.session_state) == {COOKIE_BLOCKED: True}
