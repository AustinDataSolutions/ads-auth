"""Shared accounts, sessions, usage events, statistics, disclosure page, maintenance, and CLI
for Austin Data Solutions Streamlit apps.

Framework-free modules (`db`, `models`, `hashing`, `policy`, `store`, `usage.*`, `maintenance`,
`preflight`, `cli`) import no Streamlit and do no I/O at import time. Streamlit adapters live
under `ads_auth.ui`. An app supplies one `AppSettings` object; see the README.
"""
from ads_auth.settings import AUTH_MODES, AppSettings, auth_mode_from_environ, cookie_secret_from_environ, exclude_users_from_environ

__version__ = "1.2.0"

__all__ = ["AUTH_MODES", "AppSettings", "auth_mode_from_environ", "cookie_secret_from_environ", "exclude_users_from_environ", "__version__"]
