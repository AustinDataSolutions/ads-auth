# ads-auth

Shared accounts, sessions, usage events, statistics page, data-handling (About) page,
housekeeping, startup preflight, and operator CLI for Austin Data Solutions (ADS) Streamlit apps.

Import name `ads_auth`, distribution name `ads-auth`. Python 3.12 or newer. Runtime dependencies:
Werkzeug 3.1 (password hashes in a format the other ADS apps share), tzdata, Streamlit 1.42 or newer,
pandas. The package is tested against Streamlit 1.45.1, 1.50.0, and the current release.

## What is in it

| Module | Purpose | 
|---|---|
| `ads_auth.settings` | `AppSettings`, the contract an app supplies (below) |
| `ads_auth.db` | SQLite connections and versioned, idempotent migrations |
| `ads_auth.models`, `hashing`, `policy`, `store` | users, roles, Werkzeug hashes, lockout, 8-hour sessions, temporary passwords, import from another ADS app's `users.db` |
| `ads_auth.usage.events`, `usage.reports` | usage events (timestamp, account, event type only), 13-month retention, weekly and monthly active users, the four-column `ts_utc,app,username,event` export |
| `ads_auth.ui.gate`, `ui.account` | `require_login`, `render_sidebar`, logout, password-change forms |
| `ads_auth.ui.reports`, `ui.about` | the Usage statistics page (managers and admins) and the About page that renders the app's disclosure file |
| `ads_auth.maintenance` | daily backup (seven kept), event pruning, a daemon loop, and a hook for one app-specific task |
| `ads_auth.preflight` | startup checks: settings valid, auth not disabled on Render, data dir writable, migrations applied, `serving.lock` written |
| `ads_auth.cli` | `create-user`, `set-password`, `set-role`, `set-name`, `deactivate-user`, `reactivate-user`, `delete-user`, `unlock-user`, `list-users`, `import-users`, `backup-db`, `restore-db`, `prune-events`, `export-events`, plus app-supplied commands |

Everything outside `ads_auth.ui` imports no Streamlit and does no I/O at import time.

## What an app supplies

One `AppSettings` object. Instantiate it directly, or subclass it as a keyword-only frozen dataclass
to add the app's own fields.

```python
from dataclasses import dataclass
from pathlib import Path
from ads_auth import AppSettings, auth_mode_from_environ, exclude_users_from_environ

@dataclass(frozen=True, kw_only=True)
class Settings(AppSettings):
    app_name: str = "example-app"                # `app` column in exports; CSV download prefix
    title: str = "Example App"                   # sign-in screen title
    disclosure_path: Path = REPO_ROOT / "DATA_HANDLING.md"
    usage_exclude_users: tuple[str, ...] = ("ops-test",)
    api_key: str = ""                            # the app's own fields

settings = Settings(data_dir=Path(environ["DATA_DIR"]), auth_mode=auth_mode_from_environ(environ),
                    is_render=environ.get("RENDER", "").lower() == "true")
```

The app also keeps, in its own repository:

- `DATA_HANDLING.md` (or whatever `disclosure_path` points at): the disclosure its users read on the About page.
- `manage.py`: a wrapper over `ads_auth.cli.main(load_settings=..., commands=...)`.
- `scripts/preflight.py`: a wrapper over `ads_auth.preflight.run(lambda: load_settings(...))`.
- Its router: register the package pages under the names the package links to (`app`, `about`, `reports`).

```python
# app.py
from ads_auth import db
from ads_auth.ui import about, reports
from ads_auth.ui.gate import log, render_sidebar, require_login

def main_page():
    user = require_login(st, settings)
    render_sidebar(st, settings, user, show_reports=user.has_role("manager"))
    ...
    log(settings, "search", user)

def run():
    db.migrate(settings.db_path)
    settings.pages["app"] = st.Page(main_page, title=settings.title, url_path="app", default=True)
    settings.pages["about"] = st.Page(lambda: about.render(st, settings), title="About this app and data handling", url_path="about")
    settings.pages["reports"] = st.Page(reports_page, title="Usage statistics", url_path="reports")
    st.navigation(list(settings.pages.values()), position="hidden").run()
```

`tests/demo_app.py` is the smallest complete consumer and is what the package's gate tests run.

Housekeeping: `maintenance.start_background(data_dir, db_path, extra=task)` runs the generic pass
(backup if due, prune events) and then `task(now)`, whose returned mapping is merged into the pass
result, for example a check that removes expired app data on schedule.

CLI extensions: pass `commands=[Command(name, handler, help, arguments)]` to `cli.main`; the handler
receives the `Cli` instance (`settings`, `out`, `store`, `refuse_if_serving(force)`) and the parsed
arguments.

Event vocabulary in 1.0.0: `login`, `login_failed`, `logout`, `search`, `upload`, `library_refresh`,
`download`. Event rows never hold search terms, file names, or content. Activity (for active-user counts) is everything but `login_failed` and `logout`.

## Disclosure marker

Each consuming app shows its users a data-handling document. This package decides much of what that
document describes: which tables exist, what each event row holds, how long events and backups are
kept, where files live. So:

- Any release that changes what is **stored, logged, transmitted, or retained** carries the line
  `Disclosure: affected` in its CHANGELOG entry, with a sentence on what changed.
- An app that bumps its pin to such a release updates its own disclosure document in the same commit.
  The app's CLAUDE.md should say so.
- Releases without the marker need no disclosure change.

## Layout and development

`src` layout: the importable package is `src/ads_auth`, so tests always run against the installed
(editable) copy, never a stray checkout on `sys.path`.

```bash
python3.13 -m venv venv
venv/bin/pip install -e '.[test]'
venv/bin/python -m pytest
```

## Maintenance workflow

1. Edit in this repository. Keep framework-free modules free of Streamlit and import-time I/O.
2. Run `venv/bin/python -m pytest`. For a change to the gate or pages, also run the suite with the
   Streamlit versions the consuming apps pin.
3. Add a CHANGELOG entry. Decide whether it is `Disclosure: affected`.
4. Bump `__version__` in `src/ads_auth/__init__.py`, commit, tag `vX.Y.Z`, push the tag.
5. In each app, bump the pin in `requirements.txt` to the new tag, reinstall, run the app's own suite,
   update its disclosure if the release is marked, commit, deploy.

Apps pin a tag, never a branch, so a deploy never picks up untested package changes.
