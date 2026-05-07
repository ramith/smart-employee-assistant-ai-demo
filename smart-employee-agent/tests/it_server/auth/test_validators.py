"""Tests for it_server/auth/validators.py — Sprint 1 Wave 5.

Requirements: PyJWT[cryptography]>=2.12.0, httpx, pytest, pytest-asyncio.

Structural mirror of ``tests/hr_server/auth/test_validators.py`` with
``HR`` → ``IT`` renames and it-specific scope (``it.read``).

Test count: 10 tests (>= 10 required).

Catalog:
    V-IT-01  Valid token (right aud, right act.sub, right scope) → JWTClaims
    V-IT-02  Wrong aud → JWTValidationError(error_id=ERR-MCP-001)
    V-IT-03  Wrong act.sub → PeerTrustError(error_id=ERR-AGENT-002)
    V-IT-04  Missing required scope → ScopeError(error_id=ERR-MCP-003)
    V-IT-05  Empty trusted_act_subs → PeerTrustError (deny-all)
    V-IT-06  Per-call required_scopes override applied
    V-IT-07  from_config(server_config) produces a working validator
    V-IT-08  log_startup_assertion() emits exactly one INFO log line with expected_aud
    V-IT-09  Tampered JWT (modified signature) → JWTValidationError(ERR-AUTH-006)
    V-IT-10  Depth-2 act chain (nested act) → PeerTrustError (max_depth=1 exceeded)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import jwt as pyjwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

# ---------------------------------------------------------------------------
# Module loading — isolate from package __init__.py (matches conftest.py pattern)
# ---------------------------------------------------------------------------
import importlib.util
import pathlib
import sys
import types as _types

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


# Stub top-level package namespaces so relative imports resolve
for _pkg in ("common", "common.auth", "it_server", "it_server.auth"):
    if _pkg not in sys.modules:
        _stub = _types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        _stub.__path__ = [str(_ROOT / _pkg.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub

# Also register the hyphenated directory under the dotted name the modules use
for _src, _dst in (
    ("it_server", "it_server"),
    ("it_server.auth", "it_server.auth"),
):
    if _dst not in sys.modules:
        _stub = _types.ModuleType(_dst)
        _stub.__package__ = _dst
        src_path = _ROOT / _src.replace(".", "/")
        _stub.__path__ = [str(src_path)]  # type: ignore[assignment]
        sys.modules[_dst] = _stub

_errors_mod = _load("common.auth.errors", "common/auth/errors.py")
_models_mod = _load("common.auth.models", "common/auth/models.py")
_jwt_validator_mod = _load("common.auth.jwt_validator", "common/auth/jwt_validator.py")
_peer_trust_mod = _load("common.auth.peer_trust", "common/auth/peer_trust.py")
_it_validators_mod = _load(
    "it_server.auth.validators", "it_server/auth/validators.py"
)

JWTValidationError: type = _errors_mod.JWTValidationError
PeerTrustError: type = _errors_mod.PeerTrustError
ScopeError: type = _errors_mod.ScopeError
JWTClaims: type = _models_mod.JWTClaims
JWKSCache: type = _jwt_validator_mod.JWKSCache
ITServerTokenValidationConfig: type = _it_validators_mod.ITServerTokenValidationConfig
ITServerTokenValidator: type = _it_validators_mod.ITServerTokenValidator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ISSUER = "https://api.asgardeo.io/t/ddademo/oauth2/token"
IT_AGENT_CLIENT_ID = "it_agent-oauth-client-uuid"
IT_AGENT_UUID = "it_agent-identity-uuid-0001"
SUBJECT = "user-uuid-abc123"
JTI = "jti-it-test-001"

# ---------------------------------------------------------------------------
# Session-scoped RSA keypair fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[Any, Any]:
    """Generate a 2048-bit RSA keypair once per test session.

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
    public_jwk_dict["kid"] = "it-test-key-1"
    public_jwk_dict["use"] = "sig"
    public_jwk_dict["alg"] = "RS256"
    return private_key, public_jwk_dict


@pytest.fixture(scope="session")
def sign_token(rsa_keypair):
    """Return a factory that signs JWT payloads with the session private key."""
    private_key, _ = rsa_keypair

    def _sign(payload: dict[str, Any], kid: str = "it-test-key-1") -> str:
        return pyjwt.encode(
            payload, private_key, algorithm="RS256", headers={"kid": kid}
        )

    return _sign


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_payload() -> dict[str, Any]:
    """Valid JWT payload with all required claims including act.sub."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": IT_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI,
        "scope": "openid it.read",
        "act": {"sub": IT_AGENT_UUID},
    }


@pytest.fixture
def base_validation_config() -> Any:
    return ITServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://api.asgardeo.io/.well-known/jwks",
        expected_aud=IT_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({IT_AGENT_UUID}),
        required_scopes=frozenset({"it.read"}),
    )


def _make_mock_cache(public_jwk: dict[str, Any]) -> Any:
    """Return a JWKSCache whose get_key is mocked to return the given public JWK."""
    cache = JWKSCache(jwks_url="https://mock-jwks/", insecure_tls=True)
    cache.get_key = AsyncMock(return_value=public_jwk)  # type: ignore[method-assign]
    return cache


def _make_validator(
    validation_config: Any,
    public_jwk: dict[str, Any],
) -> Any:
    """Construct an ITServerTokenValidator with a mocked JWKSCache."""
    cache = _make_mock_cache(public_jwk)
    return ITServerTokenValidator(validation_config, jwks_cache=cache)


# ---------------------------------------------------------------------------
# V-IT-01: Valid token → JWTClaims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_token_returns_jwt_claims(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-IT-01: Token with correct aud, act.sub, and scope returns JWTClaims."""
    _, public_jwk = rsa_keypair
    token = sign_token(base_payload)
    validator = _make_validator(base_validation_config, public_jwk)

    claims = await validator.validate_token(token)

    assert isinstance(claims, JWTClaims)
    assert claims.sub == SUBJECT
    assert claims.iss == ISSUER
    assert claims.jti == JTI
    # act is a dict; act.sub is accessed as claims.act["sub"]
    assert isinstance(claims.act, dict)
    assert claims.act["sub"] == IT_AGENT_UUID


# ---------------------------------------------------------------------------
# V-IT-02: Wrong aud → JWTValidationError(ERR-MCP-001)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_aud_raises_err_mcp_001(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-IT-02: Token whose aud does not match expected_aud raises ERR-MCP-001."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, aud="wrong-client-id")
    token = sign_token(payload)
    validator = _make_validator(base_validation_config, public_jwk)

    with pytest.raises(JWTValidationError) as exc_info:
        await validator.validate_token(token)

    assert exc_info.value.error_id == "ERR-MCP-001"


# ---------------------------------------------------------------------------
# V-IT-03: Wrong act.sub → PeerTrustError(ERR-AGENT-002)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_act_sub_raises_peer_trust_error(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-IT-03: Token whose act.sub is not in trusted_act_subs raises PeerTrustError."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, act={"sub": "untrusted-agent-uuid"})
    token = sign_token(payload)
    validator = _make_validator(base_validation_config, public_jwk)

    with pytest.raises(PeerTrustError) as exc_info:
        await validator.validate_token(token)

    assert exc_info.value.error_id == "ERR-AGENT-002"


# ---------------------------------------------------------------------------
# V-IT-04: Missing required scope → ScopeError(ERR-MCP-003)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_required_scope_raises_scope_error(
    base_payload, rsa_keypair, sign_token
):
    """V-IT-04: Token missing it.read raises ScopeError(ERR-MCP-003)."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, scope="openid")  # it.read absent
    config = ITServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=IT_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({IT_AGENT_UUID}),
        required_scopes=frozenset({"it.read"}),
    )
    token = sign_token(payload)
    validator = _make_validator(config, public_jwk)

    with pytest.raises(ScopeError) as exc_info:
        await validator.validate_token(token)

    assert exc_info.value.error_id == "ERR-MCP-003"
    assert "it.read" in exc_info.value.details.get("missing", [])


# ---------------------------------------------------------------------------
# V-IT-05: Empty trusted_act_subs → PeerTrustError (deny-all)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_trusted_act_subs_denies_all(
    base_payload, rsa_keypair, sign_token
):
    """V-IT-05: When trusted_act_subs is empty, every token is denied (deny-all)."""
    _, public_jwk = rsa_keypair
    config = ITServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=IT_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset(),  # empty — no agent trusted
        required_scopes=frozenset(),
    )
    token = sign_token(base_payload)
    validator = _make_validator(config, public_jwk)

    with pytest.raises(PeerTrustError):
        await validator.validate_token(token)


# ---------------------------------------------------------------------------
# V-IT-06: Per-call required_scopes override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_call_required_scopes_override(
    base_payload, rsa_keypair, sign_token
):
    """V-IT-06: validate_token(required_scopes=...) overrides config.required_scopes."""
    _, public_jwk = rsa_keypair
    # Config demands it.write, but we override per-call to only require it.read
    config = ITServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=IT_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({IT_AGENT_UUID}),
        required_scopes=frozenset({"it.write"}),  # config demands it.write
    )
    payload = dict(base_payload, scope="openid it.read")  # token has only it.read
    token = sign_token(payload)
    validator = _make_validator(config, public_jwk)

    # Per-call override: only require it.read → should succeed
    claims = await validator.validate_token(
        token, required_scopes=frozenset({"it.read"})
    )
    assert isinstance(claims, JWTClaims)

    # Now try with config's required_scopes (it.write) → should fail
    with pytest.raises(ScopeError):
        await validator.validate_token(token, required_scopes=None)


# ---------------------------------------------------------------------------
# V-IT-07: from_config() produces a working validator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_config_produces_working_validator(
    base_payload, rsa_keypair, sign_token
):
    """V-IT-07: ITServerTokenValidator.from_config(server_config) produces a working validator."""
    _, public_jwk = rsa_keypair

    # Minimal stub matching the ITServerConfig field names accessed by from_config()
    @dataclass
    class _StubITServerConfig:
        is_issuer: str
        is_jwks_url: str
        expected_aud: str
        trusted_act_subs: frozenset
        required_scopes: frozenset
        is_insecure_tls: bool = False

    stub = _StubITServerConfig(
        is_issuer=ISSUER,
        is_jwks_url="https://mock/",
        expected_aud=IT_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({IT_AGENT_UUID}),
        required_scopes=frozenset({"it.read"}),
    )

    validator = ITServerTokenValidator.from_config(stub)
    # Inject the mock JWKS cache so no HTTP call is made
    validator._jwks_cache = _make_mock_cache(public_jwk)  # type: ignore[attr-defined]
    # Rebuild ValidatorConfig to point at mock jwks_url (from_config used stub's url)
    from common.auth.jwt_validator import ValidatorConfig as _VC
    validator._validator_config = _VC(  # type: ignore[attr-defined]
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=IT_AGENT_CLIENT_ID,
        required_scopes=frozenset(),
        insecure_tls=False,
    )

    token = sign_token(base_payload)
    claims = await validator.validate_token(token)
    assert claims.sub == SUBJECT


# ---------------------------------------------------------------------------
# V-IT-08: log_startup_assertion() emits exactly one INFO log line with expected_aud
# ---------------------------------------------------------------------------


def test_log_startup_assertion_emits_info_with_expected_aud(
    base_validation_config, rsa_keypair
):
    """V-IT-08: log_startup_assertion() emits one INFO line containing expected_aud."""
    _, public_jwk = rsa_keypair
    validator = _make_validator(base_validation_config, public_jwk)

    with patch.object(
        _it_validators_mod.logger, "info"
    ) as mock_info:
        validator.log_startup_assertion()

    mock_info.assert_called_once()
    # First positional arg after format string must be expected_aud
    call_args = mock_info.call_args
    assert call_args[0][1] == IT_AGENT_CLIENT_ID


# ---------------------------------------------------------------------------
# V-IT-09: Tampered JWT → JWTValidationError(ERR-AUTH-006)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tampered_jwt_raises_err_auth_006(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-IT-09: A JWT with a modified payload (tampered signature) raises ERR-AUTH-006."""
    other_private = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    other_public_jwk: dict[str, Any] = json.loads(
        RSAAlgorithm.to_jwk(other_private.public_key())
    )
    other_public_jwk["kid"] = "it-test-key-1"

    # Sign with the session key but provide a DIFFERENT public key for verification
    token = sign_token(base_payload)
    cache = _make_mock_cache(other_public_jwk)
    validator = ITServerTokenValidator(base_validation_config, jwks_cache=cache)

    with pytest.raises(JWTValidationError) as exc_info:
        await validator.validate_token(token)

    assert exc_info.value.error_id == "ERR-AUTH-006"


# ---------------------------------------------------------------------------
# V-IT-10: Depth-2 act chain → PeerTrustError (max_depth=1 exceeded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_depth_2_act_chain_raises_peer_trust_error(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-IT-10: Token with nested depth-2 act chain is rejected because max_depth=1."""
    _, public_jwk = rsa_keypair
    # Depth-2: act.sub = IT_AGENT_UUID, act.act.sub = some-inner-agent
    payload = dict(
        base_payload,
        act={"sub": IT_AGENT_UUID, "act": {"sub": "inner-agent-uuid"}},
    )
    token = sign_token(payload)
    validator = _make_validator(base_validation_config, public_jwk)

    with pytest.raises(PeerTrustError) as exc_info:
        await validator.validate_token(token)

    # PeerTrustError default error_id
    assert exc_info.value.error_id == "ERR-AGENT-002"
