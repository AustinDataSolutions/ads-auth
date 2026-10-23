from ads_auth.ui.ads_apps import ADS_TOOLS, render_other_ads_tools


class _Sidebar:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Streamlit:
    def __init__(self):
        self.sidebar = _Sidebar()

    def divider(self):
        self.sidebar.calls.append(("divider",))

    def markdown(self, text, **kwargs):
        self.sidebar.calls.append(("markdown", text, kwargs))


def test_other_ads_tools_are_portable_sidebar_links():
    st = _Streamlit()

    render_other_ads_tools(st)

    assert st.sidebar.calls == [
        ("divider",),
        ("markdown", "**Other tools from ADS:**", {}),
        *((
            "markdown",
            f'{emoji} <a href="{url}" target="_blank" rel="noopener noreferrer" '
            f'style="text-decoration: none;"><strong>{name}</strong></a>',
            {"unsafe_allow_html": True},
        ) for emoji, name, url in ADS_TOOLS),
    ]
