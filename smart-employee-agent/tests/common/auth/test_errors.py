"""Tests for common/auth/errors.py — Wave 1, Sprint 1.

Covers:
- Class-level error_id on every exception class
- Inheritance: all subclasses are AuthError; CIBA subclasses are also CIBAError
- Default details is empty dict; explicit details is stored correctly
- __str__ format: "<error_id>: <message>"
- error_id override via constructor keyword argument
- message stored on instance
- JWTValidationError default error_id is ERR-AUTH-006 (per error-catalog.md)
- CIBATimeoutError is raised on local cancellation (F-10 rule)
"""
from __future__ import annotations

import pytest

from common.auth.errors import (
    ActorTokenError,
    AuthError,
    CIBADeniedError,
    CIBAError,
    CIBAExpiredError,
    CIBAInitiationError,
    CIBAPollError,
    CIBATimeoutError,
    JWTValidationError,
    PeerTrustError,
    ScopeError,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

ALL_EXCEPTION_CLASSES = [
    AuthError,
    JWTValidationError,
    PeerTrustError,
    ScopeError,
    CIBAError,
    CIBAInitiationError,
    CIBADeniedError,
    CIBAExpiredError,
    CIBATimeoutError,
    CIBAPollError,
    ActorTokenError,
]

EXPECTED_ERROR_IDS = {
    AuthError: "ERR-AUTH-000",
    JWTValidationError: "ERR-AUTH-006",
    PeerTrustError: "ERR-AGENT-002",
    ScopeError: "ERR-MCP-003",
    CIBAError: "ERR-CIBA-000",
    CIBAInitiationError: "ERR-CIBA-001",
    CIBADeniedError: "ERR-CIBA-005",
    CIBAExpiredError: "ERR-CIBA-009",
    CIBATimeoutError: "ERR-CIBA-010",
    CIBAPollError: "ERR-CIBA-008",
    ActorTokenError: "ERR-CIBA-009",
}


# ── error_id correctness ──────────────────────────────────────────────────────


@pytest.mark.parametrize("cls", ALL_EXCEPTION_CLASSES)
def test_class_level_error_id(cls: type) -> None:
    """Class attribute error_id is set to the expected catalog code."""
    assert cls.error_id == EXPECTED_ERROR_IDS[cls], (
        f"{cls.__name__}.error_id should be {EXPECTED_ERROR_IDS[cls]!r}, "
        f"got {cls.error_id!r}"
    )


@pytest.mark.parametrize("cls", ALL_EXCEPTION_CLASSES)
def test_instance_error_id_matches_class(cls: type) -> None:
    """Instance error_id defaults to the class-level value."""
    instance = cls("some message")
    assert instance.error_id == EXPECTED_ERROR_IDS[cls]


def test_jwt_validation_error_default_error_id() -> None:
    """JWTValidationError.error_id is ERR-AUTH-006 (bad signature baseline per catalog)."""
    err = JWTValidationError("bad sig")
    assert err.error_id == "ERR-AUTH-006"


# ── Inheritance ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cls",
    [
        JWTValidationError,
        PeerTrustError,
        ScopeError,
        CIBAError,
        CIBAInitiationError,
        CIBADeniedError,
        CIBAExpiredError,
        CIBATimeoutError,
        CIBAPollError,
        ActorTokenError,
    ],
)
def test_all_subclasses_are_auth_error(cls: type) -> None:
    """Every exported exception is a subclass of AuthError."""
    assert issubclass(cls, AuthError), f"{cls.__name__} must inherit from AuthError"
    assert isinstance(cls("msg"), AuthError)


@pytest.mark.parametrize(
    "cls",
    [
        CIBAInitiationError,
        CIBADeniedError,
        CIBAExpiredError,
        CIBATimeoutError,
        CIBAPollError,
    ],
)
def test_ciba_subclasses_are_ciba_error(cls: type) -> None:
    """CIBAInitiationError / CIBADeniedError / … are all CIBAError subclasses."""
    assert issubclass(cls, CIBAError), f"{cls.__name__} must inherit from CIBAError"
    assert isinstance(cls("msg"), CIBAError)


def test_ciba_denied_is_auth_error() -> None:
    """isinstance check: CIBADeniedError instance satisfies AuthError."""
    assert isinstance(CIBADeniedError("denied"), AuthError)


def test_ciba_denied_is_ciba_error() -> None:
    """isinstance check: CIBADeniedError instance satisfies CIBAError."""
    assert isinstance(CIBADeniedError("denied"), CIBAError)


def test_actor_token_error_is_not_ciba_error() -> None:
    """ActorTokenError is a direct AuthError subclass, NOT a CIBAError subclass."""
    assert not issubclass(ActorTokenError, CIBAError)


# ── __str__ format ────────────────────────────────────────────────────────────


def test_str_format_includes_error_id_and_message() -> None:
    """str(exc) is '<error_id>: <message>'."""
    err = CIBAExpiredError("auth_req_id=abc123 timed out")
    result = str(err)
    assert "ERR-CIBA-009" in result
    assert "auth_req_id=abc123 timed out" in result
    assert result == "ERR-CIBA-009: auth_req_id=abc123 timed out"


def test_str_format_empty_message() -> None:
    """str() still works when message is empty string."""
    err = PeerTrustError()
    assert str(err) == "ERR-AGENT-002: "


@pytest.mark.parametrize(
    "cls,expected_prefix",
    [
        (JWTValidationError, "ERR-AUTH-006"),
        (PeerTrustError, "ERR-AGENT-002"),
        (ScopeError, "ERR-MCP-003"),
        (CIBAInitiationError, "ERR-CIBA-001"),
        (CIBADeniedError, "ERR-CIBA-005"),
        (CIBAExpiredError, "ERR-CIBA-009"),
        (CIBATimeoutError, "ERR-CIBA-010"),
        (CIBAPollError, "ERR-CIBA-008"),
        (ActorTokenError, "ERR-CIBA-009"),
    ],
)
def test_str_starts_with_error_id(cls: type, expected_prefix: str) -> None:
    """str(exc) starts with the catalog error_id for each exception class."""
    err = cls("test message")
    assert str(err).startswith(expected_prefix)


# ── details ───────────────────────────────────────────────────────────────────


def test_details_defaults_to_empty_dict() -> None:
    """details is {} when not provided."""
    err = JWTValidationError("no details")
    assert err.details == {}
    assert isinstance(err.details, dict)


def test_details_stored_when_provided() -> None:
    """Explicit details dict is stored on the instance."""
    payload = {"kid": "key-1", "jti": "abc"}
    err = JWTValidationError("bad sig", details=payload)
    assert err.details == payload


def test_details_not_shared_between_instances() -> None:
    """Each instance gets its own details dict (not a class-level mutable default)."""
    err1 = AuthError("first")
    err2 = AuthError("second")
    err1.details["x"] = 1
    assert "x" not in err2.details


def test_none_details_becomes_empty_dict() -> None:
    """Passing details=None is equivalent to not passing details."""
    err = CIBADeniedError("denied", details=None)
    assert err.details == {}


# ── message attribute ─────────────────────────────────────────────────────────


def test_message_attribute_stored() -> None:
    """self.message holds the exact string passed to the constructor."""
    msg = "JWT issuer mismatch"
    err = JWTValidationError(msg)
    assert err.message == msg


def test_message_default_empty_string() -> None:
    """message defaults to empty string when not provided."""
    err = CIBATimeoutError()
    assert err.message == ""


# ── error_id override via constructor ─────────────────────────────────────────


def test_error_id_override_at_construction() -> None:
    """Callers may override error_id at construction for narrow catalog entries."""
    err = JWTValidationError("token expired", error_id="ERR-AUTH-008")
    assert err.error_id == "ERR-AUTH-008"
    assert str(err) == "ERR-AUTH-008: token expired"


def test_error_id_override_does_not_mutate_class() -> None:
    """Overriding error_id on an instance does not change the class attribute."""
    JWTValidationError("x", error_id="ERR-AUTH-007")
    assert JWTValidationError.error_id == "ERR-AUTH-006"


def test_ciba_initiation_error_override() -> None:
    """CIBAInitiationError can carry ERR-CIBA-002 / ERR-CIBA-003 / ERR-CIBA-004."""
    for catalog_id in ("ERR-CIBA-002", "ERR-CIBA-003", "ERR-CIBA-004"):
        err = CIBAInitiationError("detail", error_id=catalog_id)
        assert err.error_id == catalog_id


# ── CIBATimeoutError cancellation semantics (F-10) ────────────────────────────


def test_ciba_timeout_error_on_cancel() -> None:
    """CIBATimeoutError("cancelled") is well-formed for the F-10 cancellation path."""
    err = CIBATimeoutError("cancelled")
    assert isinstance(err, CIBAError)
    assert isinstance(err, AuthError)
    assert err.error_id == "ERR-CIBA-010"
    assert err.message == "cancelled"


# ── Exception is raised and caught correctly ──────────────────────────────────


def test_raise_and_catch_as_auth_error() -> None:
    """Any specific exception can be caught as AuthError at the boundary."""
    with pytest.raises(AuthError) as exc_info:
        raise CIBADeniedError("user pressed deny", details={"auth_req_id": "xyz"})
    assert exc_info.value.error_id == "ERR-CIBA-005"
    assert exc_info.value.details["auth_req_id"] == "xyz"


def test_raise_and_catch_as_ciba_error() -> None:
    """CIBAExpiredError can be caught as CIBAError."""
    with pytest.raises(CIBAError):
        raise CIBAExpiredError("expired after 300s")


def test_actor_token_error_not_caught_as_ciba_error() -> None:
    """ActorTokenError does NOT satisfy CIBAError catch block."""
    with pytest.raises(ActorTokenError):
        try:
            raise ActorTokenError("mint failed")
        except CIBAError:
            pytest.fail("ActorTokenError must not be caught as CIBAError")
