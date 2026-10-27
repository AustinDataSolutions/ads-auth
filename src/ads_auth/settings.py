"""The contract a consuming app supplies: one small, frozen settings object.

Apps either instantiate `AppSettings` directly or subclass it (as a keyword-only frozen
dataclass) to add their own fields. Package code only reads the attributes defined here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ads_auth.tokens import MIN_SECRET_LENGTH
from ads_auth.usage.events import CORE_ACTIVITY_EVENTS, CORE_EVENTS

AUTH_MODES = ("required", "disabled")
COOKIE_SECRET_VARIABLES = ("ADS_AUTH_COOKIE_SECRET", "STREAMLIT_SERVER_COOKIE_SECRET")
DB_FILENAME = "app.db"
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def auth_mode_from_environ(environ) -> str:
    """`AUTH_MODE` from an environment mapping: `required` (default) or `disabled` (local development only)."""
    mode = (environ.get("AUTH_MODE", "") or "required").strip().lower()
    if mode not in AUTH_MODES:
        raise ValueError("AUTH_MODE must be 'required' or 'disabled'.")
    return mode


def exclude_users_from_environ(environ, default: str = "") -> tuple[str, ...]:
    """`USAGE_EXCLUDE_USERS`: comma-separated accounts left out of the statistics page by default."""
    raw = environ.get("USAGE_EXCLUDE_USERS", default) or ""
    return tuple(name.strip().casefold() for name in raw.split(",") if name.strip())


def cookie_secret_from_environ(environ) -> str:
    """The secret that signs the sign-in cookie: `ADS_AUTH_COOKIE_SECRET`, else Streamlit's own
    `STREAMLIT_SERVER_COOKIE_SECRET` (already set on the hosted services), else empty (feature off)."""
    for name in COOKIE_SECRET_VARIABLES:
        value = (environ.get(name, "") or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True, kw_only=True)
class AppSettings:
    """What the package needs to know about the app it serves.

    app_name:            slug used in the `app` column of event exports and as the CSV file prefix,
                         e.g. `example-app`
    title:               shown on the sign-in and forced-password-change screens
    data_dir:            where `app.db` and `backups/` live (the Render disk in production)
    disclosure_path:     the app's own data-handling document, rendered verbatim by the About page
    auth_mode:           `required` or `disabled`
    is_render:           True when running on Render; `disabled` is refused there
    usage_exclude_users: accounts the statistics page excludes by default
    events:              the app's own usage event names (lowercase slugs such as `upload`, `process`), in the
                         order the statistics page and CSV exports show them. The package owns the core names
                         `login`, `login_failed`, and `logout`; an app may not redeclare them. Every name an
                         app declares must appear in its disclosure document.
    activity_events:     the subset of `events` that counts toward active users; None (default) means all of
                         them. `login` always counts; `login_failed` and `logout` never do.
    definitions:         optional app-specific wording for the caption of the statistics page (Markdown).
                         Empty means the package's generic caption.
    pages:               registry filled by the app's router (`name -> st.Page`) so package pages can link
                         to `app`, `about`, and `reports` without importing the app
    cookie_secret:       signs the "stay signed in" cookie that lets a browser reload keep the session.
                         Empty (the default) turns the cookie off: a reload then shows the sign-in screen,
                         as before. At least 32 characters when set; `cookie_secret_from_environ` reads it.
    """

    app_name: str
    title: str
    data_dir: Path
    disclosure_path: Path
    auth_mode: str = "required"
    is_render: bool = False
    usage_exclude_users: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    activity_events: tuple[str, ...] | None = None
    definitions: str = ""
    pages: dict[str, object] = field(default_factory=dict, compare=False, repr=False)
    cookie_secret: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.app_name or not self.app_name.strip():
            raise ValueError("app_name is required.")
        if self.cookie_secret and len(self.cookie_secret) < MIN_SECRET_LENGTH:
            raise ValueError(f"cookie_secret must be at least {MIN_SECRET_LENGTH} characters (or empty to turn the sign-in cookie off).")
        if self.auth_mode not in AUTH_MODES:
            raise ValueError("AUTH_MODE must be 'required' or 'disabled'.")
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "disclosure_path", Path(self.disclosure_path))
        events = tuple(self.events)
        for name in events:
            if not isinstance(name, str) or not EVENT_NAME.match(name):
                raise ValueError(f"Event names must be lowercase slugs (a-z, 0-9, _): {name!r}")
            if name in CORE_EVENTS:
                raise ValueError(f"{name!r} is a core event owned by ads_auth; do not declare it.")
        if len(set(events)) != len(events):
            raise ValueError("Event names must be unique.")
        activity = events if self.activity_events is None else tuple(self.activity_events)
        unknown = [name for name in activity if name not in events]
        if unknown:
            raise ValueError(f"activity_events must be a subset of events; unknown: {unknown}")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "activity_events", activity)

    @property
    def all_events(self) -> frozenset[str]:
        """Every name `log_event` accepts for this app: the core names plus the app's own."""
        return CORE_EVENTS | frozenset(self.events)

    @property
    def all_activity_events(self) -> frozenset[str]:
        """Every name that makes an account active in a period: `login` plus the app's activity events."""
        return CORE_ACTIVITY_EVENTS | frozenset(self.activity_events)

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def auth_disabled(self) -> bool:
        return self.auth_mode == "disabled"

    @property
    def cookie_enabled(self) -> bool:
        """True when sign-ins are remembered across a browser reload."""
        return bool(self.cookie_secret)
