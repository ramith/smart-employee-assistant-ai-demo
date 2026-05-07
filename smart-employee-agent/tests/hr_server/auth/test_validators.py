"""Tests for hr_server/auth/validators.py — Sprint 1 Wave 5.

Requirements: PyJWT[cryptography]>=2.12.0, httpx, pytest, pytest-asyncio.

Test rig
--------
A 2048-bit RSA keypair is generated once per session via the ``rsa_keypair``
session fixture.  ``JWKSCache.get_key`` is patched with ``unittest.mock.AsyncMock``
so no real HTTP calls are made.  Each test builds its own JWT signed with the
private key and validates against the matching public JWK.

Test count: 10 tests (>= 10 required).

Catalog:
    V-HR-01  Valid token (right aud, right act.sub, right scope) → JWTClaims
    V-HR-02  Wrong aud → JWTValidationError(error_id=ERR-MCP-001)
    V-HR-03  Wrong act.sub → PeerTrustError(error_id=ERR-AGENT-002)
    V-HR-04  Missing required scope → ScopeError(error_id=ERR-MCP-003)
    V-HR-05  Empty trusted_act_subs → PeerTrustError (deny-all)
    V-HR-06  Per-call required_scopes override applied
    V-HR-07  from_config(server_config) produces a working validator
    V-HR-08  log_startup_assertion() emits exactly one INFO log line with expected_aud
    V-HR-09  Tampered JWT (modified signature) → JWTValidationError(ERR-AUTH-006)
    V-HR-10  Depth-2 act chain (nested act) → PeerTrustError (max_depth=1 exceeded)
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
for _pkg in ("common", "common.auth", "hr_server", "hr_server.auth"):
    if _pkg not in sys.modules:
        _stub = _types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        _stub.__path__ = [str(_ROOT / _pkg.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub

# Also register the hyphenated directory under the dotted name the modules use
for _src, _dst in (
    ("hr_server", "hr_server"),
    ("hr_server.auth", "hr_server.auth"),
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
_hr_validators_mod = _load(
    "hr_server.auth.validators", "hr_server/auth/validators.py"
)

JWTValidationError: type = _errors_mod.JWTValidationError
PeerTrustError: type = _errors_mod.PeerTrustError
ScopeError: type = _errors_mod.ScopeError
JWTClaims: type = _models_mod.JWTClaims
JWKSCache: type = _jwt_validator_mod.JWKSCache
HRServerTokenValidationConfig: type = _hr_validators_mod.HRServerTokenValidationConfig
HRServerTokenValidator: type = _hr_validators_mod.HRServerTokenValidator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ISSUER = "https://api.asgardeo.io/t/ddademo/oauth2/token"
HR_AGENT_CLIENT_ID = "hr_agent-oauth-client-uuid"
HR_AGENT_UUID = "hr_agent-identity-uuid-0001"
SUBJECT = "user-uuid-abc123"
JTI = "jti-hr-test-001"

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
    public_jwk_dict["kid"] = "hr-test-key-1"
    public_jwk_dict["use"] = "sig"
    public_jwk_dict["alg"] = "RS256"
    return private_key, public_jwk_dict


@pytest.fixture(scope="session")
def sign_token(rsa_keypair):
    """Return a factory that signs JWT payloads with the session private key."""
    private_key, _ = rsa_keypair

    def _sign(payload: dict[str, Any], kid: str = "hr-test-key-1") -> str:
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
        "aud": HR_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI,
        "scope": "openid hr.read",
        "act": {"sub": HR_AGENT_UUID},
    }


@pytest.fixture
def base_validation_config() -> Any:
    return HRServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://api.asgardeo.io/.well-known/jwks",
        expected_aud=HR_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({HR_AGENT_UUID}),
        required_scopes=frozenset({"hr.read"}),
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
    """Construct an HRServerTokenValidator with a mocked JWKSCache."""
    cache = _make_mock_cache(public_jwk)
    return HRServerTokenValidator(validation_config, jwks_cache=cache)


# ---------------------------------------------------------------------------
# V-HR-01: Valid token → JWTClaims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_token_returns_jwt_claims(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-HR-01: Token with correct aud, act.sub, and scope returns JWTClaims."""
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
    assert claims.act["sub"] == HR_AGENT_UUID


# ---------------------------------------------------------------------------
# V-HR-02: Wrong aud → JWTValidationError(ERR-MCP-001)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_aud_raises_err_mcp_001(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-HR-02: Token whose aud does not match expected_aud raises ERR-MCP-001."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, aud="wrong-client-id")
    token = sign_token(payload)
    validator = _make_validator(base_validation_config, public_jwk)

    with pytest.raises(JWTValidationError) as exc_info:
        await validator.validate_token(token)

    assert exc_info.value.error_id == "ERR-MCP-001"


# ---------------------------------------------------------------------------
# V-HR-03: Wrong act.sub → PeerTrustError(ERR-AGENT-002)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_act_sub_raises_peer_trust_error(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-HR-03: Token whose act.sub is not in trusted_act_subs raises PeerTrustError."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, act={"sub": "untrusted-agent-uuid"})
    token = sign_token(payload)
    validator = _make_validator(base_validation_config, public_jwk)

    with pytest.raises(PeerTrustError) as exc_info:
        await validator.validate_token(token)

    assert exc_info.value.error_id == "ERR-AGENT-002"


# ---------------------------------------------------------------------------
# V-HR-04: Missing required scope → ScopeError(ERR-MCP-003)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_required_scope_raises_scope_error(
    base_payload, rsa_keypair, sign_token
):
    """V-HR-04: Token missing hr.read raises ScopeError(ERR-MCP-003)."""
    _, public_jwk = rsa_keypair
    payload = dict(base_payload, scope="openid")  # hr.read absent
    config = HRServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=HR_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({HR_AGENT_UUID}),
        required_scopes=frozenset({"hr.read"}),
    )
    token = sign_token(payload)
    validator = _make_validator(config, public_jwk)

    with pytest.raises(ScopeError) as exc_info:
        await validator.validate_token(token)

    assert exc_info.value.error_id == "ERR-MCP-003"
    assert "hr.read" in exc_info.value.details.get("missing", [])


# ---------------------------------------------------------------------------
# V-HR-05: Empty trusted_act_subs → PeerTrustError (deny-all)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_trusted_act_subs_denies_all(
    base_payload, rsa_keypair, sign_token
):
    """V-HR-05: When trusted_act_subs is empty, every token is denied (deny-all)."""
    _, public_jwk = rsa_keypair
    config = HRServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=HR_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset(),  # empty — no agent trusted
        required_scopes=frozenset(),
    )
    token = sign_token(base_payload)
    validator = _make_validator(config, public_jwk)

    with pytest.raises(PeerTrustError):
        await validator.validate_token(token)


# ---------------------------------------------------------------------------
# V-HR-06: Per-call required_scopes override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_call_required_scopes_override(
    base_payload, rsa_keypair, sign_token
):
    """V-HR-06: validate_token(required_scopes=...) overrides config.required_scopes."""
    _, public_jwk = rsa_keypair
    # Config demands hr.write, but we override per-call to only require hr.read
    config = HRServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=HR_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({HR_AGENT_UUID}),
        required_scopes=frozenset({"hr.write"}),  # config demands hr.write
    )
    payload = dict(base_payload, scope="openid hr.read")  # token has only hr.read
    token = sign_token(payload)
    validator = _make_validator(config, public_jwk)

    # Per-call override: only require hr.read → should succeed
    claims = await validator.validate_token(
        token, required_scopes=frozenset({"hr.read"})
    )
    assert isinstance(claims, JWTClaims)

    # Now try with config's required_scopes (hr.write) → should fail
    with pytest.raises(ScopeError):
        await validator.validate_token(token, required_scopes=None)


# ---------------------------------------------------------------------------
# V-HR-07: from_config() produces a working validator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_config_produces_working_validator(
    base_payload, rsa_keypair, sign_token
):
    """V-HR-07: HRServerTokenValidator.from_config(server_config) produces a working validator."""
    _, public_jwk = rsa_keypair

    # Minimal stub matching the HRServerConfig field names accessed by from_config()
    @dataclass
    class _StubHRServerConfig:
        is_issuer: str
        is_jwks_url: str
        expected_aud: str
        trusted_act_subs: frozenset
        required_scopes: frozenset
        is_insecure_tls: bool = False

    stub = _StubHRServerConfig(
        is_issuer=ISSUER,
        is_jwks_url="https://mock/",
        expected_aud=HR_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({HR_AGENT_UUID}),
        required_scopes=frozenset({"hr.read"}),
    )

    validator = HRServerTokenValidator.from_config(stub)
    # Inject the mock JWKS cache so no HTTP call is made
    validator._jwks_cache = _make_mock_cache(public_jwk)  # type: ignore[attr-defined]
    # The ValidatorConfig was built before cache injection; rebuild it to point at mock
    from common.auth.jwt_validator import ValidatorConfig as _VC
    validator._validator_config = _VC(  # type: ignore[attr-defined]
        expected_iss=ISSUER,
        jwks_url="https://mock/",
        expected_aud=HR_AGENT_CLIENT_ID,
        required_scopes=frozenset(),
        insecure_tls=False,
    )

    token = sign_token(base_payload)
    claims = await validator.validate_token(token)
    assert claims.sub == SUBJECT


# ---------------------------------------------------------------------------
# V-HR-08: log_startup_assertion() emits exactly one INFO log line with expected_aud
# ---------------------------------------------------------------------------


def test_log_startup_assertion_emits_info_with_expected_aud(
    base_validation_config, rsa_keypair
):
    """V-HR-08: log_startup_assertion() emits one INFO line containing expected_aud."""
    _, public_jwk = rsa_keypair
    validator = _make_validator(base_validation_config, public_jwk)

    with patch.object(
        _hr_validators_mod.logger, "info"
    ) as mock_info:
        validator.log_startup_assertion()

    mock_info.assert_called_once()
    # First positional arg after format string must be expected_aud
    call_args = mock_info.call_args
    assert call_args[0][1] == HR_AGENT_CLIENT_ID


# ---------------------------------------------------------------------------
# V-HR-09: Tampered JWT → JWTValidationError(ERR-AUTH-006)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tampered_jwt_raises_err_auth_006(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-HR-09: A JWT with a modified payload (tampered signature) raises ERR-AUTH-006."""
    other_private = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    other_public_jwk: dict[str, Any] = json.loads(
        RSAAlgorithm.to_jwk(other_private.public_key())
    )
    other_public_jwk["kid"] = "hr-test-key-1"

    # Sign with the session key but provide a DIFFERENT public key for verification
    token = sign_token(base_payload)
    cache = _make_mock_cache(other_public_jwk)
    validator = HRServerTokenValidator(base_validation_config, jwks_cache=cache)

    with pytest.raises(JWTValidationError) as exc_info:
        await validator.validate_token(token)

    assert exc_info.value.error_id == "ERR-AUTH-006"


# ---------------------------------------------------------------------------
# V-HR-10: Depth-2 act chain → PeerTrustError (max_depth=1 exceeded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_depth_2_act_chain_raises_peer_trust_error(
    base_payload, base_validation_config, rsa_keypair, sign_token
):
    """V-HR-10: Token with nested depth-2 act chain is rejected because max_depth=1."""
    _, public_jwk = rsa_keypair
    # Depth-2: act.sub = HR_AGENT_UUID, act.act.sub = some-inner-agent
    payload = dict(
        base_payload,
        act={"sub": HR_AGENT_UUID, "act": {"sub": "inner-agent-uuid"}},
    )
    token = sign_token(payload)
    validator = _make_validator(base_validation_config, public_jwk)

    with pytest.raises(PeerTrustError) as exc_info:
        await validator.validate_token(token)

    # PeerTrustError default error_id
    assert exc_info.value.error_id == "ERR-AGENT-002"
