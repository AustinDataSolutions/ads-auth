"""Hidden page that renders the app's data-handling document verbatim.

That file is user-facing copy owned by each app (`settings.disclosure_path`). The package's
CHANGELOG marks releases that change what is stored, logged, transmitted, or retained with
"Disclosure: affected"; the app must update its document when it bumps to such a release.
"""
from __future__ import annotations

from ads_auth.ui import link


def disclosure_text(settings) -> str:
    return settings.disclosure_path.read_text(encoding="utf-8")


def render(st_module, settings) -> None:
    link(st_module, settings, "app", "Back to the app", "⬅️")
    st_module.markdown(disclosure_text(settings))
