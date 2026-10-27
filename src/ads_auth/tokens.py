"""The signed "stay signed in" token carried by the sign-in cookie. Pure functions, no I/O.

A token names a session, not a person: the account's row id, the account's `session_version`
(bumped by every password reset or change, so a reset ends it), and the time of the actual
sign-in, so the eight-hour session lifetime is counted from that sign-in and never extended by a
reload. The signature is an HMAC-SHA256 over those three values with a key derived from the app's
cookie secret. The token holds no username and no password.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

from ads_auth.db import from_iso, to_iso, utc_now
from ads_auth.policy import SESSION_LIFETIME

COOKIE_NAME = "ads_auth_session"
MIN_SECRET_LENGTH = 32
_VERSION = "v1"
_PURPOSE = b"ads_auth session cookie " + _VERSION.encode()


@dataclass(frozen=True)
class SessionClaims:
    user_id: int
    session_version: int
    authenticated_utc: str          # ISO 8601, UTC, second precision (the format session state uses)


def _key(secret: str) -> bytes:
    # Derive a purpose-specific key so the same secret can serve Streamlit's own cookies without
    # one signature ever being valid for the other.
    return hmac.new(secret.encode("utf-8"), _PURPOSE, hashlib.sha256).digest()


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(_key(secret), payload.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def issue(secret: str, user_id: int, session_version: int, authenticated_utc: str) -> str:
    """The token for a session that signed in at `authenticated_utc`."""
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"cookie secret must be at least {MIN_SECRET_LENGTH} characters")
    epoch = int(from_iso(authenticated_utc).timestamp())
    payload = f"{_VERSION}.{int(user_id)}.{int(session_version)}.{epoch}"
    return f"{payload}.{_sign(secret, payload)}"


def verify(secret: str, token: str | None) -> SessionClaims | None:
    """The claims of a well-formed, correctly signed token; None otherwise.

    Age is not checked here: the caller applies the session policy (`policy.session_status`) or
    `remaining_seconds`, so an old token is reported as expired rather than as garbage.
    """
    if not secret or not token or len(token) > 200:
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != _VERSION:
        return None
    version, user_id, session_version, epoch, signature = parts
    if not (user_id.isdigit() and session_version.isdigit() and epoch.isdigit()):
        return None
    payload = ".".join((version, user_id, session_version, epoch))
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return None
    moment = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    return SessionClaims(int(user_id), int(session_version), to_iso(moment))


def remaining_seconds(claims: SessionClaims, now: datetime | None = None) -> int:
    """Seconds until the session named by `claims` reaches the session lifetime (0 when it has)."""
    end = from_iso(claims.authenticated_utc) + SESSION_LIFETIME
    return max(0, int((end - (now or utc_now())).total_seconds()))
