"""The signed stay-signed-in token: pure functions, no Streamlit, no server."""
from datetime import timedelta

import pytest

from ads_auth import tokens
from ads_auth.db import from_iso, to_iso, utc_now
from ads_auth.policy import SESSION_LIFETIME

SECRET = "s" * 32
WHEN = "2026-10-26T12:00:00+00:00"


def test_round_trip_carries_account_version_and_sign_in_time():
    token = tokens.issue(SECRET, 7, 3, WHEN)
    assert token.startswith("v1.7.3.")
    assert tokens.verify(SECRET, token) == tokens.SessionClaims(7, 3, WHEN)


def test_sign_in_time_is_kept_to_the_second():
    token = tokens.issue(SECRET, 7, 3, "2026-10-26T07:00:00.250000-05:00")
    assert tokens.verify(SECRET, token).authenticated_utc == "2026-10-26T12:00:00+00:00"


@pytest.mark.parametrize("bad", [
    None, "", "v1.7.3", "v2.7.3.1793016000.sig", "v1.x.3.1793016000.sig", "garbage", "a" * 300,
])
def test_malformed_tokens_are_rejected(bad):
    assert tokens.verify(SECRET, bad) is None


def test_tampering_or_another_secret_breaks_the_signature():
    token = tokens.issue(SECRET, 7, 3, WHEN)
    head, sig = token.rsplit(".", 1)
    assert tokens.verify(SECRET, head.replace(".7.", ".8.") + "." + sig) is None     # another account
    assert tokens.verify(SECRET, head.replace(".3.", ".2.") + "." + sig) is None     # older password version
    assert tokens.verify(SECRET, head + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")) is None
    assert tokens.verify("t" * 32, token) is None
    assert tokens.verify("", token) is None


def test_a_short_secret_is_refused():
    with pytest.raises(ValueError):
        tokens.issue("short", 1, 1, WHEN)


def test_remaining_seconds_counts_from_the_sign_in():
    now = utc_now()
    claims = tokens.verify(SECRET, tokens.issue(SECRET, 1, 1, to_iso(now - timedelta(hours=1))))
    left = tokens.remaining_seconds(claims, now)
    assert abs(left - (SESSION_LIFETIME - timedelta(hours=1)).total_seconds()) <= 1
    old = tokens.verify(SECRET, tokens.issue(SECRET, 1, 1, to_iso(now - SESSION_LIFETIME)))
    assert tokens.remaining_seconds(old, now) == 0
    assert from_iso(old.authenticated_utc) <= now
