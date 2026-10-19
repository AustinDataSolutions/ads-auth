"""Streamlit adapters over the framework-free modules.

Every function takes the Streamlit module and the app's `AppSettings` as arguments, so the
package never imports `streamlit` at module level and tests can pass a fake.
"""
from __future__ import annotations


def link(st_module, settings, name: str, label: str, icon: str | None = None) -> None:
    """Render a page link if the app registered `name` in `settings.pages`; otherwise nothing."""
    page = settings.pages.get(name)
    if page is not None:
        st_module.page_link(page, label=label, icon=icon)
