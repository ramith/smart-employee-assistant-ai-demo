"""Tests for common/auth/jwt_validator.py — Sprint 1 Wave 2.

Requirements: PyJWT[cryptography]>=2.12.0, httpx, pytest, pytest-asyncio.
These are already listed in hr_agent/requirements.txt (PyJWT[crypto]>=2.12.0, httpx).

Test rig
--------
A 2048-bit RSA keypair is generated once per test session via the ``rsa_keypair``
session fixture.  ``JWKSCache.get_key`` is patched with ``unittest.mock.AsyncMock``
so no real HTTP calls are made.  Each test builds its own JWT signed with the
private key and validates against the matching public JWK.

Test count: 14 tests (>= 12 required).

Catalog:
    T01  valid JWT with all claims → returns JWTClaims
    T02  bad signature → JWTValidationError(error_id=ERR-AUTH-006)
    T03  wrong issuer → JWTValidationError; details has actual_iss/expected_iss
    T04  expired token (exp < now - leeway) → JWTValidationError(ERR-AUTH-008)
    T05  token expires within leeway → accepted (leeway intent)
    T06  missing jti → JWTValidationError(ERR-AUTH-010)
    T07  empty-string jti → JWTValidationError(ERR-AUTH-010)
    T08  wrong aud (string shape) → JWTValidationError(ERR-MCP-001)
    T09  wrong aud (list shape) → JWTValidationError(ERR-MCP-001)
    T10  aud check skipped when config.expected_aud is None
    T11  missing required scope → JWTValidationError(ERR-MCP-003)
    T12  all required scopes present → no error
    T13  JWKS cache hit on second validate() call — get_key called only once
    T14  unknown kid (cache miss after refresh) → JWTValidationError(ERR-AUTH-006)
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import jwt as pyjwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from jwt.algorithms import RSAAlgorithm

# ---------------------------------------------------------------------------
# Module loading (mirrors conftest.py pattern — load without __init__.py)
# ---------------------------------------------------------------------------
import importlib.util, pathlib, sys, types as _types

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent

def _load(dotted: str, rel: str) -> _types.ModuleType:
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, _ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


for _pkg in ("common", "common.auth"):
    if _pkg not in sys.modules:
        _stub = _types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        _stub.__path__ = [str(_ROOT / _pkg.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub

_errors = _load("common.auth.errors", "common/auth/errors.py")
_models = _load("common.auth.models", "common/auth/models.py")
_validator = _load("common.auth.jwt_validator", "common/auth/jwt_validator.py")

ValidatorConfig: type = _validator.ValidatorConfig
JWKSCache: type = _validator.JWKSCache
validate = _validator.validate
JWTValidationError: type = _errors.JWTValidationError
JWTClaims: type = _models.JWTClaims

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

ISSUER = "https://api.asgardeo.io/t/ddademo/oauth2/token"
AUDIENCE = "hr_agent-client-id"
SUBJECT = "user-uuid-abc123"
JTI = "jti-test-001"


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[Any, Any]:
    """Generate a 2048-bit RSA keypair once per session.

    Returns:
        (private_key, public_jwk_dict) where public_jwk_dict is suitable for
        ``jwt.algorithms.RSAAlgorithm.from_jwk()``.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    public_jwk_dict: dict[str, Any] = json.loads(
        RSAAlgorithm.to_jwk(private_key.public_key())
    )
    public_jwk_dict["kid"] = "test-key-1"
    public_jwk_dict["use"] = "sig"
    public_jwk_dict["alg"] = "RS256"
    return private_key, public_jwk_dict


@pytest.fixture(scope="session")
def sign_token(rsa_keypair):
    """Return a factory that signs JWT payloads with the session private key."""
    private_key, _ = rsa_keypair

    def _sign(
        payload: dict[str, Any],
        kid: str = "test-key-1",
    ) -> str:
        headers = {"kid": kid}
        return pyjwt.encode(payload, private_key, algorithm="RS256", headers=headers)

    return _sign


@pytest.fixture
def base_payload() -> dict[str, Any]:
    """A valid JWT payload with all required claims."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": AUDIENCE,
        "exp": now + 300,
        "iat": now,
        "jti": JTI,
        "scope": "openid hr.read",
    }


@pytest.fixture
def base_config() -> Any:
    return ValidatorConfig(
        expected_iss=ISSUER,
        jwks_url="https://api.asgardeo.io/.well-known/jwks",
        expected_aud=AUDIENCE,
        required_scopes=frozenset(),
        leeway_seconds=30,
    )


def _make_mock_cache(public_jwk: dict[str, Any]) -> Any:
    """Return a JWKSCache whose get_key is mocked to return the given public JWK."""
    cache = JWKSCache(jwks_url="https://mock-jwks/", insecure_tls=True)
    cache.get_key = AsyncMock(return_value=public_jwk)  # type: ignore[method-assign]
    return cache


# ---------------------------------------------------------------------------
# T01 — valid JWT returns JWTClaims
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_jwt_returns_jwt_claims(base_payload, base_config, rsa_keypair, sign_token):
    """T01: A valid JWT signed with the correct key returns a populated JWTClaims."""
    _, public_jwk = rsa_keypair
    token = sign_token(base_payload)
    cache = _make_mock_cache(public_jwk)

    claims = await validate(token, base_config, jwks_cache=cache)

    assert isinstance(claims, JWTClaims)
    assert claims.sub == SUBJECT
    assert claims.iss == ISSUER
    assert claims.jti == JTI
    assert claims.scope == "openid hr.read"


# ---------------------------------------------------------------------------
# T02 — bad signature → ERR-AUTH-006
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bad_signature_raises_err_auth_006(base_payload, base_config, sign_token):
    """T02: A token signed with a different (wrong) key raises JWTValidationError(ERR-AUTH-006)."""
    # Generate a second keypair; sign with it but provide the first public key for verification.
    other_private = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    other_public_jwk: dict[str, Any] = json.loads(
        RSAAlgorithm.to_jwk(other_private.public_key())
    )
    other_public_jwk["kid"] = "test-key-1"

    token = sign_token(base_payload)  # signed with session key
    cache = _make_mock_cache(other_public_jwk)  # but verified with the OTHER public key

    with pytest.raises(JWTValidationError) as exc_info:
        await validate(token, base_config, jwks_cache=cache)

    assert exc_info.value.error_id == "ERR-AUTH-006"


# ---------------------------------------------------------------------------
# T03 — wrong issuer → ERR-AUTH-007, details has actual_iss/expected_iss
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_issuer_raises_with_details(base_payload, base_config, rsa_keypair, sign_token):
    """T03: Token with wrong iss raises JWTValidationError with actual_iss and expected_iss in details."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, iss="https://evil.example.com/oauth2/token")
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    with pytest.raises(JWTValidationError) as exc_info:
        await validate(token, base_config, jwks_cache=cache)

    err = exc_info.value
    assert err.error_id == "ERR-AUTH-007"
    assert "actual_iss" in err.details
    assert err.details["actual_iss"] == "https://evil.example.com/oauth2/token"
    assert "expected_iss" in err.details
    assert err.details["expected_iss"] == ISSUER


# ---------------------------------------------------------------------------
# T04 — expired token (outside leeway) → ERR-AUTH-008
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expired_token_raises_err_auth_008(base_payload, base_config, rsa_keypair, sign_token):
    """T04: Token whose exp is well before now-leeway raises JWTValidationError(ERR-AUTH-008)."""
    _, public_jwk = rsa_keypair
    now = int(time.time())
    payload = dict(base_payload, exp=now - 300, iat=now - 600)  # expired 300s ago, leeway=30s
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    with pytest.raises(JWTValidationError) as exc_info:
        await validate(token, base_config, jwks_cache=cache)

    assert exc_info.value.error_id == "ERR-AUTH-008"


# ---------------------------------------------------------------------------
# T05 — token expires within leeway → accepted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_within_leeway_is_accepted(base_payload, base_config, rsa_keypair, sign_token):
    """T05: Token with exp 10s in the past (within leeway=30s) must be accepted."""
    _, public_jwk = rsa_keypair
    now = int(time.time())
    # exp is 10s in the past, but leeway is 30s — should pass
    payload = dict(base_payload, exp=now - 10, iat=now - 320)
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    # Must not raise
    claims = await validate(token, base_config, jwks_cache=cache)
    assert claims.sub == SUBJECT


# ---------------------------------------------------------------------------
# T06 — missing jti → ERR-AUTH-010
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_jti_raises_err_auth_010(base_payload, base_config, rsa_keypair, sign_token):
    """T06: Token without a jti claim raises JWTValidationError(ERR-AUTH-010)."""
    _, public_jwk = rsa_keypair
    payload = {k: v for k, v in base_payload.items() if k != "jti"}
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    with pytest.raises(JWTValidationError) as exc_info:
        await validate(token, base_config, jwks_cache=cache)

    assert exc_info.value.error_id == "ERR-AUTH-010"


# ---------------------------------------------------------------------------
# T07 — empty-string jti → ERR-AUTH-010
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_jti_raises_err_auth_010(base_payload, base_config, rsa_keypair, sign_token):
    """T07: Token with jti='' raises JWTValidationError(ERR-AUTH-010)."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, jti="")
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    with pytest.raises(JWTValidationError) as exc_info:
        await validate(token, base_config, jwks_cache=cache)

    assert exc_info.value.error_id == "ERR-AUTH-010"


# ---------------------------------------------------------------------------
# T08 — wrong aud (string shape) → ERR-MCP-001
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_aud_string_raises_err_mcp_001(base_payload, base_config, rsa_keypair, sign_token):
    """T08: Token with aud as a wrong string raises JWTValidationError(ERR-MCP-001)."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, aud="some-other-client-id")
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    with pytest.raises(JWTValidationError) as exc_info:
        await validate(token, base_config, jwks_cache=cache)

    err = exc_info.value
    assert err.error_id == "ERR-MCP-001"
    assert err.details.get("expected_aud") == AUDIENCE


# ---------------------------------------------------------------------------
# T09 — wrong aud (list shape) → ERR-MCP-001
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_aud_list_raises_err_mcp_001(base_payload, base_config, rsa_keypair, sign_token):
    """T09: Token with aud as a list that doesn't contain expected_aud raises ERR-MCP-001."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, aud=["other-client-1", "other-client-2"])
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    with pytest.raises(JWTValidationError) as exc_info:
        await validate(token, base_config, jwks_cache=cache)

    assert exc_info.value.error_id == "ERR-MCP-001"


# ---------------------------------------------------------------------------
# T10 — aud check skipped when config.expected_aud is None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aud_skipped_when_config_expected_aud_is_none(base_payload, rsa_keypair, sign_token):
    """T10: When config.expected_aud is None, any aud (or no aud) is accepted."""
    _, public_jwk = rsa_keypair
    config_no_aud = ValidatorConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=None,        # skip aud check
        required_scopes=frozenset(),
    )
    payload = dict(base_payload, aud="completely-different-audience")
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    claims = await validate(token, config_no_aud, jwks_cache=cache)
    assert claims.sub == SUBJECT  # validation succeeded


# ---------------------------------------------------------------------------
# T11 — missing required scope → ERR-MCP-003
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_required_scope_raises_err_mcp_003(base_payload, rsa_keypair, sign_token):
    """T11: When required_scopes={hr.write} but token has only hr.read, raise ERR-MCP-003."""
    _, public_jwk = rsa_keypair
    config_strict = ValidatorConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=AUDIENCE,
        required_scopes=frozenset({"hr.read", "hr.write"}),
    )
    payload = dict(base_payload, scope="openid hr.read")  # hr.write missing
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    with pytest.raises(JWTValidationError) as exc_info:
        await validate(token, config_strict, jwks_cache=cache)

    err = exc_info.value
    assert err.error_id == "ERR-MCP-003"
    assert "hr.write" in err.details.get("missing", [])


# ---------------------------------------------------------------------------
# T12 — all required scopes present → no error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_required_scopes_present_succeeds(base_payload, rsa_keypair, sign_token):
    """T12: Token with scope='openid hr.read hr.write' satisfies required={hr.read, hr.write}."""
    _, public_jwk = rsa_keypair
    config_strict = ValidatorConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=AUDIENCE,
        required_scopes=frozenset({"hr.read", "hr.write"}),
    )
    payload = dict(base_payload, scope="openid hr.read hr.write")
    token = sign_token(payload)
    cache = _make_mock_cache(public_jwk)

    claims = await validate(token, config_strict, jwks_cache=cache)
    assert claims.scope == "openid hr.read hr.write"


# ---------------------------------------------------------------------------
# T13 — JWKS cache hit on second validate() call (no second HTTP fetch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jwks_cache_hit_on_second_call(base_payload, base_config, rsa_keypair, sign_token):
    """T13: Calling validate() twice with the same JWKSCache instance uses get_key once per call,
    but the underlying HTTP fetch (refresh()) happens at most once (TTL not yet expired)."""
    _, public_jwk = rsa_keypair
    token = sign_token(base_payload)
    cache = _make_mock_cache(public_jwk)
    # Override with a real JWKSCache that tracks refresh() calls
    real_cache = JWKSCache(jwks_url="https://mock/", insecure_tls=True)
    real_cache._keys = {"test-key-1": public_jwk}
    real_cache._fetched_at = time.monotonic()  # mark as fresh

    refresh_mock = AsyncMock()
    real_cache.refresh = refresh_mock  # type: ignore[method-assign]

    await validate(token, base_config, jwks_cache=real_cache)
    await validate(token, base_config, jwks_cache=real_cache)

    # Cache was populated and not stale, so refresh() should NOT have been called
    refresh_mock.assert_not_called()


# ---------------------------------------------------------------------------
# T14 — unknown kid after forced refresh → ERR-AUTH-006
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_kid_raises_err_auth_006(base_payload, base_config, rsa_keypair, sign_token):
    """T14: Token with a kid not in JWKS (even after forced refresh) raises ERR-AUTH-006."""
    _, public_jwk = rsa_keypair
    token = sign_token(base_payload, kid="test-key-1")

    # Cache that never knows about "test-key-1"
    empty_cache = JWKSCache(jwks_url="https://mock/", insecure_tls=True)
    empty_cache._keys = {}
    empty_cache._fetched_at = time.monotonic()  # mark as fresh, so first stale check passes

    # After kid-miss the cache does one forced refresh — mock that to also return nothing
    async def _empty_refresh() -> None:
        empty_cache._keys = {}
        empty_cache._fetched_at = time.monotonic()

    empty_cache.refresh = _empty_refresh  # type: ignore[method-assign]

    with pytest.raises(JWTValidationError) as exc_info:
        await validate(token, base_config, jwks_cache=empty_cache)

    assert exc_info.value.error_id == "ERR-AUTH-006"
    assert "kid" in exc_info.value.details


# ---------------------------------------------------------------------------
# Sprint 4 (security audit F-03): identity claim sanitisation
# ---------------------------------------------------------------------------

from common.auth.jwt_validator import _sanitise_user_string  # noqa: E402


class TestSanitiseUserString:
    def test_none_returns_none(self) -> None:
        assert _sanitise_user_string(None, max_len=64) is None

    def test_non_string_returns_none(self) -> None:
        assert _sanitise_user_string(42, max_len=64) is None
        assert _sanitise_user_string({"x": 1}, max_len=64) is None

    def test_empty_string_returns_none(self) -> None:
        assert _sanitise_user_string("", max_len=64) is None
        assert _sanitise_user_string("   ", max_len=64) is None

    def test_clean_string_passes_through(self) -> None:
        assert _sanitise_user_string("jane.doe", max_len=64) == "jane.doe"

    def test_strips_control_chars(self) -> None:
        assert _sanitise_user_string("jane\x00doe\x07", max_len=64) == "janedoe"

    def test_strips_newlines_and_carriage_returns(self) -> None:
        assert _sanitise_user_string("jane\ndoe\r\n", max_len=64) == "janedoe"

    def test_strips_unicode_line_separators(self) -> None:
        assert _sanitise_user_string("jane doe ", max_len=64) == "janedoe"

    def test_caps_to_max_len(self) -> None:
        long = "a" * 100
        assert _sanitise_user_string(long, max_len=64) == "a" * 64

    def test_email_max_len_256(self) -> None:
        long_email = ("x" * 250) + "@e.com"
        result = _sanitise_user_string(long_email, max_len=256)
        assert result is not None
        assert len(result) == 256

