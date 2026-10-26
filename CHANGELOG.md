# Changelog

Releases are tagged `vX.Y.Z`. A release whose entry carries the line **Disclosure: affected** changes
what consuming apps store, log, transmit, or retain; each app must update its own data-handling
document (`DATA_HANDLING.md` or equivalent) in the same commit that bumps to that release.
Entries without the marker need no disclosure change.

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
