# Changelog

Releases are tagged `vX.Y.Z`. A release whose entry carries the line **Disclosure: affected** changes
what consuming apps store, log, transmit, or retain; each app must update its own data-handling
document (`DATA_HANDLING.md` or equivalent) in the same commit that bumps to that release.
Entries without the marker need no disclosure change.

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
