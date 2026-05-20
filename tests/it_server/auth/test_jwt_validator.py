"""Tests for it_server/auth/jwt_validator.py — Sprint 4 S4.0 reconciliation.

Catalog:
    V-IT-JWT-01  build_audiences with no extras → single-entry list
    V-IT-JWT-02  build_audiences with extras (≤cap) → composed list (deduped)
    V-IT-JWT-03  build_audiences exceeding F-01 cap → ValueError (fail-closed)
    V-IT-JWT-04  REST validator accepts a token whose aud matches an extra
                 (e.g. orchestrator MCP client_id appended via env)
    V-IT-JWT-05  REST validator rejects a token whose aud is in neither
                 expected_aud nor any extra → TokenError(invalid_token)
    V-IT-JWT-06  build_validator_from_config logs each accepted audience at INFO
"""

from __future__ import annotations

import importlib.util
import json
import logging
import pathlib
import sys
import time
import types as _types
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

# ---------------------------------------------------------------------------
# Module isolation — match tests/it_server/mcp/test_tools.py pattern
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


for _pkg in ("it_server", "it_server.auth"):
    if _pkg not in sys.modules:
        _stub = _types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        _stub.__path__ = [str(_ROOT / _pkg.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub

_jwt_validator_mod = _load(
    "it_server.auth.jwt_validator", "it_server/auth/jwt_validator.py"
)

JWTValidator = _jwt_validator_mod.JWTValidator
TokenError = _jwt_validator_mod.TokenError
build_audiences = _jwt_validator_mod.build_audiences
build_validator_from_config = _jwt_validator_mod.build_validator_from_config


# ---------------------------------------------------------------------------
# Constants + RSA fixture
# ---------------------------------------------------------------------------

ISSUER = "https://is.example.com:9443/oauth2/token"
JWKS_URL = "https://is.example.com:9443/oauth2/jwks"
IT_REST_AUD = "it-server-rest-client-id"
ORCH_MCP_AUD = "orch-mcp-client-id"
SPA_AUD = "spa-client-id"


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[Any, dict[str, Any]]:
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    public_jwk: dict[str, Any] = json.loads(
        RSAAlgorithm.to_jwk(private_key.public_key())
    )
    public_jwk["kid"] = "it-rest-test-key-1"
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"
    return private_key, public_jwk


@pytest.fixture(scope="module")
def sign_token(rsa_keypair):
    private_key, _ = rsa_keypair

    def _sign(payload: dict[str, Any]) -> str:
        return pyjwt.encode(
            payload, private_key, algorithm="RS256",
            headers={"kid": "it-rest-test-key-1"},
        )

    return _sign


def _make_validator(audiences: list[str], public_jwk: dict[str, Any]) -> JWTValidator:
    """Build a JWTValidator with the JWKS cache pre-populated (no network)."""
    v = JWTValidator(
        jwks_url=JWKS_URL,
        issuer=ISSUER,
        audience=audiences,
        ssl_verify=False,
    )
    v._jwks_cache = {"keys": [public_jwk]}  # type: ignore[attr-defined]
    return v


# ---------------------------------------------------------------------------
# V-IT-JWT-01: build_audiences with no extras → single-entry list
# ---------------------------------------------------------------------------


def test_build_audiences_no_extras() -> None:
    """V-IT-JWT-01: no extras_env → audiences == [expected_aud]."""
    auds = build_audiences(IT_REST_AUD, extras_env=None)
    assert auds == [IT_REST_AUD]

    auds_empty = build_audiences(IT_REST_AUD, extras_env="")
    assert auds_empty == [IT_REST_AUD]


# ---------------------------------------------------------------------------
# V-IT-JWT-02: build_audiences appends extras (deduped) within cap
# ---------------------------------------------------------------------------


def test_build_audiences_with_extras_under_cap() -> None:
    """V-IT-JWT-02: comma-separated extras appended; duplicates removed; ≤cap."""
    auds = build_audiences(IT_REST_AUD, extras_env=f"{ORCH_MCP_AUD},{SPA_AUD}")
    assert auds == [IT_REST_AUD, ORCH_MCP_AUD, SPA_AUD]

    # Deduplication: extras containing the expected_aud do not double up.
    auds_dedup = build_audiences(
        IT_REST_AUD, extras_env=f"{IT_REST_AUD},{ORCH_MCP_AUD}"
    )
    assert auds_dedup == [IT_REST_AUD, ORCH_MCP_AUD]


# ---------------------------------------------------------------------------
# V-IT-JWT-03: F-01 cap exceeded → ValueError
# ---------------------------------------------------------------------------


def test_build_audiences_cap_enforced() -> None:
    """V-IT-JWT-03: list exceeding 3 entries fails closed (security audit F-01)."""
    too_many = ",".join(["aud1", "aud2", "aud3"])  # plus expected = 4 total
    with pytest.raises(ValueError, match="exceeds cap"):
        build_audiences(IT_REST_AUD, extras_env=too_many)


# ---------------------------------------------------------------------------
# V-IT-JWT-04: validator accepts a token whose aud matches an appended extra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_accepts_extra_audience(rsa_keypair, sign_token) -> None:
    """V-IT-JWT-04: token aud == orchestrator MCP client_id is accepted when listed."""
    _, public_jwk = rsa_keypair
    audiences = build_audiences(IT_REST_AUD, extras_env=ORCH_MCP_AUD)
    validator = _make_validator(audiences, public_jwk)

    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "user-1",
        "aud": ORCH_MCP_AUD,  # matches second list entry
        "exp": now + 300,
        "iat": now,
    }
    token = sign_token(payload)

    decoded = await validator.validate_token(token)
    assert decoded["sub"] == "user-1"
    assert decoded["aud"] == ORCH_MCP_AUD


# ---------------------------------------------------------------------------
# V-IT-JWT-05: validator rejects a token whose aud is on neither list entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_rejects_unlisted_audience(rsa_keypair, sign_token) -> None:
    """V-IT-JWT-05: aud not present in the configured list → TokenError."""
    _, public_jwk = rsa_keypair
    audiences = build_audiences(IT_REST_AUD, extras_env=ORCH_MCP_AUD)
    validator = _make_validator(audiences, public_jwk)

    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "user-2",
        "aud": "some-other-tenant-client-id",
        "exp": now + 300,
        "iat": now,
    }
    token = sign_token(payload)

    with pytest.raises(TokenError) as exc_info:
        await validator.validate_token(token)
    assert exc_info.value.error_type == "invalid_token"


# ---------------------------------------------------------------------------
# V-IT-JWT-06: build_validator_from_config logs every accepted audience at INFO
# ---------------------------------------------------------------------------


def test_build_validator_from_config_logs_audiences(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """V-IT-JWT-06: F-01 startup log enumerates every accepted audience."""

    class _StubCfg:
        expected_aud = IT_REST_AUD
        is_jwks_url = JWKS_URL
        is_issuer = ISSUER
        is_insecure_tls = True

    monkeypatch.setenv("IT_SERVER_REST_VALID_AUDIENCES", ORCH_MCP_AUD)

    with caplog.at_level(logging.INFO, logger="it_server.auth.jwt_validator"):
        validator = build_validator_from_config(_StubCfg())

    assert isinstance(validator, JWTValidator)
    assert IT_REST_AUD in caplog.text
    assert ORCH_MCP_AUD in caplog.text
    assert "expected_audiences" in caplog.text
