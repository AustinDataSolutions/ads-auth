"""The sidebar's Manage users section, for administrators.

Four tasks: reset a password, add an account, change a role, and delete an account (with
"Deactivate instead" in the confirmation, and Reactivate for a deactivated account). Passwords are
never typed here: a reset, a new account and a reactivation each get a random temporary password,
shown once until the administrator clicks Done, which the person must change at their next sign-in.
Every change is recorded in the admin log through `ads_auth.admin`.

The administrator's own account is not listed: their own password is changed with the Change
password form, and changing their own role or turning off their own account would end the session
they are using. Since the administrator using the section is an active admin, the store's "last
active admin" refusals cannot be reached from here; they still guard the command line.
"""
from __future__ import annotations

from ads_auth import admin, policy
from ads_auth.db import from_iso
from ads_auth.models import ROLES, User
from ads_auth.store import StoreError, UserStore
from ads_auth.usage.reports import DISPLAY_TZ

ISSUED = "auth_admin_issued"        # (full name, headline, temporary password) until Done is clicked
FLASH = "auth_admin_flash"          # one confirmation shown as a toast on the next run
ACTION = "auth_admin_action"

ROLE_LABEL = {"user": "User", "manager": "Manager", "admin": "Admin"}
ROLE_HELP = {"user": "Uses the app.", "manager": "Also sees Usage statistics.", "admin": "Also manages users."}

RESET, ADD, ROLE, DELETE = "Reset password", "Add user", "Change role", "Delete user"
TASKS = {
    RESET: ":material/key: " + RESET,
    ADD: ":material/person_add: " + ADD,
    ROLE: ":material/badge: " + ROLE,
    DELETE: ":material/delete: " + DELETE,
}


def _day(iso: str | None) -> str:
    moment = from_iso(iso).astimezone(DISPLAY_TZ)
    return f"{moment:%b} {moment.day}, {moment.year}"


def _name(user: User) -> str:
    return user.full_name or user.username


def _status(store: UserStore, user: User) -> str:
    bits = [ROLE_LABEL.get(user.role, user.role)]
    bits.append(f"last signed in {_day(user.last_login_utc)}" if user.last_login_utc else "never signed in")
    if not user.is_active:
        bits.append(":gray[deactivated]")
    if store.is_locked(user.username):
        bits.append(":red[locked out]")
    if user.must_change_password and user.temp_password_expires_utc:
        if policy.temp_password_expired(user):
            bits.append(":orange[temporary password expired]")
        else:
            bits.append(f":orange[temporary password until {_day(user.temp_password_expires_utc)}]")
    return " · ".join(bits)


def _pick(st_module, store: UserStore, me: User, task: str) -> User | None:
    users = {u.username: u for u in store.list_users() if u.username != me.username}

    def label(username: str) -> str:
        u = users[username]
        name = f"{u.full_name} · {u.username}" if u.full_name else u.username
        return name if u.is_active else f"{name} (deactivated)"

    names = sorted(users, key=lambda n: (_name(users[n]).casefold(), n))
    chosen = st_module.selectbox("User", names, index=None, format_func=label, placeholder="Choose a user",
                                 key=f"auth_admin_user_{task}", help="Your own account isn't listed here.")
    if chosen is None or chosen not in users:
        return None
    user = users[chosen]
    st_module.caption(_status(store, user))
    return user


def _issue(st_module, user_name: str, headline: str, temp_password: str) -> None:
    st_module.session_state[ISSUED] = (user_name, headline, temp_password)


def _done(st_module) -> None:
    st_module.session_state.pop(ISSUED, None)


def _show_issued(st_module) -> bool:
    issued = st_module.session_state.get(ISSUED)
    if not issued:
        return False
    user_name, headline, temp_password = issued
    st_module.success(f"{headline} for **{user_name}**.")
    st_module.code(temp_password, language=None, wrap_lines=True)
    st_module.caption("Shown once. Send it to them privately. They choose their own password at their next "
                      "sign-in. It stops working after 90 days.")
    st_module.button("Done", key="auth_admin_done", on_click=_done, args=(st_module,), width="stretch")
    return True


def _flash(st_module, message: str) -> None:
    st_module.session_state[FLASH] = message


def _reactivate_offer(st_module, settings, me: User, user: User) -> None:
    st_module.info("This account is deactivated, so they can't sign in.", icon=":material/block:")
    if st_module.button("Reactivate", key="auth_admin_reactivate", type="primary", width="stretch",
                        help="Turns the account back on with a new temporary password."):
        try:
            temp_password = admin.reactivate(settings.db_path, me, user.username)
        except (StoreError, ValueError) as exc:
            st_module.error(str(exc))
            return
        _issue(st_module, _name(user), "Reactivated. Temporary password", temp_password)
        st_module.rerun()


# ---- the four tasks ------------------------------------------------------------------------------

def _reset(st_module, settings, store: UserStore, me: User) -> None:
    user = _pick(st_module, store, me, "reset")
    if user is None:
        return
    if not user.is_active:
        _reactivate_offer(st_module, settings, me, user)
        return
    st_module.caption("Signs them out everywhere and clears any lockout.")
    if st_module.button("Reset password", key="auth_admin_reset", type="primary", width="stretch"):
        try:
            temp_password = admin.reset_password(settings.db_path, me, user.username)
        except (StoreError, ValueError) as exc:
            st_module.error(str(exc))
            return
        _issue(st_module, _name(user), "Temporary password", temp_password)
        st_module.rerun()


def _add(st_module, settings, me: User) -> None:
    with st_module.form("auth_admin_add_form", border=False):
        full_name = st_module.text_input("Name", key="auth_admin_add_name")
        username = st_module.text_input("Username", key="auth_admin_add_username",
                                        help="No spaces. Not case-sensitive. An email address works.")
        role = st_module.selectbox("Role", ROLES, key="auth_admin_add_role", format_func=ROLE_LABEL.get,
                                   help="User: " + ROLE_HELP["user"] + " Manager: " + ROLE_HELP["manager"]
                                        + " Admin: " + ROLE_HELP["admin"])
        submitted = st_module.form_submit_button("Create account", type="primary", width="stretch")
    if not submitted:
        return
    try:
        user, temp_password = admin.create_account(settings.db_path, me, username, full_name, role)
    except (StoreError, ValueError) as exc:
        st_module.error(str(exc))
        return
    _issue(st_module, _name(user), "Account created. Temporary password", temp_password)
    st_module.rerun()


def _role(st_module, settings, store: UserStore, me: User) -> None:
    user = _pick(st_module, store, me, "role")
    if user is None:
        return
    if not user.is_active:
        _reactivate_offer(st_module, settings, me, user)
    new_role = st_module.radio("Role", ROLES, index=ROLES.index(user.role), key=f"auth_admin_role_{user.username}",
                               format_func=ROLE_LABEL.get, captions=[ROLE_HELP[r] for r in ROLES])
    st_module.caption("They're signed out so the new role takes effect.")
    if st_module.button("Save role", key="auth_admin_role_save", type="primary", width="stretch",
                        disabled=new_role == user.role):
        try:
            admin.change_role(settings.db_path, me, user.username, new_role)
        except (StoreError, ValueError) as exc:
            st_module.error(str(exc))
            return
        _flash(st_module, f"{_name(user)} is now {ROLE_LABEL[new_role].lower()}.")
        st_module.rerun()


def _confirm_delete_body(st_module, settings, me: User, username: str) -> None:
    store = UserStore(settings.db_path)
    user = store.get_by_username(username)
    if user is None:
        st_module.write("This account no longer exists.")
        return
    st_module.markdown(f"**{_name(user)}** (`{user.username}`) will no longer be able to sign in, and the account "
                       "cannot be restored. Their past usage stays in Usage statistics under this username.")
    if user.is_active:
        st_module.markdown("To keep the account but stop them signing in, **deactivate** it instead. "
                           "You can reactivate it later.")
    typed = st_module.text_input(f"Type `{user.username}` to delete", key="auth_admin_delete_typed")
    cols = st_module.columns(3 if user.is_active else 2)
    if cols[0].button("Cancel", key="auth_admin_delete_cancel", width="stretch"):
        st_module.rerun()
    if user.is_active and cols[1].button("Deactivate instead", key="auth_admin_deactivate", width="stretch"):
        try:
            admin.deactivate(settings.db_path, me, user.username)
        except (StoreError, ValueError) as exc:
            st_module.error(str(exc))
            return
        _flash(st_module, f"Deactivated `{user.username}`.")
        st_module.rerun()
    if cols[-1].button("Delete permanently", key="auth_admin_delete_go", type="primary", width="stretch",
                       disabled=(typed or "").strip().casefold() != user.username):
        try:
            admin.delete_account(settings.db_path, me, user.username)
        except (StoreError, ValueError) as exc:
            st_module.error(str(exc))
            return
        st_module.session_state.pop("auth_admin_user_delete", None)
        _flash(st_module, f"Deleted `{user.username}`.")
        st_module.rerun()


def _delete(st_module, settings, store: UserStore, me: User) -> None:
    user = _pick(st_module, store, me, "delete")
    if user is None:
        return
    if not user.is_active:
        _reactivate_offer(st_module, settings, me, user)
    if st_module.button("Delete account…", key="auth_admin_delete", width="stretch"):
        st_module.session_state.pop("auth_admin_delete_typed", None)
        dialog = st_module.dialog("Delete account permanently?")(_confirm_delete_body)
        dialog(st_module, settings, me, user.username)


def render_manage_users(st_module, settings, me: User) -> None:
    """The collapsed Manage users expander. Call inside `with st.sidebar:`; draws nothing for
    anyone but a signed-in administrator."""
    if not me.has_role("admin") or me.id == 0:
        return
    message = st_module.session_state.pop(FLASH, None)
    if message:
        st_module.toast(message, icon=":material/check_circle:")
    with st_module.expander("Manage users", icon=":material/group:", expanded=False):
        if _show_issued(st_module):
            return
        store = UserStore(settings.db_path)
        task = st_module.radio("Task", list(TASKS), key=ACTION, format_func=TASKS.get, label_visibility="collapsed")
        if task == RESET:
            _reset(st_module, settings, store, me)
        elif task == ADD:
            _add(st_module, settings, me)
        elif task == ROLE:
            _role(st_module, settings, store, me)
        elif task == DELETE:
            _delete(st_module, settings, store, me)
