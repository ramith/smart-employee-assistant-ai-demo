"""Tests for hr_server/mcp/tools.py — Sprint 1 Wave 6.

Test count: 10 tests (>= 8 required).

Catalog:
    T-HR-MCP-01  Valid token + hr.read → get_leave_balance returns 200 with canned data
    T-HR-MCP-02  Valid token + hr.read → get_leave_history returns 200 with entries
    T-HR-MCP-03  Valid token + hr.write → approve_leave returns 200 with approved status
    T-HR-MCP-04  Missing Authorization header → 401 with error_id=ERR-AUTH-006
    T-HR-MCP-05  Token failing JWT validation → 401 with error body (error_id present)
    T-HR-MCP-06  Token with hr.read calls approve_leave (needs hr.write) → 401 ERR-MCP-003
    T-HR-MCP-07  Token with wrong aud → 401 with error_id=ERR-MCP-001
    T-HR-MCP-08  Error body contains request_id echoed from X-Request-ID header
    T-HR-MCP-09  Bad request body (missing required field) → 422
    T-HR-MCP-10  get_leave_balance with explicit employee_id overrides token.sub lookup
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time
import types as _types
from typing import Any
from unittest.mock import AsyncMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

# ---------------------------------------------------------------------------
# Module isolation (matches pattern in test_validators.py)
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


# Ensure package namespace stubs
for _pkg in (
    "common",
    "common.auth",
    "common.logging",
    "hr_server",
    "hr_server.auth",
    "hr_server.mcp",
):
    if _pkg not in sys.modules:
        _stub = _types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        src_path = _ROOT / _pkg.replace(".", "/")
        _stub.__path__ = [str(src_path)]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub

# Also shim the hyphenated filesystem path under the dotted names
for _src, _dst in (
    ("hr_server", "hr_server"),
    ("hr_server.auth", "hr_server.auth"),
    ("hr_server.mcp", "hr_server.mcp"),
):
    if _dst not in sys.modules:
        _stub = _types.ModuleType(_dst)
        _stub.__package__ = _dst
        _stub.__path__ = [str(_ROOT / _src.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_dst] = _stub

# Load dependency chain
_errors_mod = _load("common.auth.errors", "common/auth/errors.py")
_models_mod = _load("common.auth.models", "common/auth/models.py")
_jwt_validator_mod = _load("common.auth.jwt_validator", "common/auth/jwt_validator.py")
_peer_trust_mod = _load("common.auth.peer_trust", "common/auth/peer_trust.py")
_correlation_mod = _load("common.logging.correlation", "common/logging/correlation.py")
_hr_validators_mod = _load(
    "hr_server.auth.validators", "hr_server/auth/validators.py"
)
_hr_tools_mod = _load("hr_server.mcp.tools", "hr_server/mcp/tools.py")

# Pull out the types we need in tests
JWTValidationError: type = _errors_mod.JWTValidationError
PeerTrustError: type = _errors_mod.PeerTrustError
ScopeError: type = _errors_mod.ScopeError
JWTClaims: type = _models_mod.JWTClaims
JWKSCache: type = _jwt_validator_mod.JWKSCache
HRServerTokenValidationConfig: type = _hr_validators_mod.HRServerTokenValidationConfig
HRServerTokenValidator: type = _hr_validators_mod.HRServerTokenValidator
HRMcpToolRouterDeps: type = _hr_tools_mod.HRMcpToolRouterDeps
build_hr_mcp_router = _hr_tools_mod.build_hr_mcp_router

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ISSUER = "https://api.asgardeo.io/t/ddademo/oauth2/token"
HR_AGENT_CLIENT_ID = "hr_agent-oauth-client-uuid"
HR_AGENT_UUID = "hr_agent-identity-uuid-0001"
SUBJECT = "probe.user"
JTI = "jti-hr-mcp-test-001"
REQUEST_ID = "req-test-uuid-hr-001"

# ---------------------------------------------------------------------------
# Session-scoped RSA keypair
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[Any, Any]:
    """Generate a 2048-bit RSA keypair once per test session."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    public_jwk_dict: dict[str, Any] = json.loads(
        RSAAlgorithm.to_jwk(private_key.public_key())
    )
    public_jwk_dict["kid"] = "hr-mcp-test-key-1"
    public_jwk_dict["use"] = "sig"
    public_jwk_dict["alg"] = "RS256"
    return private_key, public_jwk_dict


@pytest.fixture(scope="session")
def sign_token(rsa_keypair):
    """Return a factory that signs JWT payloads."""
    private_key, _ = rsa_keypair

    def _sign(payload: dict[str, Any]) -> str:
        return pyjwt.encode(
            payload, private_key, algorithm="RS256",
            headers={"kid": "hr-mcp-test-key-1"},
        )

    return _sign


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hr_read_payload() -> dict[str, Any]:
    """Valid JWT payload with hr.read scope."""
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
def hr_write_payload() -> dict[str, Any]:
    """Valid JWT payload with hr.write scope (for approve_leave)."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": HR_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI + "-w",
        "scope": "openid hr.read hr.write",
        "act": {"sub": HR_AGENT_UUID},
    }


def _make_validator(public_jwk: dict[str, Any], required_scopes: frozenset = frozenset()) -> Any:
    """Build an HRServerTokenValidator with a mocked JWKS cache."""
    config = HRServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock-jwks/",
        expected_aud=HR_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({HR_AGENT_UUID}),
        required_scopes=required_scopes,
    )
    cache = JWKSCache(jwks_url="https://mock-jwks/", insecure_tls=True)
    cache.get_key = AsyncMock(return_value=public_jwk)  # type: ignore[method-assign]
    return HRServerTokenValidator(config, jwks_cache=cache)


def _build_app(public_jwk: dict[str, Any]) -> FastAPI:
    """Return a minimal FastAPI app with the HR MCP router mounted."""
    validator = _make_validator(public_jwk)
    deps = HRMcpToolRouterDeps(validator=validator)
    router = build_hr_mcp_router(deps)
    app = FastAPI()
    app.include_router(router, prefix="/mcp/tools")
    return app


# ---------------------------------------------------------------------------
# T-HR-MCP-01: Valid hr.read token → get_leave_balance returns 200
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_leave_balance_valid_token(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-01: Valid token with hr.read returns leave balance for token.sub."""
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_leave_balance",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["employee_id"] == SUBJECT
    assert isinstance(data["leave_days"], int)
    assert data["leave_days"] == 12  # probe.user canned value
    assert "as_of_date" in data


# ---------------------------------------------------------------------------
# T-HR-MCP-02: Valid hr.read token → get_leave_history returns 200 with entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_leave_history_valid_token(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-02: Valid token with hr.read returns leave history list."""
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_leave_history",
        json={"limit": 5},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["employee_id"] == SUBJECT
    assert isinstance(data["entries"], list)
    # probe.user has 2 canned entries; limit=5 should return both
    assert len(data["entries"]) == 2
    assert data["entries"][0]["leave_id"] == "LV-001"


# ---------------------------------------------------------------------------
# T-HR-MCP-03: Valid hr.write token → approve_leave returns 200
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_leave_valid_hr_write_token(rsa_keypair, sign_token, hr_write_payload):
    """T-HR-MCP-03: Token with hr.write scope approves a leave request."""
    _, public_jwk = rsa_keypair
    token = sign_token(hr_write_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/approve_leave",
        json={"leave_id": "LV-004"},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["leave_id"] == "LV-004"
    assert data["status"] == "approved"
    assert data["approved_by"] == HR_AGENT_UUID  # act.sub from token
    assert "approved_at" in data


# ---------------------------------------------------------------------------
# T-HR-MCP-04: Missing Authorization header → 401 with error body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_auth_header_returns_401(rsa_keypair):
    """T-HR-MCP-04: No Authorization header produces 401 with error_id in body."""
    _, public_jwk = rsa_keypair
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_leave_balance",
        json={},
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert "error_id" in detail
    assert detail["error_id"] == "ERR-AUTH-006"


# ---------------------------------------------------------------------------
# T-HR-MCP-05: Token failing JWT validation → 401 with error body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_jwt_returns_401(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-05: A token signed by a different key fails verification → 401."""
    _, public_jwk = rsa_keypair

    # Sign with a different key — verification against public_jwk will fail
    other_private = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    other_token = pyjwt.encode(
        hr_read_payload,
        other_private,
        algorithm="RS256",
        headers={"kid": "hr-mcp-test-key-1"},
    )

    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_leave_balance",
        json={},
        headers={"Authorization": f"Bearer {other_token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert "error_id" in detail


# ---------------------------------------------------------------------------
# T-HR-MCP-06: hr.read token calling approve_leave → 401 ERR-MCP-003
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hr_read_token_cannot_approve_leave(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-06: Token with only hr.read is rejected by approve_leave (needs hr.write)."""
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/approve_leave",
        json={"leave_id": "LV-004"},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["error_id"] == "ERR-MCP-003"


# ---------------------------------------------------------------------------
# T-HR-MCP-07: Wrong aud → 401 with ERR-MCP-001
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_aud_returns_err_mcp_001(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-07: Token with wrong aud claim produces ERR-MCP-001."""
    _, public_jwk = rsa_keypair
    payload = dict(hr_read_payload, aud="completely-wrong-client-id")
    token = sign_token(payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_leave_balance",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["error_id"] == "ERR-MCP-001"


# ---------------------------------------------------------------------------
# T-HR-MCP-08: Error body contains request_id from X-Request-ID header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_body_contains_request_id(rsa_keypair):
    """T-HR-MCP-08: F-07 error body includes the X-Request-ID echoed back."""
    _, public_jwk = rsa_keypair
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    custom_rid = "my-specific-request-id-xyz"
    resp = client.post(
        "/mcp/tools/get_leave_balance",
        json={},
        headers={"X-Request-ID": custom_rid},  # no Authorization
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert detail.get("request_id") == custom_rid


# ---------------------------------------------------------------------------
# T-HR-MCP-09: Bad request body → 422 Unprocessable Entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_leave_missing_required_field_returns_422(rsa_keypair, sign_token, hr_write_payload):
    """T-HR-MCP-09: approve_leave requires leave_id; omitting it yields 422."""
    _, public_jwk = rsa_keypair
    token = sign_token(hr_write_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/approve_leave",
        json={},  # missing required leave_id
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# T-HR-MCP-10: explicit employee_id overrides token.sub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_leave_balance_explicit_employee_id(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-10: Passing employee_id in body overrides token.sub for data lookup."""
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    # "user-uuid-abc123" has 10 leave days in the canned data
    resp = client.post(
        "/mcp/tools/get_leave_balance",
        json={"employee_id": "user-uuid-abc123"},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["employee_id"] == "user-uuid-abc123"
    assert data["leave_days"] == 10
