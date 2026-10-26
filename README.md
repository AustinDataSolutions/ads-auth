# ads-auth

Austin Data Solutions (ADS) builds small Streamlit apps for its clients. Each one needs the same
basic plumbing: people sign in with their own account, a session ends after a while, the app keeps
a simple record of how often it is used, and managers can see those numbers and read a plain
description of what the app stores. This package provides all of that once, so no app has to build
and maintain its own copy.

The package is called `ads-auth` when you install it and `ads_auth` when you import it. It needs
Python 3.12 or newer. It depends on Werkzeug (for password hashes in the format the ADS apps share),
tzdata (for time zones), Streamlit 1.42 or newer, and pandas. It is tested against Streamlit 1.45.1,
Streamlit 1.50.0, and the current release.

## What the package does

- **Accounts and sign-in.** Each person has a username and a password. Passwords are stored as
  hashes, never as plain text. Five failed attempts within fifteen minutes lock the account until
  the window passes. A signed-in session lasts eight hours. Every account has a role, which is
  `user`, `manager`, or `admin`. An administrator can give someone a temporary password, which must
  be changed at first sign-in. Accounts can be imported from another app's database, whether
  that app uses this package or keeps its accounts in a Flask-style `user` table.
- **Usage records.** The app writes one row each time something worth counting happens: a sign-in,
  a failed sign-in, a sign-out, and whichever of its own actions the app chooses to record, such as
  an upload. A row holds only the time, the account name, and the kind of event. It never holds
  what the person typed, uploaded, or searched for. Rows older than thirteen months are deleted
  automatically.
- **A Usage statistics page.** Managers and administrators can see how many people were active in
  each week and each month, totals for each person, and can download the figures as CSV files. One
  of those files has the same four columns in every app, so exports from different apps can be
  combined into one spreadsheet.
- **An About page.** The app points the package at its own data-handling document, and the About
  page shows that document to users exactly as written.
- **Housekeeping.** Once a day the package makes a backup of the database, keeping the seven most
  recent copies, and removes usage rows that have passed the retention period. An app can add one
  housekeeping task of its own to the same daily pass.
- **Startup checks.** Before the app begins serving, the package confirms that its settings are
  valid, that sign-in has not been switched off on a hosted server, that the data folder can be
  written to, and that the database is up to date.
- **A command line for operators.** There are commands to create an account, set a password, change
  a role or a display name, deactivate or reactivate an account, delete an account, unlock an
  account, list accounts, import accounts, back up or restore the database, remove old usage rows,
  and export usage rows. An app can add commands of its own.

Everything except the Streamlit screens is plain Python. Those modules import no Streamlit and
read or write no files when they are imported, so they can be tested without a running app.

## Where things live

| Module | What it holds |
|---|---|
| `ads_auth.settings` | `AppSettings`, the object an app fills in to describe itself. The next section explains it. |
| `ads_auth.db` | Opening the SQLite database and applying schema changes. The changes are numbered and safe to apply more than once. |
| `ads_auth.models`, `hashing`, `policy`, `store` | Accounts, roles, password hashing, the lockout and session rules, temporary passwords, and importing accounts from another app's database. |
| `ads_auth.usage.events`, `usage.reports` | Writing usage rows, the thirteen-month retention, the weekly and monthly active-user figures, and the CSV exports. |
| `ads_auth.ui.gate`, `ui.account` | The sign-in screen, the sidebar with the sign-out button, and the change-password forms. |
| `ads_auth.ui.ads_apps` | The shared links to other ADS Streamlit tools, for placement after an app's own sidebar items. |
| `ads_auth.ui.reports`, `ui.about` | The Usage statistics page and the About page. |
| `ads_auth.maintenance` | The daily backup, the removal of old usage rows, the loop that runs them in the background, and the hook for an app's own task. |
| `ads_auth.preflight` | The startup checks. When they pass, a `serving.lock` file is written so operator commands know the app is running. |
| `ads_auth.cli` | The operator commands listed above, plus any the app adds. |

## What an app has to provide

The app describes itself to the package in one settings object called `AppSettings`. It holds the
facts the package cannot know on its own: the app's short name, the title to show on the sign-in
screen, where the data folder is, where the data-handling document is, whether sign-in is required,
and which actions the app records.

The simplest way to provide it is to create an `AppSettings` directly with those values. Most apps
instead define their own settings class that starts from `AppSettings` and adds the app's own
fields, such as an API key, so that all of the app's configuration lives in one object. The example
below does that. The decorator line above the class makes the object read-only once created and
requires every value to be passed by name; copy it as it is.

```python
from dataclasses import dataclass
from pathlib import Path
from ads_auth import AppSettings, auth_mode_from_environ, exclude_users_from_environ

@dataclass(frozen=True, kw_only=True)
class Settings(AppSettings):
    app_name: str = "example-app"
    title: str = "Example App"
    disclosure_path: Path = REPO_ROOT / "DATA_HANDLING.md"
    usage_exclude_users: tuple[str, ...] = ("ops-test",)
    events: tuple[str, ...] = ("upload", "process", "download")
    api_key: str = ""                            # one of the app's own fields

settings = Settings(data_dir=Path(environ["DATA_DIR"]), auth_mode=auth_mode_from_environ(environ),
                    is_render=environ.get("RENDER", "").lower() == "true")
```

The fields mean the following.

- `app_name` is a short, lowercase name for the app. It appears in the `app` column of the usage
  export and at the start of the CSV file names, so exports from different apps can be told apart.
- `title` is the heading on the sign-in screen.
- `data_dir` is the folder that holds the database and the backups. On a hosted server this is the
  persistent disk.
- `disclosure_path` is the app's data-handling document, which the About page shows as written.
- `auth_mode` is `required` normally, or `disabled` for local development only. The package refuses
  to run with sign-in disabled on a hosted server.
- `is_render` is true when the app is running on Render, the hosting service the ADS apps use.
- `usage_exclude_users` lists accounts, such as test accounts, that the Usage statistics page leaves
  out unless a manager puts them back.
- `events` lists the actions the app records. The section on recording what people do explains how
  to choose them. Two related fields, `activity_events` and `definitions`, are explained there too.
- `pages` is filled in by the app's router, as shown below, so the package's pages can link to each
  other without knowing anything about the app.

The helper `auth_mode_from_environ` reads the `AUTH_MODE` environment variable and checks it, and
`exclude_users_from_environ` reads a comma-separated `USAGE_EXCLUDE_USERS` variable.

Besides the settings object, the app keeps a few things in its own repository:

- Its data-handling document (`DATA_HANDLING.md`, or whatever `disclosure_path` points at). This is
  what users read on the About page, so it is written for them.
- `manage.py`, a short script that calls `ads_auth.cli.main` with the app's settings, so operators
  can run the account and backup commands.
- `scripts/preflight.py`, a short script that calls `ads_auth.preflight.run` before the app starts.
- Its router, which registers the package's pages under the names the package expects: `app` for
  the app's own main page, `about` for the About page, and `reports` for the Usage statistics page.

```python
# app.py
from ads_auth import db
from ads_auth.ui import about, reports
from ads_auth.ui.gate import confirm_navigation, log, render_sidebar, require_login

def main_page():
    user = require_login(st, settings)          # shows the sign-in screen until someone signs in
    render_sidebar(st, settings, user, show_reports=user.has_role("manager"),
                   confirm_leave=lambda: bool(st.session_state.get("search")))   # optional: see below
    confirm_navigation(st, settings, "Leaving this page clears the search box.")  # pairs with confirm_leave
    render_other_ads_tools(st)                  # shared ADS app links, below this app's sidebar items
    ...
    log(settings, "upload", user)               # record that this person uploaded something

def run():
    db.migrate(settings.db_path)
    settings.pages["app"] = st.Page(main_page, title=settings.title, url_path="app", default=True)
    settings.pages["about"] = st.Page(lambda: about.render(st, settings), title="About this app and data handling", url_path="about")
    settings.pages["reports"] = st.Page(reports_page, title="Usage statistics", url_path="reports")
    st.navigation(list(settings.pages.values()), position="hidden").run()
```

`tests/demo_app.py` is the smallest complete app that uses the package. The package's own sign-in
tests run against it, and it is a good starting point for a new app.

Streamlit forgets a screen's widgets (uploads, edited text) when the person opens another page. An
app whose screen holds such work can pass `confirm_leave`, a function that returns True while there
is something to lose. While it does, the sidebar's Usage statistics and About links become buttons
that only note the chosen page, and `confirm_navigation(st, settings, message)`, called right after
`render_sidebar`, opens a dialog: the app's message, "Do you still wish to proceed?", and the buttons
"Go back" and "Proceed". Proceed switches to the chosen page; Go back, or closing the dialog, stays.

To add a housekeeping task, pass it to `maintenance.start_background(data_dir, db_path, extra=task)`.
The package runs its daily pass (back up if due, remove old usage rows) and then calls `task(now)`.
Whatever mapping the task returns is added to the pass's result, which is useful for a check that,
for example, removes expired app data on a schedule.

To add operator commands, pass `commands=[Command(name, handler, help, arguments)]` to `cli.main`.
The handler receives the `Cli` object, which gives it the settings, the output function, the account
store, and `refuse_if_serving(force)`, a guard that stops a command from running while the app is
serving unless the operator insists.

## Recording what people do

The package itself records three kinds of event: `login`, `login_failed`, and `logout`. Everything
else is up to the app, which lists its own event names in the `events` field of its settings. A name
is a short lowercase word or phrase with underscores, such as `upload` or `run_audit`, and the order
of the list is the order the Usage statistics page and the CSV exports show the columns. When the
app calls `log(settings, name, user)`, the package accepts one of its own three names or a name the
app declared, and rejects anything else. A usage row holds the time, the account name, and the event
name only. It never holds search terms, file names, or content.

A person counts as active in a week or a month if they signed in or did any of the actions the app
listed. If some recorded actions should not count as activity, the app can list the ones that do in
`activity_events`; otherwise all of them count. A failed sign-in or a sign-out never counts.

The Usage statistics page shows a Logins column and then one column for each declared event. Above
the tables it shows a short caption explaining what the numbers mean. The package's caption is
generic. An app that wants to say more, for example how repeated actions are counted, can set
`definitions` to its own wording, which replaces the generic caption.

Choosing names takes some judgement. A new name is for a different action, a different thing
produced, or a different cost. One call to a language model, for instance, is one event, recorded
whenever the call happens and whatever triggered it. Doing the same action again, or triggering it
in a different way, is not a new name. Where two apps record the same concept, such as an upload or
a download, they should use the same name so their exports line up. Configuration choices, such as
which provider or model an app uses, are not events. Every name an app declares is something its
data-handling document must mention, so adding one is a change to that document as well.

## When a release changes what is stored

Each app shows its users a data-handling document. Much of what that document describes is decided
by this package: which tables exist, what a usage row holds, how long rows and backups are kept, and
where files live. So the package keeps the apps honest in this way:

- Any release that changes what is stored, logged, sent elsewhere, or kept carries the line
  `Disclosure: affected` in its CHANGELOG entry, with a sentence saying what changed.
- An app that moves to such a release updates its own data-handling document in the same commit.
  The app's instructions for maintainers should say so.
- A release without that line needs no change to the document.

## Working on the package

The importable code is under `src/ads_auth`. Keeping it in a `src` folder means tests always run
against the installed copy of the package, never against a stray checkout that happens to be on the
path.

```bash
python3.13 -m venv venv
venv/bin/pip install -e '.[test]'
venv/bin/python -m pytest
```

To make a change and release it:

1. Edit the code here. Keep the non-Streamlit modules free of Streamlit and of file access at
   import time.
2. Run the tests with `venv/bin/python -m pytest`. For a change to the sign-in screen or the pages,
   also run them with the Streamlit versions the apps use.
3. Add a CHANGELOG entry and decide whether the release changes what is stored. If it does, add the
   `Disclosure: affected` line.
4. Raise `__version__` in `src/ads_auth/__init__.py`, commit, tag the commit `vX.Y.Z`, and push the
   tag.
5. In each app, change the pinned tag in `requirements.txt`, reinstall, run the app's own tests,
   update its data-handling document if the release calls for it, commit, and deploy.

Apps always pin a tag, never a branch, so a deploy can never pick up a change that has not been
tested.
