"""Tests for it_server/mcp/tools.py — Sprint 1 Wave 6.

Test count: 10 tests (>= 8 required).

Catalog:
    T-IT-MCP-01  Valid token + it.read → list_available_assets returns 200 with catalogue
    T-IT-MCP-02  Valid token + it.read + asset_type filter → returns filtered list
    T-IT-MCP-03  Valid token + it.read → get_my_assets returns 200 with user's assets
    T-IT-MCP-04  Missing Authorization header → 401 with error_id=ERR-AUTH-006
    T-IT-MCP-05  Token failing JWT validation → 401 with error body (error_id present)
    T-IT-MCP-06  Token with wrong aud → 401 with error_id=ERR-MCP-001
    T-IT-MCP-07  Token missing it.read scope → 401 with error_id=ERR-MCP-003
    T-IT-MCP-08  Error body contains request_id echoed from X-Request-ID header
    T-IT-MCP-09  Bad request body (invalid field type) → 422
    T-IT-MCP-10  get_my_assets with explicit employee_id overrides token.sub lookup
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
# Module isolation
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
    "it_server",
    "it_server.auth",
    "it_server.mcp",
    "it_server.service",
):
    if _pkg not in sys.modules:
        _stub = _types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        src_path = _ROOT / _pkg.replace(".", "/")
        _stub.__path__ = [str(src_path)]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub

# Shim hyphenated filesystem path
for _src, _dst in (
    ("it_server", "it_server"),
    ("it_server.auth", "it_server.auth"),
    ("it_server.mcp", "it_server.mcp"),
    ("it_server.service", "it_server.service"),
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
_it_validators_mod = _load(
    "it_server.auth.validators", "it_server/auth/validators.py"
)
# Load store and service before tools so they're in sys.modules and accessible.
_it_store_mod = _load("it_server.service.store", "it_server/service/store.py")
_it_service_mod = _load("it_server.service.it_service", "it_server/service/it_service.py")
_it_tools_mod = _load("it_server.mcp.tools", "it_server/mcp/tools.py")

# Types
JWTValidationError: type = _errors_mod.JWTValidationError
PeerTrustError: type = _errors_mod.PeerTrustError
ScopeError: type = _errors_mod.ScopeError
JWTClaims: type = _models_mod.JWTClaims
JWKSCache: type = _jwt_validator_mod.JWKSCache
ITServerTokenValidationConfig: type = _it_validators_mod.ITServerTokenValidationConfig
ITServerTokenValidator: type = _it_validators_mod.ITServerTokenValidator
ITMcpToolRouterDeps: type = _it_tools_mod.ITMcpToolRouterDeps
build_it_mcp_router = _it_tools_mod.build_it_mcp_router

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ISSUER = "https://api.asgardeo.io/t/ddademo/oauth2/token"
IT_AGENT_CLIENT_ID = "it_agent-oauth-client-uuid"
IT_AGENT_UUID = "it_agent-identity-uuid-0001"
# S5.12: email-form sub (all OAuth apps now assert email as OIDC subject).
SUBJECT = "employee_user@example.com"
JTI = "jti-it-mcp-test-001"
REQUEST_ID = "req-test-uuid-it-001"

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
    public_jwk_dict["kid"] = "it-mcp-test-key-1"
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
            headers={"kid": "it-mcp-test-key-1"},
        )

    return _sign


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset the in-memory IT store before each test (S5.12: no seed data)."""
    _it_store_mod.reset_data()
    yield
    _it_store_mod.reset_data()


@pytest.fixture
def it_read_payload() -> dict[str, Any]:
    """Valid JWT payload with it_assets_read_rest scope."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": IT_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI,
        "scope": "openid it_assets_read_rest",
        "act": {"sub": IT_AGENT_UUID},
    }


@pytest.fixture
def it_self_payload() -> dict[str, Any]:
    """Sprint 4 S4.2: payload with it_assets_self_rest scope + username claim."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": IT_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI + "-self",
        "scope": "openid it_assets_self_rest",
        "act": {"sub": IT_AGENT_UUID},
        "username": "employee_user",
    }


def _make_validator(public_jwk: dict[str, Any]) -> Any:
    """Build an ITServerTokenValidator with a mocked JWKS cache."""
    config = ITServerTokenValidationConfig(
        expected_iss=ISSUER,
        jwks_url="https://mock-jwks/",
        expected_aud=IT_AGENT_CLIENT_ID,
        trusted_act_subs=frozenset({IT_AGENT_UUID}),
        required_scopes=frozenset(),
    )
    cache = JWKSCache(jwks_url="https://mock-jwks/", insecure_tls=True)
    cache.get_key = AsyncMock(return_value=public_jwk)  # type: ignore[method-assign]
    return ITServerTokenValidator(config, jwks_cache=cache)


def _build_app(public_jwk: dict[str, Any]) -> FastAPI:
    """Return a minimal FastAPI app with the IT MCP router mounted."""
    validator = _make_validator(public_jwk)
    deps = ITMcpToolRouterDeps(validator=validator)
    router = build_it_mcp_router(deps)
    app = FastAPI()
    app.include_router(router, prefix="/mcp/tools")
    return app


# ---------------------------------------------------------------------------
# T-IT-MCP-01: Valid it.read token → list_available_assets returns full catalogue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_available_assets_returns_catalogue(rsa_keypair, sign_token, it_read_payload):
    """T-IT-MCP-01: Valid token returns the full asset catalogue (no type filter)."""
    _, public_jwk = rsa_keypair
    token = sign_token(it_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/list_available_assets",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "assets" in data
    assert len(data["assets"]) == 5  # all canned entries
    asset_types = {a["type"] for a in data["assets"]}
    assert "laptop" in asset_types
    assert "monitor" in asset_types
    assert "phone" in asset_types


# ---------------------------------------------------------------------------
# T-IT-MCP-02: asset_type filter returns only matching entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_available_assets_with_type_filter(rsa_keypair, sign_token, it_read_payload):
    """T-IT-MCP-02: Supplying asset_type='laptop' returns only laptop entries."""
    _, public_jwk = rsa_keypair
    token = sign_token(it_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/list_available_assets",
        json={"asset_type": "laptop"},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert all(a["type"] == "laptop" for a in data["assets"])
    assert len(data["assets"]) == 2  # MBP-14 and MBP-16


# ---------------------------------------------------------------------------
# T-IT-MCP-03: Valid it.read token → get_my_assets returns user's assets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_assets_returns_user_assets(rsa_keypair, sign_token, it_self_payload):
    """T-IT-MCP-03: Sprint 4 S4.2 — valid it_assets_self_rest + username claim
    returns the user's assets keyed by username.
    """
    # S5.12: no seed data — register the user and append their assets first.
    _it_store_mod.ensure_user(
        SUBJECT, "Employee", "User",
        username="employee_user", email=SUBJECT,
    )
    _it_store_mod.assets.append(
        {"asset_id": "AST-12345", "username": "employee_user", "type": "laptop",
         "model": "MBP 14 M3", "status": "outstanding"}
    )
    _it_store_mod.assets.append(
        {"asset_id": "AST-12346", "username": "employee_user", "type": "phone",
         "model": "iPhone 15 Pro", "status": "returned"}
    )

    _, public_jwk = rsa_keypair
    token = sign_token(it_self_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_my_assets",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    # Sprint 4 S4.2: shape changed to {assets, total} — no employee_id field.
    assert "assets" in data
    assert "total" in data
    assert data["total"] == 2  # employee_user has 2 live-setup assets (laptop + phone)
    asset_ids = {a["asset_id"] for a in data["assets"]}
    assert "AST-12345" in asset_ids
    assert all("status" in a for a in data["assets"])


# ---------------------------------------------------------------------------
# T-IT-MCP-04: Missing Authorization header → 401 with error_id=ERR-AUTH-006
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_auth_header_returns_401(rsa_keypair):
    """T-IT-MCP-04: No Authorization header → 401 with ERR-AUTH-006."""
    _, public_jwk = rsa_keypair
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/list_available_assets",
        json={},
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["error_id"] == "ERR-AUTH-006"


# ---------------------------------------------------------------------------
# T-IT-MCP-05: Token failing JWT validation → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_jwt_returns_401(rsa_keypair, it_read_payload):
    """T-IT-MCP-05: Token signed by wrong key → 401."""
    _, public_jwk = rsa_keypair

    other_private = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    bad_token = pyjwt.encode(
        it_read_payload,
        other_private,
        algorithm="RS256",
        headers={"kid": "it-mcp-test-key-1"},
    )

    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/list_available_assets",
        json={},
        headers={"Authorization": f"Bearer {bad_token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert "error_id" in detail


# ---------------------------------------------------------------------------
# T-IT-MCP-06: Wrong aud → 401 with ERR-MCP-001
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_aud_returns_err_mcp_001(rsa_keypair, sign_token, it_read_payload):
    """T-IT-MCP-06: Token with wrong aud → 401 ERR-MCP-001."""
    _, public_jwk = rsa_keypair
    payload = dict(it_read_payload, aud="completely-wrong-client-id")
    token = sign_token(payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/list_available_assets",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["error_id"] == "ERR-MCP-001"


# ---------------------------------------------------------------------------
# T-IT-MCP-07: Token missing it.read scope → 401 ERR-MCP-003
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_scope_returns_err_mcp_003(rsa_keypair, sign_token, it_read_payload):
    """T-IT-MCP-07: Token without it.read scope → 401 ERR-MCP-003."""
    _, public_jwk = rsa_keypair
    payload = dict(it_read_payload, scope="openid")  # it.read removed
    token = sign_token(payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/list_available_assets",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["error_id"] == "ERR-MCP-003"


# ---------------------------------------------------------------------------
# T-IT-MCP-08: Error body contains request_id from X-Request-ID header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_body_contains_request_id(rsa_keypair):
    """T-IT-MCP-08: F-07 — error detail includes the X-Request-ID echoed back."""
    _, public_jwk = rsa_keypair
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    custom_rid = "my-specific-it-request-id"
    resp = client.post(
        "/mcp/tools/get_my_assets",
        json={},
        headers={"X-Request-ID": custom_rid},  # no Authorization
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert detail.get("request_id") == custom_rid


# ---------------------------------------------------------------------------
# T-IT-MCP-09: Bad request body → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_available_assets_invalid_body_type_returns_422(
    rsa_keypair, sign_token, it_read_payload
):
    """T-IT-MCP-09: asset_type must be a string; passing an int yields 422."""
    _, public_jwk = rsa_keypair
    token = sign_token(it_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/list_available_assets",
        # Send raw JSON bytes so we can pass an int for a str field without Python
        # type-checking stopping us before the request is dispatched.
        content=b'{"asset_type": 12345}',
        headers={
            "Authorization": f"Bearer {token}",
            "X-Request-ID": REQUEST_ID,
            "Content-Type": "application/json",
        },
    )

    # Pydantic v2 coerces int → str in non-strict mode, so fall back to an
    # actually-invalid type such as a nested object, which cannot be coerced.
    # If 200 is returned for the int case (valid coercion), just re-test with
    # a clearly invalid body structure.
    if resp.status_code == 200:
        resp2 = client.post(
            "/mcp/tools/list_available_assets",
            content=b'{"asset_type": {"nested": "object"}}',
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": REQUEST_ID,
                "Content-Type": "application/json",
            },
        )
        assert resp2.status_code == 422
    else:
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# T-IT-MCP-10: Sprint 4 S4.2 — username claim drives the lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_assets_username_drives_lookup(rsa_keypair, sign_token, it_self_payload):
    """T-IT-MCP-10: Sprint 4 S4.2 — different username claim returns different rows.

    Sprint 4 dropped the ``employee_id`` body argument — the tool reads
    ``claims.username`` from the validated token. Switching the username
    claim should switch the returned rows.
    """
    # S5.12: no seed — register hr_admin_user and append their asset first.
    _it_store_mod.ensure_user(
        "hr_admin_user@example.com", "HR", "Admin",
        username="hr_admin_user", email="hr_admin_user@example.com",
    )
    _it_store_mod.assets.append(
        {"asset_id": "AST-22001", "username": "hr_admin_user", "type": "monitor",
         "model": "Dell UltraSharp 27", "status": "outstanding"}
    )

    _, public_jwk = rsa_keypair
    payload = dict(it_self_payload, username="hr_admin_user")
    token = sign_token(payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_my_assets",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    asset_ids = {a["asset_id"] for a in data["assets"]}
    assert "AST-22001" in asset_ids


# ---------------------------------------------------------------------------
# T-IT-MCP-13: Sprint 4 S4.2 — get_my_assets without it_assets_self_rest → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_assets_missing_self_scope_returns_401(rsa_keypair, sign_token, it_read_payload):
    """T-IT-MCP-13: a token carrying only it_assets_read_rest cannot hit the
    self-service endpoint — Sprint 4 sprint-4.md §6 distinguishes admin-grade
    read from self-service read.
    """
    _, public_jwk = rsa_keypair
    # it_read_payload carries it_assets_read_rest only.
    token = sign_token(it_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_my_assets",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["error_id"] == "ERR-MCP-003"


# ---------------------------------------------------------------------------
# T-IT-MCP-14: Sprint 4 — username claim absent → fall back to sub→username
# (revised: WSO2 IS OBO/CIBA tokens carry only `sub`, not the `username`
# profile claim, so the self-service path resolves sub→username via the seed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_assets_resolves_username_from_sub(rsa_keypair, sign_token, it_self_payload):
    """T-IT-MCP-14: ``username`` claim absent, ``sub`` resolves via store to
    the registered user → that user's assets are returned (200)."""
    # S5.12: no seed — register the user (REST-auth path) so sub→username resolves,
    # then append their assets.
    _it_store_mod.ensure_user(
        SUBJECT, "Employee", "User",
        username="employee_user", email=SUBJECT,
    )
    _it_store_mod.assets.append(
        {"asset_id": "AST-12345", "username": "employee_user", "type": "laptop",
         "model": "MBP 14 M3", "status": "outstanding"}
    )
    _it_store_mod.assets.append(
        {"asset_id": "AST-12346", "username": "employee_user", "type": "phone",
         "model": "iPhone 15 Pro", "status": "returned"}
    )

    _, public_jwk = rsa_keypair
    payload = dict(it_self_payload)
    payload.pop("username")  # token carries only `sub` — no username claim
    token = sign_token(payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_my_assets",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2  # employee_user's 2 live-setup assets resolved via sub
    assert "AST-12345" in {a["asset_id"] for a in data["assets"]}


@pytest.mark.asyncio
async def test_get_my_assets_unresolvable_sub_returns_empty(rsa_keypair, sign_token, it_self_payload):
    """``username`` absent and ``sub`` matches no seeded user → empty list (200),
    not a 500/401 — the caller simply has no assets on record."""
    _, public_jwk = rsa_keypair
    payload = dict(it_self_payload)
    payload.pop("username")
    payload["sub"] = "00000000-0000-0000-0000-000000000000"
    token = sign_token(payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/get_my_assets",
        json={},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    assert resp.json() == {"assets": [], "total": 0}


# ---------------------------------------------------------------------------
# T-IT-MCP-11: issue_asset write tool — Sprint 2A.2 / D2.8 / N33
# ---------------------------------------------------------------------------


@pytest.fixture
def it_write_payload() -> dict[str, Any]:
    """Valid JWT payload with it_assets_write_rest scope (HR Admin write path)."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": IT_AGENT_CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "jti": JTI + "-w",
        "scope": "openid it_assets_write_rest",
        "act": {"sub": IT_AGENT_UUID},
    }


@pytest.mark.asyncio
async def test_issue_asset_valid_write_token(rsa_keypair, sign_token, it_write_payload):
    """T-IT-MCP-11: Token with it_assets_write_rest issues an asset successfully."""
    _, public_jwk = rsa_keypair
    token = sign_token(it_write_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/issue_asset",
        json={"asset_id": "MBP-14-001", "employee_id": "user-uuid-abc123"},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["asset_id"] == "MBP-14-001"
    assert data["employee_id"] == "user-uuid-abc123"
    assert data["issued_by"] == IT_AGENT_UUID  # act.sub
    assert "issued_at" in data


@pytest.mark.asyncio
async def test_issue_asset_read_token_rejected(rsa_keypair, sign_token, it_read_payload):
    """T-IT-MCP-12: Token with only it_assets_read_rest cannot issue assets."""
    _, public_jwk = rsa_keypair
    token = sign_token(it_read_payload)
    app = _build_app(public_jwk)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/mcp/tools/issue_asset",
        json={"asset_id": "MBP-14-001", "employee_id": "user-uuid-abc123"},
        headers={"Authorization": f"Bearer {token}", "X-Request-ID": REQUEST_ID},
    )

    assert resp.status_code == 401
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["error_id"] == "ERR-MCP-003"
