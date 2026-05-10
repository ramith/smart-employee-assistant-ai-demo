"""Tests for orchestrator/auth/pattern_c.py — Wave 5, Sprint 1.

Coverage targets
----------------
 1. ``make_pkce()`` returns a 43-char verifier + correctly computed S256 challenge.
 2. ``make_pkce()`` returns different values on successive calls (entropy check).
 3. ``build_authorize_url()`` includes all required PKCE + state + requested_actor
    parameters, correctly URL-encoded.
 4. ``exchange()`` happy-path: actor_token_provider returns a mock token, IS returns
    a valid token-A, ``validate()`` passes → ``PatternCResult`` with expected fields.
 5. ``exchange()`` IS returns HTTP 4xx → ``AuthError`` propagates to caller.
 6. ``exchange()`` IS returns a valid token but ``validate()`` raises
    ``JWTValidationError`` → exception propagates unchanged.
 7. ``exchange()`` places ``actor_token`` in the POST **body**, not in an
    Authorization header — verified by inspecting the captured httpx request.
 8. ``exchange()`` calls ``actor_token_provider.ensure_valid_token()`` exactly once
    per invocation.
 9. Multiple concurrent ``exchange()`` calls each invoke
    ``actor_token_provider.ensure_valid_token()`` independently — no local caching.
10. The ``claims.act["sub"]`` in a successful ``PatternCResult`` matches the
    orchestrator-agent UUID that the mocked actor_token carries.

Style: Python 3.11+, type hints, async, stdlib + common.* imports only.
Isolation: all IS HTTP calls are replaced by ``unittest.mock`` / ``AsyncMock``
           (no live network); ``validate()`` is patched at the module level.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import pathlib
import secrets
import sys
import types
import urllib.parse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module isolation bootstrap
# ---------------------------------------------------------------------------
# We load each module under test directly from its source file, bypassing the
# package __init__.py files that may not yet be complete.  This mirrors the
# approach used in tests/orchestrator/agent_registry/test_cards.py.

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str) -> None:
    """Register a bare package namespace stub in sys.modules if absent."""
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    stub.__path__ = [str(_ROOT / dotted_name.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules under *dotted_name*."""
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


# ---------------------------------------------------------------------------
# Third-party stubs
# ---------------------------------------------------------------------------
# PyJWT is declared in orchestrator/requirements.txt but may not be installed in
# the current test runner's environment.  jwt_validator.py has a hard top-level
# ``import jwt`` + ``from jwt.algorithms import RSAAlgorithm``.  We pre-register
# minimal stubs so the module can be imported; the validate() function is always
# patched in these tests so the real JWT machinery is never exercised.

def _stub_jwt_if_missing() -> None:
    """Register a minimal jwt stub so jwt_validator.py can be imported."""
    if "jwt" in sys.modules:
        return

    jwt_stub = types.ModuleType("jwt")

    # Minimal exception hierarchy expected by jwt_validator.py
    class _JWTError(Exception):
        pass

    class InvalidTokenError(_JWTError):
        pass

    class DecodeError(InvalidTokenError):
        pass

    class InvalidSignatureError(InvalidTokenError):
        pass

    class ExpiredSignatureError(InvalidTokenError):
        pass

    class InvalidIssuerError(InvalidTokenError):
        pass

    jwt_stub.exceptions = types.SimpleNamespace(
        DecodeError=DecodeError,
        InvalidTokenError=InvalidTokenError,
    )
    jwt_stub.ExpiredSignatureError = ExpiredSignatureError
    jwt_stub.InvalidIssuerError = InvalidIssuerError
    jwt_stub.InvalidSignatureError = InvalidSignatureError
    jwt_stub.DecodeError = DecodeError
    jwt_stub.InvalidTokenError = InvalidTokenError

    def _noop_decode(*args: object, **kwargs: object) -> dict:  # type: ignore[return]
        return {}

    def _noop_get_unverified_header(token: str) -> dict:
        return {}

    jwt_stub.decode = _noop_decode
    jwt_stub.get_unverified_header = _noop_get_unverified_header

    # algorithms sub-module
    alg_stub = types.ModuleType("jwt.algorithms")

    class _RSAAlgorithmStub:
        @staticmethod
        def from_jwk(jwk_dict: object) -> object:
            return object()

    alg_stub.RSAAlgorithm = _RSAAlgorithmStub
    jwt_stub.algorithms = alg_stub

    sys.modules["jwt"] = jwt_stub
    sys.modules["jwt.algorithms"] = alg_stub


_stub_jwt_if_missing()

# Ensure every intermediate package namespace exists.
# Note: only register package stubs here (directories with __init__.py), NOT leaf
# modules.  Leaf modules (common.auth.errors, common.auth.models, etc.) are loaded
# below via _load_module() so they get their real content.
for _pkg in (
    "common",
    "common.auth",
    "orchestrator",
    "orchestrator.auth",
):
    _ensure_pkg(_pkg)

# Load leaf modules in dependency order.
_errors_mod = _load_module("common.auth.errors", "common/auth/errors.py")
_models_mod = _load_module("common.auth.models", "common/auth/models.py")
_jwt_validator_mod = _load_module("common.auth.jwt_validator", "common/auth/jwt_validator.py")
_wso2_client_mod = _load_module("common.auth.wso2_is_client", "common/auth/wso2_is_client.py")
_actor_provider_mod = _load_module(
    "common.auth.actor_token_provider", "common/auth/actor_token_provider.py"
)
_pattern_c_mod = _load_module(
    "orchestrator.auth.pattern_c", "orchestrator/auth/pattern_c.py"
)

# Extract symbols under test.
make_pkce: "Callable[[], tuple[str, str]]" = _pattern_c_mod.make_pkce
build_authorize_url = _pattern_c_mod.build_authorize_url
PatternCExchanger = _pattern_c_mod.PatternCExchanger
PatternCResult = _pattern_c_mod.PatternCResult

OAuthToken = _models_mod.OAuthToken
JWTClaims = _models_mod.JWTClaims
AuthError = _errors_mod.AuthError
JWTValidationError = _errors_mod.JWTValidationError
ValidatorConfig = _jwt_validator_mod.ValidatorConfig
JWKSCache = _jwt_validator_mod.JWKSCache

# ---------------------------------------------------------------------------
# Shared fixtures and factories
# ---------------------------------------------------------------------------

_ORCHESTRATOR_AGENT_ID = "orch-agent-uuid-0001"
_MCP_CLIENT_ID = "orchestrator-mcp-client-id"
_MCP_CLIENT_SECRET = "super-secret"
_IS_BASE = "https://is.example.com"
_AUTHORIZE_ENDPOINT = f"{_IS_BASE}/oauth2/authorize"
_SPA_CLIENT_ID = "orchestrator-app-id"
_REDIRECT_URI = "http://localhost:3001/callback"
_SCOPE = "openid orchestrate"


def _make_oauth_token(access_token: str = "actor.access.token") -> OAuthToken:
    """Build a minimal OAuthToken for use as an actor_token mock."""
    from datetime import datetime, timedelta, timezone

    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=3600,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(seconds=3600),
        refresh_token=None,
        scope="openid internal_login",
        id_token=None,
    )


def _make_token_a(access_token: str = "token-a.jwt.string") -> OAuthToken:
    """Build a minimal OAuthToken representing token-A returned by IS."""
    from datetime import datetime, timedelta, timezone

    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=3600,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(seconds=3600),
        refresh_token=None,
        scope="openid orchestrate",
        id_token=None,
    )


def _make_jwt_claims(act_sub: str = _ORCHESTRATOR_AGENT_ID) -> JWTClaims:
    """Build a minimal JWTClaims representing a validated token-A."""
    return JWTClaims(
        sub="user-uuid-1234",
        iss=f"{_IS_BASE}/oauth2/token",
        aud=_SPA_CLIENT_ID,
        exp=9999999999,
        iat=1700000000,
        jti="jti-unique-abc123",
        act={"sub": act_sub},
        scope="openid orchestrate",
        aut="APPLICATION_USER",
    )


def _make_validator() -> ValidatorConfig:
    return ValidatorConfig(
        expected_iss=f"{_IS_BASE}/oauth2/token",
        jwks_url=f"{_IS_BASE}/oauth2/jwks",
        expected_aud=_SPA_CLIENT_ID,
    )


def _make_jwks_cache() -> JWKSCache:
    return JWKSCache(jwks_url=f"{_IS_BASE}/oauth2/jwks")


def _make_exchanger(
    *,
    actor_token_provider: MagicMock | None = None,
    is_client: MagicMock | None = None,
    jwks_cache: JWKSCache | None = None,
) -> PatternCExchanger:
    """Factory that wires up a PatternCExchanger with mocked dependencies."""
    if actor_token_provider is None:
        actor_token_provider = MagicMock()
        actor_token_provider.ensure_valid_token = AsyncMock(
            return_value=_make_oauth_token()
        )
    if is_client is None:
        is_client = MagicMock()
        is_client.exchange_code = AsyncMock(return_value=_make_token_a())
    return PatternCExchanger(
        is_client=is_client,
        actor_token_provider=actor_token_provider,
        mcp_client_id=_MCP_CLIENT_ID,
        mcp_client_secret=_MCP_CLIENT_SECRET,
        validator=_make_validator(),
        jwks_cache=jwks_cache or _make_jwks_cache(),
    )


# ---------------------------------------------------------------------------
# Test 1 — make_pkce() returns a 43-char verifier + correct S256 challenge
# ---------------------------------------------------------------------------


def test_make_pkce_verifier_length_and_challenge() -> None:
    """make_pkce() must return a 43-char verifier and a matching S256 challenge."""
    verifier, challenge = make_pkce()

    # RFC 7636 §4.1: verifier is base64url(32 bytes) → exactly 43 chars (no padding)
    assert len(verifier) == 43, f"Expected 43-char verifier, got {len(verifier)}"

    # Challenge must be S256(verifier): SHA-256 → base64url (no padding)
    expected_digest = hashlib.sha256(verifier.encode()).digest()
    expected_challenge = base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode()
    assert challenge == expected_challenge, (
        f"Challenge mismatch: expected {expected_challenge!r}, got {challenge!r}"
    )

    # Both must be ASCII strings with only URL-safe base64 characters.
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in verifier)
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in challenge)


# ---------------------------------------------------------------------------
# Test 2 — make_pkce() returns different values each call
# ---------------------------------------------------------------------------


def test_make_pkce_returns_different_values_each_call() -> None:
    """Successive calls to make_pkce() must produce different verifiers and challenges."""
    pairs = [make_pkce() for _ in range(5)]
    verifiers = [p[0] for p in pairs]
    challenges = [p[1] for p in pairs]

    # All verifiers must be distinct (cryptographically independent)
    assert len(set(verifiers)) == 5, "make_pkce() returned duplicate verifiers"
    assert len(set(challenges)) == 5, "make_pkce() returned duplicate challenges"


# ---------------------------------------------------------------------------
# Test 3 — build_authorize_url() includes all required parameters
# ---------------------------------------------------------------------------


def test_build_authorize_url_contains_all_required_params() -> None:
    """build_authorize_url() must embed all PKCE + state + requested_actor params."""
    verifier, _ = make_pkce()
    state = secrets.token_urlsafe(16)

    url, code_challenge = build_authorize_url(
        is_authorize_endpoint=_AUTHORIZE_ENDPOINT,
        client_id=_SPA_CLIENT_ID,
        redirect_uri=_REDIRECT_URI,
        scope=_SCOPE,
        requested_actor=_ORCHESTRATOR_AGENT_ID,
        state=state,
        code_verifier=verifier,
    )

    # URL must start with the IS authorize endpoint
    assert url.startswith(_AUTHORIZE_ENDPOINT + "?"), (
        f"URL does not start with expected endpoint: {url}"
    )

    # Parse query string
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))

    assert params["client_id"] == _SPA_CLIENT_ID
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == _REDIRECT_URI
    assert params["scope"] == _SCOPE
    assert params["state"] == state
    assert params["code_challenge_method"] == "S256"
    assert params["requested_actor"] == _ORCHESTRATOR_AGENT_ID

    # code_challenge in URL must match the returned value and be S256(verifier)
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert params["code_challenge"] == expected_challenge
    assert code_challenge == expected_challenge


def test_build_authorize_url_encodes_special_chars() -> None:
    """Spaces and special chars in scope and redirect_uri must be percent-encoded."""
    verifier, _ = make_pkce()
    url, _ = build_authorize_url(
        is_authorize_endpoint=_AUTHORIZE_ENDPOINT,
        client_id=_SPA_CLIENT_ID,
        redirect_uri="http://localhost:3001/call back",  # space in path (edge case)
        scope="openid orchestrate profile",
        requested_actor=_ORCHESTRATOR_AGENT_ID,
        state="state-value",
        code_verifier=verifier,
    )
    # The raw space must not appear literally in the URL
    assert " " not in url, "URL contains unencoded space"


# ---------------------------------------------------------------------------
# Test 4 — exchange() happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_happy_path() -> None:
    """exchange() must return a PatternCResult with token_a and verified claims."""
    actor_mock = MagicMock()
    actor_mock.ensure_valid_token = AsyncMock(return_value=_make_oauth_token())

    is_mock = MagicMock()
    token_a = _make_token_a("token-a.access")
    is_mock.exchange_code = AsyncMock(return_value=token_a)

    expected_claims = _make_jwt_claims()

    exchanger = _make_exchanger(
        actor_token_provider=actor_mock,
        is_client=is_mock,
    )

    with patch.object(
        sys.modules["orchestrator.auth.pattern_c"],
        "validate",
        new=AsyncMock(return_value=expected_claims),
    ):
        result = await exchanger.exchange(
            code="auth-code-xyz",
            code_verifier="verifier-abc",
            redirect_uri=_REDIRECT_URI,
        )

    assert isinstance(result, PatternCResult)
    assert result.token_a is token_a
    assert result.claims is expected_claims
    assert result.claims.sub == "user-uuid-1234"
    assert result.claims.act is not None
    assert result.claims.act["sub"] == _ORCHESTRATOR_AGENT_ID


# ---------------------------------------------------------------------------
# Test 5 — exchange() IS 4xx → AuthError raised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_is_4xx_raises_auth_error() -> None:
    """exchange() must propagate AuthError when IS returns a 4xx on the token exchange."""
    actor_mock = MagicMock()
    actor_mock.ensure_valid_token = AsyncMock(return_value=_make_oauth_token())

    is_mock = MagicMock()
    is_mock.exchange_code = AsyncMock(
        side_effect=AuthError("POST /oauth2/token returned HTTP 400: invalid_grant")
    )

    exchanger = _make_exchanger(
        actor_token_provider=actor_mock,
        is_client=is_mock,
    )

    with pytest.raises(AuthError, match="invalid_grant"):
        await exchanger.exchange(
            code="expired-code",
            code_verifier="verifier-abc",
            redirect_uri=_REDIRECT_URI,
        )


# ---------------------------------------------------------------------------
# Test 6 — exchange() JWT validation failure propagates JWTValidationError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_jwt_validation_failure_propagates() -> None:
    """exchange() must propagate JWTValidationError when signature validation fails."""
    actor_mock = MagicMock()
    actor_mock.ensure_valid_token = AsyncMock(return_value=_make_oauth_token())

    is_mock = MagicMock()
    is_mock.exchange_code = AsyncMock(return_value=_make_token_a())

    exchanger = _make_exchanger(
        actor_token_provider=actor_mock,
        is_client=is_mock,
    )

    with patch.object(
        sys.modules["orchestrator.auth.pattern_c"],
        "validate",
        new=AsyncMock(
            side_effect=JWTValidationError(
                "Invalid signature: bad key", error_id="ERR-AUTH-006"
            )
        ),
    ):
        with pytest.raises(JWTValidationError, match="Invalid signature"):
            await exchanger.exchange(
                code="auth-code-xyz",
                code_verifier="verifier-abc",
                redirect_uri=_REDIRECT_URI,
            )


# ---------------------------------------------------------------------------
# Test 7 — actor_token in POST body, NOT Authorization header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_actor_token_is_in_body_not_header() -> None:
    """actor_token must appear in the POST body form data, not in any HTTP header.

    We verify this by inspecting the kwargs passed to is_client.exchange_code().
    ``WSO2ISClient.exchange_code`` places ``actor_token`` in the form body when
    the kwarg is present (verified in C1 probe / F-01).  We confirm PatternCExchanger
    passes the kwarg — and does NOT set any authorization header override.
    """
    actor_access_token = "orch-agent-i4-token-abc"
    actor_mock = MagicMock()
    actor_mock.ensure_valid_token = AsyncMock(
        return_value=_make_oauth_token(access_token=actor_access_token)
    )

    is_mock = MagicMock()
    is_mock.exchange_code = AsyncMock(return_value=_make_token_a())

    exchanger = _make_exchanger(
        actor_token_provider=actor_mock,
        is_client=is_mock,
    )

    with patch.object(
        sys.modules["orchestrator.auth.pattern_c"],
        "validate",
        new=AsyncMock(return_value=_make_jwt_claims()),
    ):
        await exchanger.exchange(
            code="code-abc",
            code_verifier="verifier-xyz",
            redirect_uri=_REDIRECT_URI,
        )

    # exchange_code must have been called with actor_token= kwarg (body field, per F-01)
    is_mock.exchange_code.assert_awaited_once()
    call_kwargs = is_mock.exchange_code.call_args.kwargs
    assert call_kwargs.get("actor_token") == actor_access_token, (
        f"actor_token not passed as body kwarg; got call_kwargs={call_kwargs}"
    )

    # The IS client's exchange_code method signature does NOT accept
    # an 'authorization_header' parameter — confirm no such kwarg is passed.
    assert "authorization_header" not in call_kwargs
    assert "headers" not in call_kwargs


# ---------------------------------------------------------------------------
# Test 8 — ensure_valid_token() called exactly once per exchange() invocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_calls_ensure_valid_token_exactly_once() -> None:
    """exchange() must call actor_token_provider.ensure_valid_token() exactly once."""
    actor_mock = MagicMock()
    actor_mock.ensure_valid_token = AsyncMock(return_value=_make_oauth_token())

    is_mock = MagicMock()
    is_mock.exchange_code = AsyncMock(return_value=_make_token_a())

    exchanger = _make_exchanger(
        actor_token_provider=actor_mock,
        is_client=is_mock,
    )

    with patch.object(
        sys.modules["orchestrator.auth.pattern_c"],
        "validate",
        new=AsyncMock(return_value=_make_jwt_claims()),
    ):
        await exchanger.exchange(
            code="code-abc",
            code_verifier="verifier-xyz",
            redirect_uri=_REDIRECT_URI,
        )

    actor_mock.ensure_valid_token.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 9 — concurrent exchange() calls each invoke ensure_valid_token() once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_exchange_calls_each_invoke_ensure_valid_token() -> None:
    """Each concurrent exchange() call must invoke ensure_valid_token() independently.

    PatternCExchanger does NOT cache actor tokens locally — that is entirely the
    ActorTokenProvider's job.  Three concurrent exchanges should therefore produce
    three calls to ensure_valid_token().
    """
    import asyncio

    call_count = 0

    async def counting_ensure_valid_token() -> OAuthToken:
        nonlocal call_count
        call_count += 1
        return _make_oauth_token()

    actor_mock = MagicMock()
    actor_mock.ensure_valid_token = AsyncMock(side_effect=counting_ensure_valid_token)

    is_mock = MagicMock()
    is_mock.exchange_code = AsyncMock(return_value=_make_token_a())

    exchanger = _make_exchanger(
        actor_token_provider=actor_mock,
        is_client=is_mock,
    )

    with patch.object(
        sys.modules["orchestrator.auth.pattern_c"],
        "validate",
        new=AsyncMock(return_value=_make_jwt_claims()),
    ):
        await asyncio.gather(
            exchanger.exchange(code="code-1", code_verifier="v1", redirect_uri=_REDIRECT_URI),
            exchanger.exchange(code="code-2", code_verifier="v2", redirect_uri=_REDIRECT_URI),
            exchanger.exchange(code="code-3", code_verifier="v3", redirect_uri=_REDIRECT_URI),
        )

    assert call_count == 3, (
        f"Expected ensure_valid_token() to be called 3 times; got {call_count}"
    )


# ---------------------------------------------------------------------------
# Test 10 — claims.act["sub"] matches the orchestrator-agent UUID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claims_act_sub_matches_orchestrator_agent_uuid() -> None:
    """claims.act['sub'] in PatternCResult must match the mocked orchestrator-agent UUID."""
    specific_agent_id = "orchestrator-agent-uuid-specific-9999"

    actor_token = _make_oauth_token(access_token=f"i4-token-for-{specific_agent_id}")
    actor_mock = MagicMock()
    actor_mock.ensure_valid_token = AsyncMock(return_value=actor_token)

    is_mock = MagicMock()
    is_mock.exchange_code = AsyncMock(return_value=_make_token_a())

    # Build claims whose act.sub == specific_agent_id, as IS would set it.
    expected_claims = _make_jwt_claims(act_sub=specific_agent_id)

    exchanger = _make_exchanger(
        actor_token_provider=actor_mock,
        is_client=is_mock,
    )

    with patch.object(
        sys.modules["orchestrator.auth.pattern_c"],
        "validate",
        new=AsyncMock(return_value=expected_claims),
    ):
        result = await exchanger.exchange(
            code="code-abc",
            code_verifier="verifier-xyz",
            redirect_uri=_REDIRECT_URI,
        )

    assert result.claims.act is not None
    assert result.claims.act["sub"] == specific_agent_id, (
        f"Expected act.sub={specific_agent_id!r}, got {result.claims.act['sub']!r}"
    )
