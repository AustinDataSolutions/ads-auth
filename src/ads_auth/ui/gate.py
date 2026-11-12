"""Login gate, session validation, logout, and event logging for Streamlit."""
from __future__ import annotations

from functools import partial

from ads_auth import db, policy, tokens
from ads_auth.models import User
from ads_auth.store import LoginOutcome, UserStore
from ads_auth.ui import cookie, link
from ads_auth.usage import events

SESSION_USER_ID = "auth_user_id"
SESSION_VERSION = "auth_session_version"
SESSION_AUTHENTICATED_UTC = "auth_authenticated_utc"
EXPIRED_NOTICE = "auth_expired_notice"
PENDING_PAGE = "auth_pending_page"      # page name chosen in the sidebar while `confirm_leave` held
COOKIE_ACTION = "auth_cookie_action"    # ("set", token) or ("clear",): what the browser is asked to do with the sign-in cookie
COOKIE_BLOCKED = "auth_cookie_blocked"  # after sign-out or a rejected cookie: the cookie this websocket connected with is stale
SIGN_OUT_PENDING = "auth_sign_out_pending"  # a sign-out has begun: the gate finishes it before it looks at anything else
RESTORED = "auth_restored_from_cookie"  # this session was signed in by the cookie, not the sign-in form
LEAVE_QUESTION = "Do you still wish to proceed?"
INVALID_LOGIN_MESSAGE = "Sign-in was not accepted. Check your username and password and try again."
SETUP_MESSAGE = "This app has no accounts yet. The administrator must create the first account before anyone can sign in."

# Returned when AUTH_MODE=disabled. id 0 means "never log events for this principal".
LOCAL_DEV_USER = User(0, "local-dev", "Local development", "admin", True, False, None, 1, "1970-01-01T00:00:00+00:00", None)


def log(settings, event: str, user: User | None, **kwargs) -> None:
    """Write one usage event. Silent no-op for the local-dev principal."""
    if user is not None and user.id == 0:
        return
    with db.connection(settings.db_path) as conn:
        events.log_event(conn, event, allowed=settings.events, user=user, **kwargs)


# --- emptying a session while another script run of it is going ----------------------------------
#
# Streamlit starts the next run of a session as soon as the browser asks for one and tells the run in
# progress to stop. That run goes on until its next Streamlit call, and every `st.session_state`
# read, write or delete is one, inside a widget callback too. So the code below can be cut off at any
# session-state access (Streamlit's stop exception, a BaseException, comes out of it), and until the
# superseded run reaches such a call it works on the same `SessionState` as the new one: each run has
# its own lock around it. `st.session_state.clear()` copes with neither. It is `MutableMapping.clear`,
# which stops without a word at the first key that is gone when it comes to delete it.

_SIGNED_IN = (SESSION_USER_ID, SESSION_VERSION, SESSION_AUTHENTICATED_UTC, RESTORED)
_CLEAR_PASSES = 10      # times the session is listed and emptied before a run that keeps writing is left to it
_LIST_TRIES = 50        # times one listing is retried while another run is adding or removing entries


def _drop(state, key) -> None:
    try:
        del state[key]
    except KeyError:
        pass            # another run of this session removed it first


def _listed(state) -> list:
    """The session's keys, or none when another run kept changing it under every attempt."""
    for _ in range(_LIST_TRIES):
        try:
            return [key for key in state]
        except (KeyError, RuntimeError):    # an entry went, or one arrived, while Streamlit was collecting them
            continue
    return []


def clear_protected_state(st_module) -> None:
    """Drop everything a signed-in user produced: uploads, results, API clients, credentials.

    Two entries stay. `COOKIE_BLOCKED` says the cookie this connection arrived with is stale, which
    stays true whoever signs in next. `SIGN_OUT_PENDING` is taken off by the sign-out it belongs to.

    Another run of the same session may be reading and writing it meanwhile, so an entry that is
    already gone is not an error and the session is listed again until nothing is left. That is
    bounded: a run that writes an entry back for every one dropped keeps what it wrote after
    `_CLEAR_PASSES` rounds. The keys that make a session signed in are dropped regardless, and last:
    if this run is superseded partway, the session is either still the one `current_user` rejected,
    and is emptied again on the next check, or it is signed out and holds nothing the account made.
    """
    state = st_module.session_state
    later = (COOKIE_BLOCKED, SIGN_OUT_PENDING, *_SIGNED_IN)
    for _ in range(_CLEAR_PASSES):
        keys = [key for key in _listed(state) if key not in later]
        if not keys:
            break
        for key in keys:
            _drop(state, key)
    for key in _SIGNED_IN:
        _drop(state, key)


def _store(settings) -> UserStore:
    return UserStore(settings.db_path)


def current_user(st_module, settings) -> User | None:
    """The signed-in user if the session is still valid, else None (state cleared).

    Reads session state only. Restoring a session from the sign-in cookie is `require_login`'s job,
    so the cheap re-checks in fragments never touch the request. A sign-out that the run doing it
    could not finish (see `logout`) is finished here first.
    """
    if settings.auth_disabled:
        return LOCAL_DEV_USER
    state = st_module.session_state
    if state.get(SIGN_OUT_PENDING):
        _finish_sign_out(st_module, settings)   # the run that began it was superseded partway
        return None
    user_id = state.get(SESSION_USER_ID)
    if user_id is None:
        return None
    user = _store(settings).get_by_id(user_id)
    status = policy.session_status(user, state.get(SESSION_VERSION), state.get(SESSION_AUTHENTICATED_UTC))
    if status is policy.SessionStatus.OK:
        return user
    clear_protected_state(st_module)
    if status is not policy.SessionStatus.MISSING:
        state[EXPIRED_NOTICE] = True
    return None


def logout(st_module, settings) -> None:
    """Sign the session out. Runs as the Log out button's callback.

    The first thing written is `SIGN_OUT_PENDING`. If the browser asks for another run while this
    one is still at it, this one is stopped at its next session-state access, and the gate of the run
    that replaced it (`current_user`, so `require_login` too) finishes the sign-out before it looks
    at anything else. Superseded before that first write, nothing has happened and the account is
    still signed in. The event is written straight after the mark, with no session-state access in
    between, so there is one `logout` row for a sign-out that happens and none for one that does not.
    """
    user = current_user(st_module, settings)
    st_module.session_state[SIGN_OUT_PENDING] = True
    if user is not None:
        log(settings, "logout", user)
    _finish_sign_out(st_module, settings)


def _finish_sign_out(st_module, settings) -> None:
    """Everything a sign-out does to the session, in an order that is safe to stop after any step
    and to run again from the top (which is what happens when the run doing it is superseded)."""
    state = st_module.session_state
    if settings.cookie_enabled:
        # The websocket this session connected with still carries the old cookie, and it is still
        # valid. Refuse it before the account's keys go, so that no run that finds the session
        # signed out meanwhile restores it from the cookie.
        state[COOKIE_BLOCKED] = True
    clear_protected_state(st_module)
    if settings.cookie_enabled:
        _schedule_cookie(st_module, ("clear",))     # ask the browser to drop it
    _drop(state, SIGN_OUT_PENDING)


# --- the sign-in cookie --------------------------------------------------------------------------
#
# After an actual sign-in the browser is asked to store a signed token (`ads_auth.tokens`) in an
# HttpOnly cookie. A new Streamlit session (a reload, a new tab) that arrives with that cookie is
# restored silently: same account, same sign-in time, so the eight-hour lifetime and every revocation
# rule (`policy.session_status`) apply exactly as if the websocket had never dropped. Restores log no
# event; only a sign-in through the form logs `login`.

def _schedule_cookie(st_module, action: tuple) -> None:
    st_module.session_state[COOKIE_ACTION] = action


def _render_cookie_action(st_module, settings) -> None:
    """Emit the pending set/clear instruction for the browser once, on this run.

    It is drawn first and taken off the session after. Drawing is a point at which a superseded run
    stops, and an instruction already taken off would then never reach the browser: after a
    sign-out, the cookie would stay. Drawn and then stopped, it is drawn again by the next run,
    which does no harm.
    """
    state = st_module.session_state
    action = state.get(COOKIE_ACTION)
    if action is None:
        return
    if settings.cookie_enabled:
        cookie.apply_action(st_module, settings, action)
    _drop(state, COOKIE_ACTION)


def _session_token(st_module, settings) -> str | None:
    state = st_module.session_state
    user_id, version, when = state.get(SESSION_USER_ID), state.get(SESSION_VERSION), state.get(SESSION_AUTHENTICATED_UTC)
    if user_id is None or version is None or not when:
        return None
    return tokens.issue(settings.cookie_secret, user_id, version, when)


def remember_session(st_module, settings, *, now: bool = False) -> None:
    """(Re)issue the cookie for the signed-in session in session state.

    With `now` the browser instruction is drawn immediately (for a run that will not be followed
    by a rerun); otherwise it is drawn at the start of the next run by `require_login`.
    """
    if not settings.cookie_enabled:
        return
    token = _session_token(st_module, settings)
    if token is None:
        return
    if now:
        cookie.apply_action(st_module, settings, ("set", token))
    else:
        _schedule_cookie(st_module, ("set", token))


def _restore_from_cookie(st_module, settings, store: UserStore) -> User | None:
    """Adopt the session named by a valid cookie into session state; otherwise have the cookie cleared."""
    state = st_module.session_state
    if state.get(COOKIE_BLOCKED):
        return None
    raw = cookie.request_cookies(st_module).get(tokens.COOKIE_NAME)
    if not raw:
        return None
    claims = tokens.verify(settings.cookie_secret, raw)
    if claims is None:
        status, user = policy.SessionStatus.MISSING, None
    else:
        user = store.get_by_id(claims.user_id)
        status = policy.session_status(user, claims.session_version, claims.authenticated_utc)
    if status is policy.SessionStatus.OK:
        state[SESSION_USER_ID] = user.id
        state[SESSION_VERSION] = claims.session_version
        state[SESSION_AUTHENTICATED_UTC] = claims.authenticated_utc
        state[RESTORED] = True
        return user
    state[COOKIE_BLOCKED] = True
    _schedule_cookie(st_module, ("clear",))
    if status is not policy.SessionStatus.MISSING:
        state[EXPIRED_NOTICE] = True
    return None


def restored_from_cookie(st_module) -> bool:
    """Whether the sign-in cookie, not the sign-in form, signed this session in.

    True for the rest of a session that began by restoring from the cookie: a page reload, a new
    tab, or a browser that reconnected after the server restarted or dropped its connection. Such a
    session starts empty, so an app can say that uploads and results from before are not here. A
    sign-in through the form clears it (with the rest of session state); so does signing out.
    """
    return bool(st_module.session_state.get(RESTORED))


def require_login(st_module, settings) -> User:
    """Render the gate and stop the script unless a valid signed-in user exists."""
    if settings.auth_disabled:
        if settings.is_render:
            st_module.error("Authentication is disabled in a hosted environment. Refusing to serve.")
            st_module.stop()
        st_module.warning("Local development mode: authentication is disabled and nothing is recorded.")
        return LOCAL_DEV_USER

    store = _store(settings)
    user = current_user(st_module, settings)
    if user is None and settings.cookie_enabled:
        user = _restore_from_cookie(st_module, settings, store)
    _render_cookie_action(st_module, settings)
    if user is not None:
        if user.must_change_password:
            from ads_auth.ui.account import render_forced_change
            render_forced_change(st_module, settings, user)
            st_module.stop()
        return user

    st_module.title(settings.title)
    if store.count_users() == 0:
        st_module.info(SETUP_MESSAGE)
        st_module.stop()
    if st_module.session_state.pop(EXPIRED_NOTICE, False):
        st_module.info("Your session ended. Please sign in again.")
    with st_module.form("login_form", clear_on_submit=False):
        username = st_module.text_input("Username", key="login_username", autocomplete="username")
        password = st_module.text_input("Password", type="password", key="login_password", autocomplete="current-password")
        submitted = st_module.form_submit_button("Sign in")
    link(st_module, settings, "about", "About this app and data handling", "ℹ️")
    if submitted:
        found, outcome = store.verify_login(username, password)
        if outcome is LoginOutcome.SUCCESS:
            # Keeps `COOKIE_BLOCKED`: after a sign-out on this page the connection still carries
            # the previous account's cookie, and it must stay refused when this session ends.
            clear_protected_state(st_module)
            state = st_module.session_state
            state[SESSION_USER_ID] = found.id
            state[SESSION_VERSION] = found.session_version
            state[SESSION_AUTHENTICATED_UTC] = db.to_iso(db.utc_now())
            remember_session(st_module, settings)
            log(settings, "login", found)
            st_module.rerun()
        # Known account (any failure reason) -> attributed row; unknown/invalid -> anonymous row.
        log(settings, "login_failed", found if outcome not in (LoginOutcome.UNKNOWN, LoginOutcome.INVALID_INPUT) else None)
        st_module.error(INVALID_LOGIN_MESSAGE)
    st_module.stop()
    raise RuntimeError("unreachable")  # st.stop() raises inside Streamlit; keeps control flow explicit for fakes


def render_sidebar(st_module, settings, user: User, show_reports: bool, confirm_leave=None) -> None:
    """The account block and the links to the package's pages.

    `confirm_leave` is an optional callable. When it returns True (the screen holds work that a page
    change would discard: uploads, edited text), the page links are drawn as buttons that only note
    the chosen page in `session_state[PENDING_PAGE]`; the app then calls `confirm_navigation` to ask
    before leaving. When it is None or returns False the links navigate at once, as before.
    """
    guard = bool(confirm_leave is not None and confirm_leave())
    with st_module.sidebar:
        # Account block: identity, then the two things a user can do about it.
        st_module.markdown(f"Signed in as **{user.full_name or user.username}**")
        with st_module.expander("Change password"):
            from ads_auth.ui.account import render_change_form
            render_change_form(st_module, settings, user)
        if user.id != 0:
            # on_click runs before the next script run, so that run starts signed out
            # and draws the login screen directly without a mid-run rerun.
            st_module.button("Log out", key="logout_button", on_click=logout, args=(st_module, settings))
        if show_reports:
            _page_link(st_module, settings, "reports", "Usage statistics", "📊", guard)
        _page_link(st_module, settings, "about", "About this app and data handling", "ℹ️", guard)


def _page_link(st_module, settings, name: str, label: str, icon: str, guard: bool) -> None:
    if not guard:
        link(st_module, settings, name, label, icon)
    elif settings.pages.get(name) is not None:
        st_module.button(label, icon=icon, key=f"auth_nav_{name}", on_click=_request_page, args=(st_module, name))


def _request_page(st_module, name: str) -> None:
    st_module.session_state[PENDING_PAGE] = name


def _clear_pending_page(st_module) -> None:
    st_module.session_state.pop(PENDING_PAGE, None)


def confirm_navigation(st_module, settings, message: str, *, title: str = "Leave this page?") -> None:
    """Ask before leaving when a sidebar link was clicked while `confirm_leave` held.

    Call it once per run, after `render_sidebar`. It does nothing unless a page link was clicked
    while the app's `confirm_leave` check returned True. Then it opens a dialog with `message`, the
    question "Do you still wish to proceed?", and two buttons: "Go back" (close the dialog, stay) and
    "Proceed" (`st.switch_page` to the chosen page, which discards the screen's widget state the way
    the link itself would have). Closing the dialog any other way counts as "Go back".
    """
    name = st_module.session_state.get(PENDING_PAGE)
    if not name:
        return
    page = settings.pages.get(name)
    if page is None:
        _clear_pending_page(st_module)
        return

    @st_module.dialog(title, on_dismiss=partial(_clear_pending_page, st_module))
    def _ask() -> None:
        st_module.write(message)
        st_module.write(f"**{LEAVE_QUESTION}**")
        back, proceed = st_module.columns(2)
        # The dialog body runs as a fragment, so a click inside it reruns only the dialog: Go back
        # must ask for a full rerun itself, which then draws no dialog because the choice is gone.
        if back.button("Go back", key="auth_leave_back"):
            _clear_pending_page(st_module)
            st_module.rerun()
        if proceed.button("Proceed", key="auth_leave_proceed", type="primary"):
            _clear_pending_page(st_module)
            st_module.switch_page(page)

    _ask()
