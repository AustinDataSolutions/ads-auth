"""The contract a consuming app supplies: one small, frozen settings object.

Apps either instantiate `AppSettings` directly or subclass it (as a keyword-only frozen
dataclass) to add their own fields. Package code only reads the attributes defined here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

AUTH_MODES = ("required", "disabled")
DB_FILENAME = "app.db"


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
    pages:               registry filled by the app's router (`name -> st.Page`) so package pages can link
                         to `app`, `about`, and `reports` without importing the app
    """

    app_name: str
    title: str
    data_dir: Path
    disclosure_path: Path
    auth_mode: str = "required"
    is_render: bool = False
    usage_exclude_users: tuple[str, ...] = ()
    pages: dict[str, object] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.app_name or not self.app_name.strip():
            raise ValueError("app_name is required.")
        if self.auth_mode not in AUTH_MODES:
            raise ValueError("AUTH_MODE must be 'required' or 'disabled'.")
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "disclosure_path", Path(self.disclosure_path))

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def auth_disabled(self) -> bool:
        return self.auth_mode == "disabled"
