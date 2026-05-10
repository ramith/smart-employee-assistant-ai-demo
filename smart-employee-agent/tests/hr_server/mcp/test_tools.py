"""Tests for hr_server/mcp/tools.py — Sprint 1 Wave 6 / Sprint 4 S4.0 Track B.

Sprint 4 reshape: handlers now delegate to ``hr_service`` (in-memory store)
instead of the Sprint-1 canned dicts, per
``docs/architecture/sprint-4-stage-6.5-reconciliation.md`` §D1. Assertions
are reshaped against the ``hr_service`` response projection.

Test count: 10 tests (>= 8 required).

Catalog:
    T-HR-MCP-01  Valid token + hr_self_rest → get_leave_balance returns 200 with
                 {employee, balance: {annual, sick, personal}, as_of_date}.
    T-HR-MCP-02  Valid token + hr_self_rest → get_leave_history returns 200 with
                 entries shaped as hr_service rows (request_id/type/start_date/...).
    T-HR-MCP-03  Valid token + hr_approve_rest → approve_leave returns 200 with
                 success-or-error envelope from hr_service.
    T-HR-MCP-04  Missing Authorization header → 401 with error_id=ERR-AUTH-006.
    T-HR-MCP-05  Token failing JWT validation → 401 with error body (error_id present).
    T-HR-MCP-06  Token with hr_self_rest only calls approve_leave (needs
                 hr_approve_rest) → 401 ERR-MCP-003.
    T-HR-MCP-07  Token with wrong aud → 401 with error_id=ERR-MCP-001.
    T-HR-MCP-08  Error body contains request_id echoed from X-Request-ID header.
    T-HR-MCP-09  Bad request body (missing required field) → 422.
    T-HR-MCP-10  get_leave_balance auto-registers the user via store.ensure_user
                 (legacy ``employee_id`` arg is ignored — Sprint 4 D1).
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
    "hr_server.service",
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
    ("hr_server.service", "hr_server.service"),
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
# Sprint 4 S4.0 Track B: tools.py now imports hr_service. The service in turn
# imports `hr_server.service.store` — load both so the full delegation chain
# resolves under the importlib bootstrap.
_hr_store_mod = _load("hr_server.service.store", "hr_server/service/store.py")
_hr_service_mod = _load("hr_server.service.hr_service", "hr_server/service/hr_service.py")
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


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset the in-memory store before each test so user auto-registration is
    deterministic (no leak from a prior test's ensure_user call)."""
    _hr_store_mod.reset_data()
    yield
    _hr_store_mod.reset_data()


@pytest.fixture
def hr_read_payload() -> dict[str, Any]:
    """Valid JWT payload with hr_self_rest scope and Sprint 4 username claim."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": HR_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI,
        "scope": "openid hr_self_rest",
        "act": {"sub": HR_AGENT_UUID},
        # Sprint 4 Track A: identity claims plumbed through JWTClaims.
        "username": "Probe",
        "email": "probe.user@example.com",
    }


@pytest.fixture
def hr_write_payload() -> dict[str, Any]:
    """Valid JWT payload with hr_approve_rest scope (for approve_leave)."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": HR_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI + "-w",
        "scope": "openid hr_self_rest hr_approve_rest",
        "act": {"sub": HR_AGENT_UUID},
        "username": "Probe",
        "email": "probe.user@example.com",
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
# T-HR-MCP-01: Valid token → get_leave_balance returns Sprint-4 balance shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_leave_balance_valid_token(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-01: Returns the hr_service balance projection (annual/sick/personal)."""
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
    # Sprint 4 D1 reshape: employee + balance buckets, no leave_days int.
    assert data["employee"] == "Probe"
    assert data["balance"] == {"annual": 20, "sick": 10, "personal": 5}
    assert "as_of_date" in data
    # store.ensure_user side-effect: user is now registered.
    assert SUBJECT in _hr_store_mod.users


# ---------------------------------------------------------------------------
# T-HR-MCP-02: Valid token → get_leave_history returns hr_service shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_leave_history_valid_token(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-02: Returns rows shaped per hr_service.get_my_leave_requests."""
    _, public_jwk = rsa_keypair

    # Seed one leave request via the service so the endpoint has data to project.
    await _hr_service_mod.apply_leave(
        sub=SUBJECT,
        first_name="Probe",
        last_name="",
        leave_type="Sick Leave",
        start_date="2026-06-10",
        end_date="2026-06-10",
        reason="Doctor visit",
    )

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
    assert len(data["entries"]) == 1
    row = data["entries"][0]
    # Sprint 4 D1 row shape: request_id / days_requested / type — not leave_id / days.
    assert row["request_id"] == "LR001"
    assert row["type"] == "Sick Leave"
    assert row["days_requested"] == 1
    assert row["status"] == "Pending"


# ---------------------------------------------------------------------------
# T-HR-MCP-03: Valid hr_approve_rest token → approve_leave delegates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_leave_valid_hr_write_token(rsa_keypair, sign_token, hr_write_payload):
    """T-HR-MCP-03: approve_leave routes through hr_service.approve_leave_request."""
    _, public_jwk = rsa_keypair

    # Seed a pending leave request authored by SUBJECT.
    await _hr_service_mod.apply_leave(
        sub=SUBJECT,
        first_name="Probe",
        last_name="",
        leave_type="Annual Leave",
        start_date=(_dt(8)),  # 8 days out — passes 7-day notice
        end_date=(_dt(8)),
        reason="Family event",
    )
    request_id = "LR001"

    token = sign_token(hr_write_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/approve_leave",
        json={"leave_id": request_id},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["request_id"] == request_id
    assert data["new_status"] == "Approved"
    assert data["approved_by"] == HR_AGENT_UUID  # act.sub from token
    # The store now reflects the approval.
    assert _hr_store_mod.leave_requests[request_id]["status"] == "Approved"


def _dt(days_out: int) -> str:
    """Return an ISO-format date `days_out` days from today."""
    from datetime import date as _date, timedelta as _td
    return (_date.today() + _td(days=days_out)).isoformat()


# ---------------------------------------------------------------------------
# T-HR-MCP-04: Missing Authorization header → 401 with error body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_auth_header_returns_401(rsa_keypair):
    """T-HR-MCP-04: No Authorization header produces 401 with error_id=ERR-AUTH-006."""
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
# T-HR-MCP-06: hr_self_rest-only token cannot approve_leave
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hr_read_token_cannot_approve_leave(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-06: hr_self_rest is rejected by approve_leave → 401 ERR-MCP-003."""
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/approve_leave",
        json={"leave_id": "LR001"},
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
# T-HR-MCP-10: get_leave_balance auto-registers via store.ensure_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_leave_balance_auto_registers_user(rsa_keypair, sign_token, hr_read_payload):
    """T-HR-MCP-10: First call seeds the user + default balance via ensure_user.

    Sprint 4 D1 supersedes the legacy ``employee_id``-override behaviour: the
    body field is ignored; the token's sub + username drive store registration.
    """
    _, public_jwk = rsa_keypair
    assert SUBJECT not in _hr_store_mod.users  # autouse fixture cleared the store.
    token = sign_token(hr_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_leave_balance",
        json={"employee_id": "ignored-by-design"},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["employee"] == "Probe"
    assert _hr_store_mod.users[SUBJECT]["name"] == "Probe"


# ---------------------------------------------------------------------------
# Sprint 4 S4.1 cubicle scope-guard tests (UC-11)
# ---------------------------------------------------------------------------


@pytest.fixture
def hr_read_rest_payload() -> dict[str, Any]:
    """Valid JWT payload with hr_read_rest scope (HR Admin read tier)."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": HR_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI + "-r",
        "scope": "openid hr_read_rest",
        "act": {"sub": HR_AGENT_UUID},
        "username": "Probe",
        "email": "probe.user@example.com",
    }


@pytest.fixture
def hr_assets_write_payload() -> dict[str, Any]:
    """Valid JWT payload with hr_assets_write_rest scope (admin write tier)."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": HR_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI + "-aw",
        "scope": "openid hr_assets_write_rest",
        "act": {"sub": HR_AGENT_UUID},
        "username": "Probe",
        "email": "probe.user@example.com",
    }


# T-HR-MCP-CUBE-01: hr_read_rest token → get_cubicle_summary returns 200.


@pytest.mark.asyncio
async def test_cubicle_summary_with_hr_read_rest(rsa_keypair, sign_token, hr_read_rest_payload):
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_rest_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_cubicle_summary",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["floor_1"] == {"total": 25, "vacant": 25}
    assert data["floor_4"] == {"total": 25, "vacant": 25}


# T-HR-MCP-CUBE-02: hr_self_rest-only token → get_cubicle_summary rejected.


@pytest.mark.asyncio
async def test_cubicle_summary_rejects_hr_self_only(rsa_keypair, sign_token, hr_read_payload):
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_payload)  # has hr_self_rest, not hr_read_rest
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_cubicle_summary",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_id"] == "ERR-MCP-003"


# T-HR-MCP-CUBE-03: hr_read_rest token → vacant_on_floor returns 200.


@pytest.mark.asyncio
async def test_vacant_floor_with_hr_read_rest(rsa_keypair, sign_token, hr_read_rest_payload):
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_rest_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_vacant_cubicles_on_floor",
        json={"floor": 2},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["floor"] == 2
    assert "C-027" in data["vacant"]


# T-HR-MCP-CUBE-04: assign_cubicle requires hr_assets_write_rest (NEW scope).


@pytest.mark.asyncio
async def test_assign_cubicle_with_hr_assets_write(rsa_keypair, sign_token, hr_assets_write_payload):
    _, public_jwk = rsa_keypair
    token = sign_token(hr_assets_write_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/assign_cubicle",
        json={
            "cubicle_id": "C-027",
            "employee_username": "jane.doe",
            "employee_email": "jane.doe@example.com",
        },
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert data["cubicle_id"] == "C-027"
    assert data["floor"] == 2
    assert data["assigned_to"]["username"] == "jane.doe"


# T-HR-MCP-CUBE-05: hr_read_rest-only token CANNOT assign_cubicle.


@pytest.mark.asyncio
async def test_assign_cubicle_rejects_hr_read_only(rsa_keypair, sign_token, hr_read_rest_payload):
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_rest_payload)  # missing hr_assets_write_rest
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/assign_cubicle",
        json={
            "cubicle_id": "C-027",
            "employee_username": "jane.doe",
            "employee_email": "jane.doe@example.com",
        },
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_id"] == "ERR-MCP-003"


# T-HR-MCP-CUBE-06: get_my_cubicle requires hr_self_rest.


@pytest.mark.asyncio
async def test_get_my_cubicle_with_hr_self_rest(rsa_keypair, sign_token, hr_read_payload):
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_payload)  # hr_self_rest
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_my_cubicle",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # The fixture user has no cubicle → assigned False.
    assert data["assigned"] is False


# T-HR-MCP-CUBE-07: lookup_employee requires hr_read_rest.


@pytest.mark.asyncio
async def test_lookup_employee_with_hr_read_rest(rsa_keypair, sign_token, hr_read_rest_payload):
    _, public_jwk = rsa_keypair
    token = sign_token(hr_read_rest_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/lookup_employee",
        json={"username_or_email": "jane.doe"},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["found"] is True
    assert data["username"] == "jane.doe"
    assert data["email"] == "jane.doe@example.com"
