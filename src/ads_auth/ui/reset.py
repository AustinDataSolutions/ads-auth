"""Self-service password reset screens, drawn by the sign-in gate.

- "Forgot your password?" under the sign-in form opens the request screen (username or email).
- After a request: the same "Check your email" message whether or not an email went out.
- The emailed link opens the app at `?reset=<token>`; the token is moved into the session and taken
  out of the address bar at once, then the person chooses a new password.

Shown only when `settings.password_reset_enabled`. The package owns the session keys starting
`auth_reset_`.
"""
from __future__ import annotations

from ads_auth import mail, resets
from ads_auth.hashing import MIN_NEW_PASSWORD_LENGTH
from ads_auth.store import StoreError

MODE = "auth_reset_mode"        # "request" or "sent" while the sign-in screen shows a reset step
TOKEN = "auth_reset_token"      # the token from an opened link, moved here from the address bar
DONE = "auth_reset_done"        # the new password is set; show the confirmation until Sign in

CHECK_EMAIL = ("**Check your email.** If that matches an account with an email address, we've sent a link "
               f"to choose a new password. The link works once, for {int(resets.LINK_LIFETIME.total_seconds() // 60)} minutes.")
EXPIRED = ("**This reset link has expired or was already used.** Links work once, for "
           f"{int(resets.LINK_LIFETIME.total_seconds() // 60)} minutes. You can ask for a new one.")


def _set_mode(st_module, mode: str | None) -> None:
    state = st_module.session_state
    for key in (TOKEN, DONE):
        state.pop(key, None)
    if mode is None:
        state.pop(MODE, None)
    else:
        state[MODE] = mode


def capture_token(st_module) -> None:
    """Move `?reset=<token>` from the address bar into the session, so it isn't left in the
    browser's history or shown on screen."""
    params = st_module.query_params
    if resets.QUERY_PARAM in params:
        token = params.get(resets.QUERY_PARAM)
        del params[resets.QUERY_PARAM]
        if token:
            state = st_module.session_state
            state.pop(DONE, None)
            state.pop(MODE, None)
            state[TOKEN] = token


def _code(text: str) -> str:
    return "`" + text.replace("`", "'") + "`"


def render_link_page(st_module, settings) -> bool:
    """The page an emailed link opens, and its confirmation. True when it drew something (the caller
    stops there)."""
    state = st_module.session_state
    if state.get(DONE):
        st_module.title(settings.title)
        st_module.success("**Password changed.** Sign in with your new password.", icon=":material/check_circle:")
        st_module.button("Sign in", key="auth_reset_signin", type="primary", on_click=_set_mode, args=(st_module, None))
        return True
    token = state.get(TOKEN)
    if not token:
        return False
    st_module.title(settings.title)
    user = resets.check(settings.db_path, token)
    if user is None:
        st_module.warning(EXPIRED, icon=":material/link_off:")
        if settings.password_reset_enabled:
            st_module.button("Send a new link", key="auth_reset_again", type="primary",
                             on_click=_set_mode, args=(st_module, "request"))
        st_module.button("Back to sign in", key="auth_reset_back_expired", type="tertiary",
                         on_click=_set_mode, args=(st_module, None))
        return True
    st_module.subheader("Choose a new password")
    st_module.markdown(f"For the account {_code(user.username)}.")
    with st_module.form("auth_reset_form", clear_on_submit=True):
        new = st_module.text_input(f"New password (at least {MIN_NEW_PASSWORD_LENGTH} characters)", type="password",
                                   autocomplete="new-password", key="auth_reset_new")
        confirm = st_module.text_input("Confirm new password", type="password", autocomplete="new-password",
                                       key="auth_reset_confirm")
        submitted = st_module.form_submit_button("Set new password", type="primary")
    if submitted:
        if new != confirm:
            st_module.error("The passwords don't match.")
        else:
            try:
                updated = resets.complete(settings.db_path, token, new)
            except ValueError as exc:
                st_module.error(str(exc))
            except StoreError:
                st_module.rerun()           # the link stopped working meanwhile: show the expired notice
            else:
                from ads_auth.ui.gate import clear_protected_state, log
                log(settings, "password_reset", updated)
                clear_protected_state(st_module)    # this browser's session ends too
                state[DONE] = True
                st_module.rerun()
    st_module.caption("This signs you out on every other device.")
    return True


def render_forgot_button(st_module, settings) -> None:
    if settings.password_reset_enabled:
        st_module.button("Forgot your password?", key="auth_reset_forgot", type="tertiary",
                         on_click=_set_mode, args=(st_module, "request"))


def _send(settings, identifier: str) -> None:
    result = resets.request(settings.db_path, identifier)
    if result is None:
        return
    user, token = result
    subject, body = resets.email_text(settings.title, user, resets.link(settings.public_url, token))
    mail.send_later(settings.mail, user.email, subject, body)
    from ads_auth.ui.gate import log
    log(settings, "reset_link_sent", user)


def render_request_steps(st_module, settings) -> bool:
    """The request screen and the "Check your email" screen, under the app's title. True when one
    was drawn (the caller stops there instead of drawing the sign-in form)."""
    state = st_module.session_state
    mode = state.get(MODE)
    if mode not in ("request", "sent") or not settings.password_reset_enabled:
        return False
    if mode == "sent":
        st_module.success(CHECK_EMAIL, icon=":material/mail:")
        st_module.caption("Nothing arrived after a few minutes? Check your spam folder, or ask your administrator "
                          "to reset your password. Accounts without an email address can't be reset from here.")
        st_module.button("Back to sign in", key="auth_reset_back_sent", type="tertiary",
                         on_click=_set_mode, args=(st_module, None))
        return True
    st_module.subheader("Reset your password")
    st_module.write("Enter your username or the email address on your account. If it matches an account, "
                    "we'll email you a link to choose a new password.")
    with st_module.form("auth_reset_request_form"):
        identifier = st_module.text_input("Username or email", key="auth_reset_who", autocomplete="username")
        submitted = st_module.form_submit_button("Send reset link", type="primary")
    if submitted and (identifier or "").strip():
        _send(settings, identifier)
        state[MODE] = "sent"
        st_module.rerun()
    st_module.button("Back to sign in", key="auth_reset_back", type="tertiary",
                     on_click=_set_mode, args=(st_module, None))
    return True
