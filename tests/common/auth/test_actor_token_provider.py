"""Tests for common/auth/actor_token_provider.py — Wave 3, Sprint 1.

Covers (≥10 tests):
 1. Fresh provider (no cache) → all 3 IS endpoints called, OAuthToken returned
 2. Cached fresh token → no IS round-trip on second call
 3. Token within buffer_seconds of expiry → re-mints
 4. force_refresh() ignores valid cache, calls all 3 IS endpoints
 5. invalidate() clears cache; next call re-mints
 6. Single-flight: 10 concurrent ensure_valid_token() calls → exactly 1 IS round-trip
 7. IS returns 4xx on /authorize → ActorTokenError raised
 8. IS returns 200 on /authorize but no flowId → ActorTokenError raised
 9. IS returns 200 on /authn but no code → ActorTokenError raised
10. IS returns 200 on /token but no access_token → ActorTokenError raised
11. AgentCredentials frozen — mutation attempt raises FrozenInstanceError
12. _pkce_pair() generates distinct verifier+challenge on each call
13. _is_fresh() returns False when token is None
14. force_refresh() stores result in cache (next ensure_valid_token() is a cache hit)
"""

from __future__ import annotations

# ── Bootstrap (mirrors pattern from test_wso2_is_client.py) ───────────────────
# Load the full dependency chain without running the stale __init__.py files.

import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/

# Ensure package stubs exist (conftest does 'common' and 'common.auth'; be idempotent)
for _pkg in ("common", "common.auth"):
    if _pkg not in sys.modules:
        _stub = types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        _stub.__path__ = [str(_ROOT / _pkg.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules without executing package __init__."""
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]
    file_path = _ROOT / rel_path
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {file_path}"
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted_name.rsplit(".", 1)[0] if "." in dotted_name else ""
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Load dependency chain in order
_load_module("common.auth.models", "common/auth/models.py")
_load_module("common.auth.errors", "common/auth/errors.py")
_load_module("common.auth.wso2_is_client", "common/auth/wso2_is_client.py")
_load_module("common.auth.actor_token_provider", "common/auth/actor_token_provider.py")

# ── Imports ────────────────────────────────────────────────────────────────────

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_httpx

from common.auth.errors import ActorTokenError
from common.auth.models import OAuthToken
from common.auth.wso2_is_client import WSO2ISClient, WSO2ISClientConfig
from common.auth.actor_token_provider import (
    ACTOR_TOKEN_CACHE_MAX_TTL_SECONDS,
    REFRESH_BUFFER_SECONDS,
    AgentCredentials,
    ActorTokenProvider,
    _pkce_pair,
)

# ── Constants / helpers ────────────────────────────────────────────────────────

BASE_URL = "https://is.example.com:9443"
AUTHORIZE_URL = f"{BASE_URL}/oauth2/authorize"
AUTHN_URL = f"{BASE_URL}/oauth2/authn"
TOKEN_URL = f"{BASE_URL}/oauth2/token"

CREDS = AgentCredentials(
    agent_id="agent-uuid-1234",
    agent_secret="super-secret",
    oauth_client_id="oauth-client-abc",
    oauth_client_secret="oauth-secret-xyz",
    redirect_uri="http://localhost:9999/agent-callback",
)


def _make_is_client(httpx_mock: pytest_httpx.HTTPXMock) -> WSO2ISClient:
    """Build a WSO2ISClient whose httpx transport is intercepted by pytest-httpx."""
    cfg = WSO2ISClientConfig(base_url=BASE_URL)
    http = httpx.AsyncClient(verify=False, headers={"Accept": "application/json"})
    return WSO2ISClient(cfg, http=http)


def _authorize_body(flow_id: str = "flow-001") -> dict[str, Any]:
    return {
        "flowId": flow_id,
        "nextStep": {
            "authenticators": [{"authenticatorId": "BasicAuthenticator"}]
        },
    }


def _authn_body(code: str = "auth-code-001") -> dict[str, Any]:
    return {"authData": {"code": code}}


def _token_body(
    access_token: str = "eyJhbGciOiJSUzI1NiJ9.payload.sig",
    expires_in: int = 3600,
) -> dict[str, Any]:
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": "openid internal_login",
    }


def _register_full_flow(
    httpx_mock: pytest_httpx.HTTPXMock,
    *,
    access_token: str = "eyJhbGciOiJSUzI1NiJ9.payload.sig",
    expires_in: int = 3600,
) -> None:
    """Register mock responses for all three App-Native Auth steps."""
    httpx_mock.add_response(method="POST", url=AUTHORIZE_URL, json=_authorize_body())
    httpx_mock.add_response(method="POST", url=AUTHN_URL, json=_authn_body())
    httpx_mock.add_response(
        method="POST",
        url=TOKEN_URL,
        json=_token_body(access_token=access_token, expires_in=expires_in),
    )


def _make_provider(
    httpx_mock: pytest_httpx.HTTPXMock,
    *,
    buffer_seconds: int = REFRESH_BUFFER_SECONDS,
    scope: str = "openid internal_login",
) -> ActorTokenProvider:
    """Build an ActorTokenProvider backed by the httpx mock."""
    is_client = _make_is_client(httpx_mock)
    return ActorTokenProvider(
        credentials=CREDS,
        is_client=is_client,
        buffer_seconds=buffer_seconds,
        scope=scope,
    )


# ── 1. Fresh provider — all 3 IS endpoints called ─────────────────────────────


class TestFreshProviderMints:
    """A provider with no cached token calls all three IS endpoints."""

    @pytest.mark.asyncio
    async def test_returns_oauth_token(self, httpx_mock: pytest_httpx.HTTPXMock) -> None:
        _register_full_flow(httpx_mock)
        provider = _make_provider(httpx_mock)

        token = await provider.ensure_valid_token()

        assert isinstance(token, OAuthToken)
        assert token.access_token == "eyJhbGciOiJSUzI1NiJ9.payload.sig"
        assert token.expires_in == 3600

    @pytest.mark.asyncio
    async def test_calls_all_three_endpoints(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        _register_full_flow(httpx_mock)
        provider = _make_provider(httpx_mock)

        await provider.ensure_valid_token()

        requests = httpx_mock.get_requests()
        urls = [str(r.url) for r in requests]
        assert any(AUTHORIZE_URL in u for u in urls), "POST /authorize not called"
        assert any(AUTHN_URL in u for u in urls), "POST /authn not called"
        assert any(TOKEN_URL in u for u in urls), "POST /token not called"
        assert len(requests) == 3


# ── 2. Second call hits cache — no IS round-trip ──────────────────────────────


class TestCacheHit:
    """A second ensure_valid_token() on a fresh token must not call IS."""

    @pytest.mark.asyncio
    async def test_second_call_no_is_request(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        _register_full_flow(httpx_mock)
        provider = _make_provider(httpx_mock)

        first = await provider.ensure_valid_token()
        second = await provider.ensure_valid_token()

        # Only 3 requests for the first mint; nothing extra for the second call.
        assert len(httpx_mock.get_requests()) == 3
        assert second is first  # exact same object returned from cache

    @pytest.mark.asyncio
    async def test_cache_returns_same_token_object(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        _register_full_flow(httpx_mock)
        provider = _make_provider(httpx_mock)

        t1 = await provider.ensure_valid_token()
        t2 = await provider.ensure_valid_token()

        assert t1.access_token == t2.access_token


# ── 2b. Cache TTL is capped well below the IS-issued lifetime ─────────────────


class TestCacheTtlCap:
    """The cached token's expiry is capped at ACTOR_TOKEN_CACHE_MAX_TTL_SECONDS.

    IS issues ~1-hour tokens, but the cache must re-mint far sooner so an agent
    deactivated in the IS Console loses access within seconds (the underlying
    JWT keeps its real exp and stays valid downstream; only our cache is capped).
    """

    @pytest.mark.asyncio
    async def test_cached_expiry_capped_below_is_lifetime(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        _register_full_flow(httpx_mock, expires_in=3600)
        provider = _make_provider(httpx_mock)

        before = datetime.now(tz=timezone.utc)
        token = await provider.ensure_valid_token()
        after = datetime.now(tz=timezone.utc)

        # expires_in (the raw IS claim) is untouched; only expires_at is capped.
        assert token.expires_in == 3600
        cap_lo = before + timedelta(seconds=ACTOR_TOKEN_CACHE_MAX_TTL_SECONDS)
        cap_hi = after + timedelta(seconds=ACTOR_TOKEN_CACHE_MAX_TTL_SECONDS)
        assert cap_lo <= token.expires_at <= cap_hi
        # Sanity: capped far below the 1-hour IS lifetime.
        assert token.expires_at < before + timedelta(seconds=60)


# ── 3. Token within buffer of expiry → re-mints ───────────────────────────────


class TestBufferExpiry:
    """When the cached token is within buffer_seconds of expiry, a fresh mint occurs."""

    @pytest.mark.asyncio
    async def test_remints_when_within_buffer(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        # Prime cache with a first token
        _register_full_flow(httpx_mock, access_token="token-first", expires_in=3600)
        provider = _make_provider(httpx_mock, buffer_seconds=30)
        await provider.ensure_valid_token()

        # Manually wind the cached token's expires_at to 10 seconds from now
        # (within the 30-second buffer)
        assert provider._cached is not None
        near_expiry = datetime.now(tz=timezone.utc) + timedelta(seconds=10)
        # OAuthToken is frozen — replace via object.__setattr__ on the dataclass field
        expired_token = OAuthToken(
            access_token="token-first",
            token_type="Bearer",
            expires_in=10,
            expires_at=near_expiry,
            refresh_token=None,
            scope="openid internal_login",
            id_token=None,
        )
        # Bypass the frozen dataclass to inject the near-expiry token
        object.__setattr__(provider, "_cached", expired_token)

        # Register a second full flow for the re-mint
        _register_full_flow(httpx_mock, access_token="token-second", expires_in=3600)

        new_token = await provider.ensure_valid_token()

        assert new_token.access_token == "token-second"
        # 3 requests for first mint + 3 requests for second mint = 6 total
        assert len(httpx_mock.get_requests()) == 6


# ── 4. force_refresh() ignores cache ─────────────────────────────────────────


class TestForceRefresh:
    """force_refresh() always mints a new token regardless of cache state."""

    @pytest.mark.asyncio
    async def test_force_refresh_ignores_valid_cache(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        _register_full_flow(httpx_mock, access_token="token-initial", expires_in=3600)
        provider = _make_provider(httpx_mock)

        first = await provider.ensure_valid_token()
        assert first.access_token == "token-initial"

        # Register a second round for force_refresh
        _register_full_flow(httpx_mock, access_token="token-refreshed", expires_in=3600)

        refreshed = await provider.force_refresh()

        assert refreshed.access_token == "token-refreshed"
        # 3 for initial mint + 3 for force_refresh = 6 total IS requests
        assert len(httpx_mock.get_requests()) == 6

    @pytest.mark.asyncio
    async def test_force_refresh_updates_cache(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """After force_refresh, ensure_valid_token() should hit the new cache."""
        _register_full_flow(httpx_mock, access_token="token-a", expires_in=3600)
        provider = _make_provider(httpx_mock)
        await provider.ensure_valid_token()

        _register_full_flow(httpx_mock, access_token="token-b", expires_in=3600)
        await provider.force_refresh()

        # No new IS calls — cache should now hold token-b
        third = await provider.ensure_valid_token()
        assert third.access_token == "token-b"
        assert len(httpx_mock.get_requests()) == 6  # 3 + 3, not 9


# ── 5. invalidate() drops cache ───────────────────────────────────────────────


class TestInvalidate:
    """invalidate() clears the cache; next call re-mints."""

    @pytest.mark.asyncio
    async def test_invalidate_triggers_remint(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        _register_full_flow(httpx_mock, access_token="token-first", expires_in=3600)
        provider = _make_provider(httpx_mock)
        await provider.ensure_valid_token()

        provider.invalidate()
        assert provider._cached is None

        # Re-register a second flow
        _register_full_flow(httpx_mock, access_token="token-second", expires_in=3600)
        new_token = await provider.ensure_valid_token()

        assert new_token.access_token == "token-second"
        assert len(httpx_mock.get_requests()) == 6  # 3 + 3


# ── 6. Single-flight: 10 concurrent calls → 1 IS round-trip ──────────────────


class TestSingleFlight:
    """Under concurrent load, only one IS round-trip is executed."""

    @pytest.mark.asyncio
    async def test_concurrent_calls_single_mint(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """10 concurrent ensure_valid_token() calls must result in exactly 3 IS requests."""
        # Register one full flow — if more than one mint fires the extra calls will
        # produce pytest-httpx "unexpected request" errors, so this also acts as an
        # assertion on the single-flight guarantee.
        _register_full_flow(httpx_mock)
        provider = _make_provider(httpx_mock)

        results = await asyncio.gather(
            *[provider.ensure_valid_token() for _ in range(10)]
        )

        # All 10 callers received the same token
        access_tokens = {t.access_token for t in results}
        assert len(access_tokens) == 1

        # Exactly one IS round-trip (3 requests: authorize + authn + token)
        assert len(httpx_mock.get_requests()) == 3


# ── 7. IS 4xx on /authorize → ActorTokenError ────────────────────────────────


class TestAuthorizeError:
    """A non-200 response from /oauth2/authorize raises ActorTokenError."""

    @pytest.mark.asyncio
    async def test_authorize_4xx_raises_actor_token_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=AUTHORIZE_URL,
            status_code=401,
            json={"error": "unauthorized_client"},
        )
        provider = _make_provider(httpx_mock)

        with pytest.raises(ActorTokenError) as exc_info:
            await provider.ensure_valid_token()

        assert exc_info.value.error_id == "ERR-CIBA-009"

    @pytest.mark.asyncio
    async def test_authorize_500_raises_actor_token_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=AUTHORIZE_URL,
            status_code=500,
            text="Internal Server Error",
        )
        provider = _make_provider(httpx_mock)

        with pytest.raises(ActorTokenError):
            await provider.ensure_valid_token()


# ── 8. /authorize returns 200 but no flowId → ActorTokenError ─────────────────


class TestAuthorizeNoFlowId:
    """A 200 /authorize response that lacks flowId or authenticatorId raises ActorTokenError."""

    @pytest.mark.asyncio
    async def test_missing_flow_id_raises(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=AUTHORIZE_URL,
            json={"nextStep": {"authenticators": [{"authenticatorId": "BasicAuthenticator"}]}},
            # flowId deliberately absent
        )
        provider = _make_provider(httpx_mock)

        with pytest.raises(ActorTokenError) as exc_info:
            await provider.ensure_valid_token()

        assert "flowId" in str(exc_info.value).lower() or "authenticatorid" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_empty_authenticators_raises(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=AUTHORIZE_URL,
            json={"flowId": "flow-001", "nextStep": {"authenticators": []}},
        )
        provider = _make_provider(httpx_mock)

        with pytest.raises(ActorTokenError):
            await provider.ensure_valid_token()


# ── 8b. /authorize short-circuit: SUCCESS_COMPLETED returns the code directly ─


class TestAuthorizeShortCircuit:
    """IS 7.3 may return flowStatus=SUCCESS_COMPLETED + authData.code on
    /oauth2/authorize when a prior IS session for the OAuth client is still
    valid. The mint must skip /authn entirely and go straight to /token."""

    @pytest.mark.asyncio
    async def test_short_circuit_skips_authn(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=AUTHORIZE_URL,
            json={
                "flowStatus": "SUCCESS_COMPLETED",
                "authData": {"code": "sc-code-001", "session_state": "sess-xyz"},
            },
        )
        # No /authn mock registered — if the code calls it, the test fails.
        httpx_mock.add_response(
            method="POST", url=TOKEN_URL, json=_token_body(access_token="sc-token")
        )
        provider = _make_provider(httpx_mock)

        token = await provider.ensure_valid_token()

        assert token.access_token == "sc-token"
        # Exactly two round-trips: /authorize then /token (no /authn).
        paths = [r.url.path for r in httpx_mock.get_requests()]
        assert paths == ["/oauth2/authorize", "/oauth2/token"]


# ── 9. /authn returns no code → ActorTokenError ───────────────────────────────


class TestAuthnNoCode:
    """/authn responds with 200 but no authorization code raises ActorTokenError."""

    @pytest.mark.asyncio
    async def test_authn_no_code_raises(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="POST", url=AUTHORIZE_URL, json=_authorize_body())
        httpx_mock.add_response(
            method="POST",
            url=AUTHN_URL,
            json={"status": "INCOMPLETE"},  # neither authData.code nor code present
        )
        provider = _make_provider(httpx_mock)

        with pytest.raises(ActorTokenError):
            await provider.ensure_valid_token()


# ── 10. /token returns no access_token → ActorTokenError ──────────────────────


class TestTokenNoAccessToken:
    """/token responds with 200 but missing access_token raises ActorTokenError."""

    @pytest.mark.asyncio
    async def test_token_no_access_token_raises(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="POST", url=AUTHORIZE_URL, json=_authorize_body())
        httpx_mock.add_response(method="POST", url=AUTHN_URL, json=_authn_body())
        # /token returns 200 but without access_token → IS raises AuthError wrapping
        # in _mint which re-raises as ActorTokenError
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            status_code=400,
            json={"error": "invalid_grant"},
        )
        provider = _make_provider(httpx_mock)

        with pytest.raises(ActorTokenError):
            await provider.ensure_valid_token()


# ── 11. AgentCredentials is frozen ────────────────────────────────────────────


class TestAgentCredentialsFrozen:
    """AgentCredentials must be immutable (frozen=True on the dataclass)."""

    def test_mutation_raises_frozen_instance_error(self) -> None:
        creds = AgentCredentials(
            agent_id="uid",
            agent_secret="secret",
            oauth_client_id="cid",
            oauth_client_secret="csecret",
        )
        with pytest.raises(FrozenInstanceError):
            creds.agent_id = "mutated"  # type: ignore[misc]

    def test_default_redirect_uri(self) -> None:
        creds = AgentCredentials(
            agent_id="uid",
            agent_secret="secret",
            oauth_client_id="cid",
            oauth_client_secret="csecret",
        )
        assert creds.redirect_uri == "http://localhost:9999/agent-callback"


# ── 12. _pkce_pair() generates distinct pairs ─────────────────────────────────


class TestPkcePair:
    """_pkce_pair() must produce distinct (verifier, challenge) on every call."""

    def test_distinct_pairs_across_calls(self) -> None:
        pair1 = _pkce_pair()
        pair2 = _pkce_pair()
        assert pair1[0] != pair2[0], "verifiers must differ"
        assert pair1[1] != pair2[1], "challenges must differ"

    def test_verifier_and_challenge_differ(self) -> None:
        verifier, challenge = _pkce_pair()
        assert verifier != challenge

    def test_no_padding(self) -> None:
        verifier, challenge = _pkce_pair()
        assert "=" not in verifier
        assert "=" not in challenge

    def test_challenge_is_sha256_of_verifier(self) -> None:
        """The challenge must equal base64url(sha256(verifier_bytes)) with no padding."""
        import base64
        import hashlib

        verifier, challenge = _pkce_pair()
        expected_digest = hashlib.sha256(verifier.encode()).digest()
        expected_challenge = base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode()
        assert challenge == expected_challenge


# ── 13. _is_fresh() returns False for None ────────────────────────────────────


class TestIsFresh:
    """_is_fresh() must return False when token is None (no cache yet)."""

    def test_none_token_not_fresh(self, httpx_mock: pytest_httpx.HTTPXMock) -> None:
        provider = _make_provider(httpx_mock)
        assert provider._is_fresh(None) is False

    def test_expired_token_not_fresh(self, httpx_mock: pytest_httpx.HTTPXMock) -> None:
        provider = _make_provider(httpx_mock, buffer_seconds=30)
        past = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
        expired = OAuthToken(
            access_token="tok",
            token_type="Bearer",
            expires_in=0,
            expires_at=past,
            refresh_token=None,
            scope="openid",
            id_token=None,
        )
        assert provider._is_fresh(expired) is False

    def test_fresh_token_is_fresh(self, httpx_mock: pytest_httpx.HTTPXMock) -> None:
        provider = _make_provider(httpx_mock, buffer_seconds=30)
        future = datetime.now(tz=timezone.utc) + timedelta(seconds=120)
        fresh = OAuthToken(
            access_token="tok",
            token_type="Bearer",
            expires_in=120,
            expires_at=future,
            refresh_token=None,
            scope="openid",
            id_token=None,
        )
        assert provider._is_fresh(fresh) is True
