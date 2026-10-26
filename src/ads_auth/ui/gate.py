"""Login gate, session validation, logout, and event logging for Streamlit."""
from __future__ import annotations

from functools import partial

from ads_auth import db, policy
from ads_auth.models import User
from ads_auth.store import LoginOutcome, UserStore
from ads_auth.ui import link
from ads_auth.usage import events

SESSION_USER_ID = "auth_user_id"
SESSION_VERSION = "auth_session_version"
SESSION_AUTHENTICATED_UTC = "auth_authenticated_utc"
EXPIRED_NOTICE = "auth_expired_notice"
PENDING_PAGE = "auth_pending_page"      # page name chosen in the sidebar while `confirm_leave` held
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


def clear_protected_state(st_module) -> None:
    """Drop everything a signed-in user produced: uploads, results, API clients, credentials."""
    st_module.session_state.clear()


def _store(settings) -> UserStore:
    return UserStore(settings.db_path)


def current_user(st_module, settings) -> User | None:
    """The signed-in user if the session is still valid, else None (state cleared)."""
    if settings.auth_disabled:
        return LOCAL_DEV_USER
    state = st_module.session_state
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
    user = current_user(st_module, settings)
    if user is not None:
        log(settings, "logout", user)
    clear_protected_state(st_module)


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
            clear_protected_state(st_module)
            state = st_module.session_state
            state[SESSION_USER_ID] = found.id
            state[SESSION_VERSION] = found.session_version
            state[SESSION_AUTHENTICATED_UTC] = db.to_iso(db.utc_now())
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
