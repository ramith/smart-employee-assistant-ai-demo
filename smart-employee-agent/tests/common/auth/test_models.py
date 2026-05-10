"""Tests for common/auth/models.py — Wave 1, Sprint 1.

Covers:
- OAuthToken.from_response: correct expires_at computation
- OBOToken.is_expired: fresh / past / within-buffer edge cases
- JWTClaims.act_chain: no-act, depth-1, depth-2 nested
- Frozen dataclass mutation raises FrozenInstanceError
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from common.auth.models import JWTClaims, OAuthToken, OBOToken


# ── helpers ───────────────────────────────────────────────────────────────────

def _utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _make_oauth_token(
    *,
    expires_at: datetime,
    access_token: str = "tok",
    scope: str = "openid",
) -> OAuthToken:
    """Build a minimal OAuthToken with a specified expires_at."""
    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=3600,
        expires_at=expires_at,
        refresh_token=None,
        scope=scope,
        id_token=None,
    )


def _make_obo_token(*, expires_at: datetime) -> OBOToken:
    """Build a minimal OBOToken with the given expires_at on its raw token."""
    raw = _make_oauth_token(expires_at=expires_at)
    return OBOToken(
        raw=raw,
        sub="user-uuid",
        act_sub="agent-uuid",
        aud="agent-client-id",
        iss="https://is.example.com/oauth2/token",
        iat=datetime.now(tz=timezone.utc),
        jti="jti-12345",
    )


def _make_jwt_claims(*, act: dict | None = None) -> JWTClaims:
    """Build a minimal JWTClaims with a customisable act claim."""
    return JWTClaims(
        sub="user-uuid",
        iss="https://is.example.com/oauth2/token",
        aud="client-id",
        exp=9999999999,
        iat=1700000000,
        jti="jti-abc",
        act=act,
        scope="openid hr.read",
        aut="APPLICATION_USER",
    )


# ── OAuthToken.from_response ──────────────────────────────────────────────────


class TestOAuthTokenFromResponse:
    """OAuthToken.from_response computes expires_at correctly."""

    def test_expires_at_equals_now_plus_expires_in(self) -> None:
        """expires_at must be exactly now + expires_in seconds."""
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        body = {
            "access_token": "abc",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid",
        }
        token = OAuthToken.from_response(body, now=now)
        expected = now + timedelta(seconds=3600)
        assert token.expires_at == expected

    def test_expires_at_uses_wall_clock_when_now_omitted(self) -> None:
        """When now is not supplied, expires_at should be in the future."""
        before = datetime.now(tz=timezone.utc)
        token = OAuthToken.from_response(
            {"access_token": "x", "expires_in": 60, "scope": "openid"}
        )
        after = datetime.now(tz=timezone.utc)
        assert before + timedelta(seconds=60) <= token.expires_at <= after + timedelta(seconds=60)

    def test_optional_fields_absent(self) -> None:
        """refresh_token and id_token default to None when absent from body."""
        token = OAuthToken.from_response(
            {"access_token": "x", "expires_in": 100, "scope": "openid"}
        )
        assert token.refresh_token is None
        assert token.id_token is None

    def test_optional_fields_present(self) -> None:
        """refresh_token and id_token are captured when present in body."""
        body = {
            "access_token": "x",
            "token_type": "Bearer",
            "expires_in": 100,
            "scope": "openid",
            "refresh_token": "rtoken",
            "id_token": "idtoken",
        }
        token = OAuthToken.from_response(body)
        assert token.refresh_token == "rtoken"
        assert token.id_token == "idtoken"

    def test_token_type_defaults_to_bearer(self) -> None:
        """token_type defaults to 'Bearer' if absent from body."""
        token = OAuthToken.from_response(
            {"access_token": "x", "expires_in": 100, "scope": "openid"}
        )
        assert token.token_type == "Bearer"

    def test_expires_at_is_timezone_aware(self) -> None:
        """expires_at must carry UTC timezone info."""
        token = OAuthToken.from_response(
            {"access_token": "x", "expires_in": 100, "scope": "openid"}
        )
        assert token.expires_at.tzinfo is not None


# ── OBOToken.is_expired ───────────────────────────────────────────────────────


class TestOBOTokenIsExpired:
    """OBOToken.is_expired returns False when fresh, True when past, True within buffer."""

    def test_fresh_token_not_expired(self) -> None:
        """A token expiring far in the future should not be considered expired."""
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        obo = _make_obo_token(expires_at=future)
        assert obo.is_expired() is False

    def test_past_token_is_expired(self) -> None:
        """A token whose expires_at is in the past must be considered expired."""
        past = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
        obo = _make_obo_token(expires_at=past)
        assert obo.is_expired() is True

    def test_within_buffer_is_expired(self) -> None:
        """A token expiring within the buffer window must be treated as expired."""
        # expires in 10 seconds; default buffer is 30 seconds
        near_future = datetime.now(tz=timezone.utc) + timedelta(seconds=10)
        obo = _make_obo_token(expires_at=near_future)
        assert obo.is_expired(buffer_s=30) is True

    def test_just_outside_buffer_not_expired(self) -> None:
        """A token expiring just outside the buffer window must NOT be expired."""
        # expires in 60 seconds; buffer is 30 seconds
        comfortable = datetime.now(tz=timezone.utc) + timedelta(seconds=60)
        obo = _make_obo_token(expires_at=comfortable)
        assert obo.is_expired(buffer_s=30) is False

    def test_custom_buffer_zero(self) -> None:
        """With buffer_s=0, a token expiring in 1 second must NOT be expired."""
        slight_future = datetime.now(tz=timezone.utc) + timedelta(seconds=1)
        obo = _make_obo_token(expires_at=slight_future)
        assert obo.is_expired(buffer_s=0) is False

    def test_expired_with_custom_buffer(self) -> None:
        """A token expiring in 5 seconds with buffer_s=10 must be expired."""
        near = datetime.now(tz=timezone.utc) + timedelta(seconds=5)
        obo = _make_obo_token(expires_at=near)
        assert obo.is_expired(buffer_s=10) is True


# ── JWTClaims.act_chain ───────────────────────────────────────────────────────


class TestJWTClaimsActChain:
    """JWTClaims.act_chain returns the depth-walk of the act claim."""

    def test_no_act_returns_empty_list(self) -> None:
        """act=None must yield an empty list."""
        claims = _make_jwt_claims(act=None)
        assert claims.act_chain() == []

    def test_depth_1_act(self) -> None:
        """A single-level act claim with sub='agent-a' must yield ['agent-a']."""
        claims = _make_jwt_claims(act={"sub": "agent-a"})
        assert claims.act_chain() == ["agent-a"]

    def test_depth_2_nested_act(self) -> None:
        """Depth-2 nesting must yield [outer_sub, inner_sub] i.e. outermost first."""
        claims = _make_jwt_claims(act={"sub": "agent-b", "act": {"sub": "agent-a"}})
        assert claims.act_chain() == ["agent-b", "agent-a"]

    def test_depth_3_nested_act(self) -> None:
        """Depth-3 nesting returns three entries, outermost first."""
        act = {"sub": "c", "act": {"sub": "b", "act": {"sub": "a"}}}
        claims = _make_jwt_claims(act=act)
        assert claims.act_chain() == ["c", "b", "a"]

    def test_act_missing_sub_is_skipped(self) -> None:
        """An act dict without a sub key must not contribute an entry."""
        # Outer act has no sub; inner act has sub="inner"
        claims = _make_jwt_claims(act={"act": {"sub": "inner"}})
        assert claims.act_chain() == ["inner"]

    def test_non_dict_act_terminates_walk(self) -> None:
        """If an act value is not a dict (e.g. null in inner nesting), walk terminates."""
        claims = _make_jwt_claims(act={"sub": "only-one", "act": "not-a-dict"})
        assert claims.act_chain() == ["only-one"]


# ── Frozen dataclass immutability ─────────────────────────────────────────────


class TestFrozenDataclasses:
    """Verify that all three dataclasses are truly frozen."""

    def test_oauth_token_is_frozen(self) -> None:
        """Assigning to OAuthToken.access_token must raise FrozenInstanceError."""
        now = datetime.now(tz=timezone.utc)
        token = OAuthToken(
            access_token="original",
            token_type="Bearer",
            expires_in=3600,
            expires_at=now + timedelta(seconds=3600),
            refresh_token=None,
            scope="openid",
            id_token=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            token.access_token = "x"  # type: ignore[misc]

    def test_obo_token_is_frozen(self) -> None:
        """Assigning to OBOToken.sub must raise FrozenInstanceError."""
        obo = _make_obo_token(expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1))
        with pytest.raises(dataclasses.FrozenInstanceError):
            obo.sub = "new-sub"  # type: ignore[misc]

    def test_jwt_claims_is_frozen(self) -> None:
        """Assigning to JWTClaims.jti must raise FrozenInstanceError."""
        claims = _make_jwt_claims()
        with pytest.raises(dataclasses.FrozenInstanceError):
            claims.jti = "tampered"  # type: ignore[misc]


class TestJWTClaimsSprint4Identity:
    """Sprint 4: JWTClaims gains username/email fields with None defaults."""

    def test_username_email_default_to_none(self) -> None:
        claims = _make_jwt_claims()
        assert claims.username is None
        assert claims.email is None

    def test_username_email_settable(self) -> None:
        claims = JWTClaims(
            sub="user-uuid",
            iss="https://is.example.com/oauth2/token",
            aud="client-id",
            exp=9999999999,
            iat=1700000000,
            jti="jti-abc",
            act=None,
            scope="openid",
            aut="APPLICATION_USER",
            username="jane.doe",
            email="jane.doe@example.com",
        )
        assert claims.username == "jane.doe"
        assert claims.email == "jane.doe@example.com"
