"""Links to the other ADS tools for Streamlit app sidebars."""

from __future__ import annotations


# Kept separate from authentication and app settings so every Streamlit app can
# place this shared section after its own sidebar content.
ADS_TOOLS = (
    ("📋", "Automatic Audit", "https://aarp-automatic-audit.onrender.com/"),
    ("🔎", "Rules Finder", "https://model-rules-finder.onrender.com/"),
    ("📝", "Report Summarizer", "https://periodic-report-summarizer.onrender.com/"),
    ("🔭", "Themes Explorer", "https://aarp-themes-explorer.onrender.com/"),
)


def render_other_ads_tools(st_module) -> None:
    """Add the shared ADS-tools section below an application's existing sidebar items."""
    with st_module.sidebar:
        st_module.divider()
        st_module.markdown("**Other tools from ADS:**")
        for emoji, name, url in ADS_TOOLS:
            st_module.markdown(
                f'{emoji} <a href="{url}" target="_blank" rel="noopener noreferrer" '
                f'style="text-decoration: none;"><strong>{name}</strong></a>',
                unsafe_allow_html=True,
            )
