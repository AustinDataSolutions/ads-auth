import pytest

from ads_auth import hashing
from ads_auth.models import User, normalize_username


@pytest.mark.parametrize("raw,expected", [
    ("  Pat@Example.org ", "pat@example.org"),
    ("JDOE", "jdoe"),
])
def test_normalize_username(raw, expected):
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "a b", "x" * 81, "tab\tname", "nul\x00", None])
def test_normalize_username_rejects(raw):
    with pytest.raises(ValueError):
        normalize_username(raw)


def test_generate_and_verify_scrypt_roundtrip():
    h = hashing.generate("correct horse battery staple")
    assert h.startswith("scrypt:")
    assert hashing.verify("correct horse battery staple", h)
    assert not hashing.verify("wrong", h)
    assert not hashing.verify("", h) and not hashing.verify("x", "")


def test_verifies_pbkdf2_hash_from_another_app():
    from werkzeug.security import generate_password_hash
    h = generate_password_hash("legacy-password-123", method="pbkdf2:sha256")
    assert hashing.is_supported_hash(h)
    assert hashing.verify("legacy-password-123", h)


def test_garbage_hash_verifies_false_not_raise():
    assert not hashing.verify("pw", "not-a-hash")
    assert not hashing.is_supported_hash("md5$abc") and not hashing.is_supported_hash(None)


def test_new_password_policy():
    with pytest.raises(ValueError):
        hashing.validate_new_password("short")
    hashing.validate_new_password("x" * 15)
    with pytest.raises(ValueError):
        hashing.validate_new_password("x" * 2000)
    temp = hashing.generate_temporary_password()
    assert len(temp) >= 15 and temp != hashing.generate_temporary_password()


def test_user_dataclass_has_no_hash_and_role_rank():
    u = User(1, "a", None, "manager", True, False, None, 1, "2026-01-01T00:00:00+00:00", None)
    assert "hash" not in repr(u) and not hasattr(u, "password_hash")
    assert u.has_role("user") and u.has_role("manager") and not u.has_role("admin")
