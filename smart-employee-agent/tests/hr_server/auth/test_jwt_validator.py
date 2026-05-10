"""Tests for hr_server REST-path JWT validator + audience-list cap.

Sprint 4 S4.0 Track B introduces:
  - REST validator that accepts a configurable audience LIST
    (``HR_SERVER_REST_VALID_AUDIENCES`` env var; capped at 3 per security
    audit F-01).
  - The MCP-tool validator stays strict single-aud (already covered by
    ``test_validators.py`` — this file only adds the segregation assertion).

Test count: 4 tests. Mapped to brief deliverable B4:
    T-HR-REST-AUDCAP-01  Cap exceeded → ``_resolve_rest_audiences`` raises.
    T-HR-REST-AUDIN-02   REST validator accepts token whose aud IS in the list.
    T-HR-REST-AUDOUT-03  REST validator rejects token whose aud is NOT in list.
    T-HR-AUDSEG-04       MCP-tool path stays strict (multi-aud token-A is
                         accepted by REST but rejected by MCP-tool validator).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time
import types as _types
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

# ---------------------------------------------------------------------------
# Module isolation (matches test_validators.py)
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent


def _load(dotted: str, rel: str) -> _types.ModuleType:
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, _ROOT / rel)
    assert spec and spec.loader, f"Cannot find {rel}"
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


for _pkg in (
    "common",
    "common.auth",
    "common.logging",
    "hr_server",
    "hr_server.auth",
):
    if _pkg not in sys.modules:
        _stub = _types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        _stub.__path__ = [str(_ROOT / _pkg.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub

_errors_mod = _load("common.auth.errors", "common/auth/errors.py")
_models_mod = _load("common.auth.models", "common/auth/models.py")
_jwt_validator_common = _load("common.auth.jwt_validator", "common/auth/jwt_validator.py")
_peer_trust_mod = _load("common.auth.peer_trust", "common/auth/peer_trust.py")
_hr_validators_mod = _load(
    "hr_server.auth.validators", "hr_server/auth/validators.py"
)
_hr_jwt_validator_mod = _load(
    "hr_server.auth.jwt_validator", "hr_server/auth/jwt_validator.py"
)

JWTValidator = _hr_jwt_validator_mod.JWTValidator
TokenError = _hr_jwt_validator_mod.TokenError
JWKSCache = _jwt_validator_common.JWKSCache
HRServerTokenValidationConfig = _hr_validators_mod.HRServerTokenValidationConfig
HRServerTokenValidator = _hr_validators_mod.HRServerTokenValidator
JWTValidationError = _errors_mod.JWTValidationError


# ---------------------------------------------------------------------------
# Constants — three audiences used across tests so we exercise the LIST path.
# ---------------------------------------------------------------------------

ISSUER = "https://is.example.com:9443/oauth2/token"
HR_SERVER_AUD = "hr-server-client-id"
ORCHESTRATOR_AUD = "orchestrator-mcp-client-id"
SPA_AUD = "spa-client-id"
ROGUE_AUD = "rogue-client-id"
HR_AGENT_UUID = "hr_agent-identity-uuid-0001"
SUBJECT = "user-uuid-abc123"


# ---------------------------------------------------------------------------
# Session-scoped RSA keypair (mirror test_validators.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[Any, Any]:
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    public_jwk_dict: dict[str, Any] = json.loads(
        RSAAlgorithm.to_jwk(private_key.public_key())
    )
    public_jwk_dict["kid"] = "hr-rest-test-key-1"
    public_jwk_dict["use"] = "sig"
    public_jwk_dict["alg"] = "RS256"
    return private_key, public_jwk_dict


@pytest.fixture(scope="session")
def sign_token(rsa_keypair):
    private_key, _ = rsa_keypair

    def _sign(payload: dict[str, Any]) -> str:
        return pyjwt.encode(
            payload, private_key, algorithm="RS256",
            headers={"kid": "hr-rest-test-key-1"},
        )

    return _sign


def _stub_signing_key(validator: JWTValidator, public_jwk: dict[str, Any]) -> None:
    """Replace ``_get_signing_key`` with an AsyncMock that returns the local key.

    Avoids hitting the network during tests; the real JWKS fetch happens
    inside ``JWTValidator._get_signing_key`` which we don't need to exercise
    here (the common-side JWKS path is covered elsewhere).
    """
    rsa_key = RSAAlgorithm.from_jwk(public_jwk)
    validator._get_signing_key = AsyncMock(return_value=rsa_key)  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# T-HR-REST-AUDCAP-01: cap enforcement at startup
# ---------------------------------------------------------------------------


@dataclass
class _CfgStub:
    """Minimal stub matching the fields ``_resolve_rest_audiences`` reads."""

    expected_aud: str = HR_SERVER_AUD


def _import_resolve():
    """Import ``_resolve_rest_audiences`` from main.py without executing
    ``create_app`` (which needs a full env). Module-level imports there pull
    FastAPI; that's fine for the cap helper."""
    if "hr_server.main" in sys.modules:
        return sys.modules["hr_server.main"]._resolve_rest_audiences
    main_mod = _load("hr_server.main", "hr_server/main.py")
    return main_mod._resolve_rest_audiences


def test_audience_list_cap_exceeds_raises():
    """T-HR-REST-AUDCAP-01: 4 distinct entries (cfg + 3 extras) exceed the cap of 3.

    Exceeding the cap is an F-01 fail-closed condition — the validator must
    refuse to start rather than silently accept a wider audience set.
    """
    resolve = _import_resolve()
    env = {
        "HR_SERVER_REST_VALID_AUDIENCES": ",".join(
            ["extra-1", "extra-2", "extra-3"]
        ),
    }
    with pytest.raises(ValueError, match="cap"):
        resolve(_CfgStub(), env)


def test_audience_list_within_cap_passes():
    """Sanity rail: cfg + 2 extras = 3 entries — within cap, returns full list."""
    resolve = _import_resolve()
    env = {
        "HR_SERVER_REST_VALID_AUDIENCES": ",".join(
            [ORCHESTRATOR_AUD, SPA_AUD]
        ),
    }
    audiences = resolve(_CfgStub(), env)
    assert audiences == [HR_SERVER_AUD, ORCHESTRATOR_AUD, SPA_AUD]


# ---------------------------------------------------------------------------
# T-HR-REST-AUDIN-02: REST validator accepts any aud in the configured list.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_validator_accepts_aud_in_list(rsa_keypair, sign_token):
    """T-HR-REST-AUDIN-02: token-A audience (orchestrator client) is accepted
    by the REST validator when the audience list includes it."""
    _, public_jwk = rsa_keypair

    rest = JWTValidator(
        jwks_url="https://mock/jwks",
        issuer=ISSUER,
        audience=[HR_SERVER_AUD, ORCHESTRATOR_AUD, SPA_AUD],
        ssl_verify=False,
    )
    _stub_signing_key(rest, public_jwk)

    now = int(time.time())
    token = sign_token({
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": ORCHESTRATOR_AUD,  # token-A from the orchestrator
        "exp": now + 300,
        "iat": now,
        "scope": "openid hr_self_rest",
    })

    payload = await rest.validate_token(token)
    assert payload["sub"] == SUBJECT
    assert payload["aud"] == ORCHESTRATOR_AUD


# ---------------------------------------------------------------------------
# T-HR-REST-AUDOUT-03: REST validator rejects an aud NOT in the configured list.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_validator_rejects_aud_not_in_list(rsa_keypair, sign_token):
    """T-HR-REST-AUDOUT-03: a rogue aud (not in the configured list) is rejected
    even when sig + iss + exp are correct."""
    _, public_jwk = rsa_keypair

    rest = JWTValidator(
        jwks_url="https://mock/jwks",
        issuer=ISSUER,
        audience=[HR_SERVER_AUD, ORCHESTRATOR_AUD, SPA_AUD],
        ssl_verify=False,
    )
    _stub_signing_key(rest, public_jwk)

    now = int(time.time())
    token = sign_token({
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": ROGUE_AUD,  # not in the configured list
        "exp": now + 300,
        "iat": now,
        "scope": "openid hr_self_rest",
    })

    with pytest.raises(TokenError) as exc_info:
        await rest.validate_token(token)
    # PyJWT raises InvalidAudienceError → mapped to "invalid_token" by JWTValidator.
    assert exc_info.value.error_type == "invalid_token"


# ---------------------------------------------------------------------------
# T-HR-AUDSEG-04: MCP-tool validator stays strict single-aud.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_validator_stays_strict_single_aud(rsa_keypair, sign_token):
    """T-HR-AUDSEG-04: A token whose aud is the orchestrator-aud is accepted
    on the REST path but REJECTED by the MCP-tool validator (whose
    ``expected_aud`` remains the strict HR-server own client ID).

    This is the audience-segregation guarantee: relaxing REST does NOT relax
    the MCP-tool boundary.
    """
    _, public_jwk = rsa_keypair

    # MCP-tool validator: strict on HR_SERVER_AUD only.
    mcp_cfg = HRServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock/jwks",
        expected_aud=HR_SERVER_AUD,
        trusted_act_subs=frozenset({HR_AGENT_UUID}),
        required_scopes=frozenset(),
    )
    cache = JWKSCache(jwks_url="https://mock/jwks", insecure_tls=True)
    cache.get_key = AsyncMock(return_value=public_jwk)  # type: ignore[method-assign]
    mcp_validator = HRServerTokenValidator(mcp_cfg, jwks_cache=cache)

    now = int(time.time())
    # Token whose aud is the orchestrator (allowed on REST, rejected on MCP).
    token_with_orch_aud = sign_token({
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": ORCHESTRATOR_AUD,
        "exp": now + 300,
        "iat": now,
        "jti": "jti-segregation-01",
        "scope": "openid hr_self_rest",
        "act": {"sub": HR_AGENT_UUID},
    })

    # MCP path: strict expected_aud=HR_SERVER_AUD → fail.
    with pytest.raises(JWTValidationError):
        await mcp_validator.validate_token(token_with_orch_aud)

    # REST path with the same token: accepted (orchestrator-aud is in list).
    rest = JWTValidator(
        jwks_url="https://mock/jwks",
        issuer=ISSUER,
        audience=[HR_SERVER_AUD, ORCHESTRATOR_AUD],
        ssl_verify=False,
    )
    _stub_signing_key(rest, public_jwk)
    payload = await rest.validate_token(token_with_orch_aud)
    assert payload["aud"] == ORCHESTRATOR_AUD
