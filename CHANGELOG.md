# Changelog

Releases are tagged `vX.Y.Z`. A release whose entry carries the line **Disclosure: affected** changes
what consuming apps store, log, transmit, or retain; each app must update its own data-handling
document (`DATA_HANDLING.md` or equivalent) in the same commit that bumps to that release.
Entries without the marker need no disclosure change.

## v1.5.1 (2026-11-11)

Disclosure: affected. Every recorded usage event now also goes to the server log as one line
with the account name and the event type, the same two values the usage events table holds, so an
operator reading the log can see who signed in, signed out, or did one of the app's recorded
actions. A failed sign-in for a name that matches no account is logged with no name, as before.
Nothing new is stored, and the line never holds content. An app that adopts this release should
add the account name and event type to what it says the server log holds, for example:

> The server log also has one line per recorded action, with your account name and the kind of
> action, the same as the usage events table.

- `usage.events.log_event` logs `usage event: <event> by <account>` at INFO through the
  `ads_auth.usage.events` logger after writing the row. Apps that do not want the lines can set
  that logger's level to WARNING.

## v1.5.0 (2026-11-06)

Disclosure: affected. The Usage statistics page's downloads are now Excel workbooks whose second
sheet records who downloaded the file and when, alongside the app and the filters used. Nothing new
is stored in the app's database or logged; the name and time go only into the downloaded file. An
app that adopts this release should say so, for example:

> Downloads from the Usage statistics page are Excel files that also record which manager or
> administrator downloaded them and when, so a shared copy can be traced back to its source.

- The page's three downloads (Aggregates, Per user, All events) are `.xlsx` workbooks instead of
  `.csv` files. The `Data` sheet keeps the CSV's columns and machine headers; period dates are real
  dates. The `About this report` sheet lists the report, the source app (title and `app_name`), who
  generated it, when (Eastern and UTC), the date range, the excluded accounts, the events counted,
  and the ads_auth version. The All events file covers every stored event, as before; its About
  sheet and the button's help now say the page's date range does not apply to it.
- File names keep their pattern with `.xlsx` and date-stamp in Eastern time.
- Text from accounts and settings is always written as text, never as a spreadsheet formula.
- New in `usage.reports`: `xlsx_workbook(header, rows, about)`, `XLSX_MIME`, and the row builders
  `aggregate_rows`, `per_user_rows` and `combined_events_rows`, which the CSV functions now use too.
  New in `ui.reports`: `report_about(...)`. The CSV functions and `manage.py export-events` are
  unchanged.
- New dependency: `openpyxl>=3.1`.

## v1.4.0 (2026-11-05)

Disclosure: affected. The Usage statistics page now shows managers and administrators who has the
app open right now. It is read from the server's memory when the page is shown and is not stored or
logged. An app that adopts this release should say so, for example:

> Managers and administrators can also see who has the app open at the moment, so a restart doesn't
> catch anyone by surprise. That list is read from the server's memory when they look, and is not
> recorded.

- The Usage statistics page opens with who else has the app open on this server: each signed-in
  person's name, whether their page is open, working, or closed and held
  (`server.disconnectedSessionTTL`), and pages still on the sign-in screen. Loud (a warning) when
  anyone else is there, so a restart or deploy doesn't end someone's work unannounced.
  `ui.live_sessions.open_sessions(settings)` reads Streamlit's session manager and returns
  `OpenSession` rows; it returns nothing outside a running server or if Streamlit moves its
  internals.
- A Daily active users table, after the weekly one; the aggregates CSV includes its rows with
  `period_type` `day`. `usage.reports.active_users_by_period` accepts `kind="day"`.
- `AppSettings.usage_since`: the earliest day the page's date range starts on by default (the range
  is still the last twelve months, but never starts before this day). None keeps the old default.

## v1.3.3 (2026-11-02)

Disclosure: unaffected. Restores on newer Streamlit what v1.3.1 already disclosed: a session someone
left is released when its hold ends. Nothing new is stored, sent, or kept.

- `ui.reconnect.keep_sessions_on_reconnect()` also releases disconnected sessions on Streamlit
  releases that keep held sessions in Streamlit's own `TTLCache` instead of cachetools' (1.66 does).
  Before, it logged "Streamlit's session storage changed" there and held each session's files until a
  restart. `SESSION_CACHE_TYPES` lists the cache classes it handles; any other storage is left to
  Streamlit, as before.

## v1.3.2 (2026-10-30)

Disclosure: unaffected. Display only.

- The Usage statistics tables size themselves with `width="stretch"` instead of
  `use_container_width=True`, which Streamlit deprecated (removal after 2025-12-31) and warned about
  on every render.

## v1.3.1 (2026-10-29)

Disclosure: affected. A session whose connection closed is now released when Streamlit's hold on it
(`server.disconnectedSessionTTL`) ends, plus up to a minute: its uploaded files and prepared
downloads leave the server's memory then. Before, Streamlit forgot the session but kept those in
memory until the server restarted. An app that adopts this release should say how long a session
someone left is kept, for example:

> If you close the page or lose your connection, your upload and results stay in the server's
> memory for up to N minutes, so a page that reconnects can carry on, and are then discarded.

- `ui.reconnect.keep_sessions_on_reconnect()` also replaces the cache in Streamlit's
  `MemorySessionStorage` with one that shuts each session down (`AppSession.shutdown()`) when it
  expires or is evicted. It checks once a minute on the server's event loop. Requires
  `cachetools>=5.4`, whose `TTLCache.expire()` returns what it removed.

## v1.3.0 (2026-10-29)

Disclosure: unaffected. Nothing new is stored, sent, or kept: the session a reconnecting tab gets
back was already held in memory; before, the tab just could not reach it.

- `ui.reconnect.keep_sessions_on_reconnect()` lets a browser tab whose connection dropped rejoin
  its own session. Without it, a page that reconnects before the server has noticed the old
  connection is gone (a sleeping laptop behind Render's proxy, for example) gets a new, empty
  session, and the old one, upload and results included, stays in memory where nothing can reach
  it. With it, the old connection is closed and the page keeps its session; the server logs
  "A browser tab reconnected and rejoined its session." Call it once at the top of the app's
  router. It patches Streamlit's `WebsocketSessionManager.connect_session` and leaves Streamlit's
  behavior in place for any connection it does not recognise.

## v1.2.1 (2026-10-28)

Disclosure: unaffected.

- `ui.gate.restored_from_cookie(st)` says whether the sign-in cookie, rather than the sign-in form,
  signed the current session in. That covers a reload, a new tab, and a browser that reconnected
  after the server restarted or dropped its connection. Such a session starts empty, so an app can
  tell the person that earlier uploads and results are gone. Without this, a file name left in the
  page's upload box looks loaded when it is not. It is stored in session state under the new key
  `auth_restored_from_cookie`, set only by a cookie restore and cleared by a form sign-in or sign-out.

## v1.2.0 (2026-10-26)

Disclosure: affected. Each app that adopts this release and sets the cookie secret must add the
following to its data-handling document (and refresh its "Last updated" date), in its own words
if it prefers, keeping the facts:

> When you sign in, the app places one cookie in your browser so that reloading the page or
> opening the app in a new tab within the next eight hours does not sign you out. The cookie
> holds your account's internal number, a version number that changes whenever your password is
> reset or changed, the time you signed in, and a signature that lets the app check those values.
> It holds no password and no name. It expires eight hours after you signed in, is removed when you
> sign out, and is sent only to this app.

- Staying signed in across a browser reload. After an actual sign-in the gate asks the browser to
  keep an HttpOnly, SameSite=Lax cookie named `ads_auth_session` holding a signed token
  (`ads_auth.tokens`: account id, `session_version`, sign-in time, HMAC-SHA256 signature). A new
  Streamlit session that arrives with that cookie (`st.context.cookies`) is restored silently:
  same account, same sign-in time, so the eight-hour lifetime still counts from the sign-in and a
  password reset, a password change by the person, or a deactivation still ends it on the next
  run. A restore logs no event; `login` is logged only for a sign-in through the form. Sign-out
  clears the cookie and refuses to restore from it again on that connection; a cookie that is
  expired, revoked, or not signed with the app's secret is cleared and the sign-in screen shows
  (with "Your session ended" when it was once valid). A password change re-issues the cookie with
  the new version.
- `AppSettings.cookie_secret` (new, default empty, at least 32 characters, never in `repr`) signs
  the token; empty keeps the old behaviour, so an app that sets nothing sees no change.
  `cookie_secret_from_environ(environ)` reads `ADS_AUTH_COOKIE_SECRET`, else
  `STREAMLIT_SERVER_COOKIE_SECRET` (already present on the hosted services). The derived signing
  key is purpose-specific, so sharing Streamlit's secret is safe. `AppSettings.cookie_enabled`
  says whether the feature is on; preflight prints `stay_signed_in=on|off`.
- How the cookie is set. Streamlit offers no way to set a cookie from a script, and an HttpOnly
  cookie can only come from an HTTP response, so `ads_auth.ui.cookie` registers one route on the
  running server the first time it is needed, `POST <baseUrlPath>/_ads_auth/cookie`, and draws a
  zero-height iframe whose script calls it once (with a custom request header, so a cross-site
  form cannot). The route verifies the token's signature before setting the cookie; `Secure` is
  added when the request came over HTTPS directly or via `X-Forwarded-Proto`. The server object
  is reached through Streamlit's session client, which is not public API: Streamlit 1.37 to 1.57
  serve through Tornado (`Application.add_handlers`), 1.58 and newer through Starlette
  (`app.router.routes`). Both are implemented and tested (Tornado on 1.45.1, 1.50.0, 1.52.2;
  Starlette on 1.66.0). On a Streamlit whose server the package does not recognise, sign-in works
  as before, a reload shows the sign-in screen, and one warning is logged.
- `ui.gate.remember_session(st, settings, now=False)` re-issues the cookie for the session in
  session state; `ui.account` calls it after a password change. New session keys
  `auth_cookie_action` and `auth_cookie_blocked`. `current_user` is unchanged and never reads the
  request; only `require_login` restores.

## v1.1.3 (2026-10-26)

Disclosure: unaffected.

- `ui.gate.render_sidebar` takes an optional `confirm_leave` callable. While it returns True (the
  screen holds uploads or edited text that a page change would discard), the Usage statistics and
  About links are drawn as buttons that only record the chosen page in
  `session_state[PENDING_PAGE]` instead of navigating. The new `ui.gate.confirm_navigation(st,
  settings, message)` then opens a dialog with the app's message, "Do you still wish to proceed?",
  and the buttons "Go back" and "Proceed" (`st.switch_page` to the chosen page). Closing the dialog
  any other way counts as "Go back". Apps that pass nothing see no change.
- Requires Streamlit 1.52 or newer (the dialog's `on_dismiss` hook).

## v1.1.2 (2026-10-23)

Disclosure: unaffected.

- `ui.ads_apps.render_other_ads_tools` adds a portable sidebar section linking to Automatic Audit,
  Rules Finder, Report Summarizer, and Themes Explorer. Each link has a plain emoji, a bold app
  name, and no underline. Streamlit apps can call the helper after their own sidebar content.

## v1.1.1 (2026-10-19)

Disclosure: unaffected.

- `UserStore.import_from_sqlite` and the `import-users` command accept a source database in this
  package's own schema (a `users` table) as well as the Flask-style `user` table they read before.
  The `users` table is tried first, then `user`; a source with neither fails with a message naming
  both. Dry run, allow list, no overwrite and no role inheritance are unchanged, so accounts can
  now be copied between two apps that both use this package.

## v1.1.0 (2026-10-19)

The usage event vocabulary is app-supplied. The package owns three core names, `login`,
`login_failed`, and `logout`; every other name is declared by the app.

- `AppSettings` gains `events` (the app's event names, lowercase slugs, in display order),
  `activity_events` (the subset that counts toward active users; default all of `events`), and
  `definitions` (optional Markdown replacing the generic caption on the statistics page).
  Validation rejects names that are not lowercase slugs, duplicates, core names, and activity names
  not in `events`. `all_events` and `all_activity_events` give the full sets including the core names.
- `usage.events.log_event` takes `allowed`, the names it accepts beside the core ones;
  `ui.gate.log` passes `settings.events`. The constants `EVENTS` and `ACTIVITY_EVENTS` are gone,
  replaced by `CORE_EVENTS` and `CORE_ACTIVITY_EVENTS`.
- `usage.reports`: `PeriodRow` and `UserRow` carry `logins` plus `counts`, one entry per declared
  event, instead of fixed `searches` and `uploads` fields. `active_users_by_period` takes `events` and
  `activity`; `per_user_totals`, `aggregate_csv`, and `per_user_csv` take `events`. CSV headers follow:
  `period_type,period_start,period_end,active_users,logins,<one column per event>,complete` and
  `username,logins,<one column per event>`. `AGGREGATE_HEADER` and `PER_USER_HEADER` became the
  functions `aggregate_header(events)` and `per_user_header(events)`. `combined_events_csv` is
  unchanged: any name fits the four-column export.
- `ui.reports`: one count column per declared event, labelled from the name (`library_refresh` ->
  "Library refreshes"), after Logins. The caption is generic unless `definitions` is set.

Migration: declare the names the app logs on its settings object, for example
`events=("upload", "process", "generate_labels", "summarize_dataset", "summarize_cluster", "download", "tag")`
or `events=("upload_audit_file", "upload_model_tree", "run_audit", "email_results", "download")`.
`log_event` rejects an undeclared name, so a missing declaration fails on first use. Naming
principle: a new name is for a different action, a different artifact, or a different cost (one
model call is one event, logged whenever the call happens, whatever triggered it); repeating an
action or triggering it differently is not a new name; use the same name across apps where the
concept is shared (`upload`, `download`); configuration choices (which provider or model) are not
events. Set `definitions` where the generic caption is not enough, for example to say how repeats
of an action are counted.

Disclosure: unaffected. The package stores nothing new: event rows still hold timestamp, account,
and event type, with the same retention. The vocabulary itself is now each app's responsibility,
so each app's disclosure must list the actions it records, and adding a name to `events` is an
app-side disclosure change in the same commit.

## v1.0.0 (2026-10-19)

First release. Code moved from the first consuming app with these changes:

- The app name is a field on `AppSettings` (`app_name`) instead of a module constant;
  `usage.reports.combined_events_csv` takes it as a required argument.
- The sign-in title, disclosure path, and page registry come from `AppSettings` (`title`,
  `disclosure_path`, `pages`); `ui.about.render` takes the settings object.
- `maintenance.run_once` and `start_background` accept an optional `extra` task for app-specific
  housekeeping, which stays in the app.
- `preflight.run` takes a settings loader instead of reading the app's config module.
- `cli.main` takes `load_settings` and an optional list of app `Command`s for the app's own
  subcommands.

Disclosure: unaffected. Storage, logging, retention, and transmission are identical to the code
that moved: `app.db` with `users`, `login_attempts`, `usage_events`; events hold timestamp, account,
and event type; events are pruned after 13 months; seven daily backups are kept under
`backups/`; nothing is sent to a third party.
