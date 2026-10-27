"""Password-change forms."""
from __future__ import annotations

from ads_auth.hashing import MIN_NEW_PASSWORD_LENGTH
from ads_auth.models import User
from ads_auth.store import StoreError, UserStore


def _change(st_module, settings, user: User, current: str, new: str, confirm: str, *, rerun_follows: bool) -> bool:
    if new != confirm:
        st_module.error("New passwords do not match.")
        return False
    try:
        new_version = UserStore(settings.db_path).change_password(user.id, current, new)
    except (StoreError, ValueError) as exc:
        st_module.error(str(exc))
        return False
    from ads_auth.ui.gate import SESSION_VERSION, remember_session
    st_module.session_state[SESSION_VERSION] = new_version   # keep this session valid
    remember_session(st_module, settings, now=not rerun_follows)   # and the cookie with it
    return True


def render_change_form(st_module, settings, user: User) -> None:
    with st_module.form("change_password_form", clear_on_submit=True):
        current = st_module.text_input("Current password", type="password", autocomplete="current-password")
        new = st_module.text_input(f"New password (at least {MIN_NEW_PASSWORD_LENGTH} characters)", type="password", autocomplete="new-password")
        confirm = st_module.text_input("Confirm new password", type="password", autocomplete="new-password")
        if st_module.form_submit_button("Change password") and user.id != 0:
            if _change(st_module, settings, user, current, new, confirm, rerun_follows=False):
                st_module.success("Password changed.")


def render_forced_change(st_module, settings, user: User) -> None:
    st_module.title(settings.title)
    st_module.warning("You signed in with a temporary password. Choose a new one to continue.")
    with st_module.form("forced_change_form", clear_on_submit=True):
        current = st_module.text_input("Temporary password", type="password", autocomplete="current-password")
        new = st_module.text_input(f"New password (at least {MIN_NEW_PASSWORD_LENGTH} characters)", type="password", autocomplete="new-password")
        confirm = st_module.text_input("Confirm new password", type="password", autocomplete="new-password")
        if st_module.form_submit_button("Set password"):
            if _change(st_module, settings, user, current, new, confirm, rerun_follows=True):
                st_module.success("Password set. Loading the app…")
                st_module.rerun()
