"""Tests for it_server/main.py — Sprint 1 Wave 8.

Mirror of tests/hr_server/test_main.py with IT* types and service name
"it_server".

Test count: 6 tests (>= 4 required).

Catalog:
    T-IT-MAIN-01  create_app returns a FastAPI instance
    T-IT-MAIN-02  MCP tool routes are mounted (list_available_assets, get_my_assets)
    T-IT-MAIN-03  GET /healthz returns 200 with service="it_server"
    T-IT-MAIN-04  F-15 startup log: caplog captures expected_aud= at INFO level
    T-IT-MAIN-05  CorrelationIdMiddleware adds X-Request-ID to responses
    T-IT-MAIN-06  create_app with explicit config does not call ITServerConfig.from_env()

Strategy
--------
Identical to tests/hr_server/test_main.py.  ``ITServerTokenValidator`` and
``build_it_mcp_router`` are stubbed in sys.modules to avoid the PyJWT
transitive dependency.  Only modules with no jwt dependency (config, correlation,
redaction, wso2_is_client, errors) are loaded from the real source files.
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys
import types as _types
from unittest.mock import patch

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Path roots
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted: str, rel_dir: str | None = None) -> None:
    """Register a bare package namespace in sys.modules if not already present."""
    if dotted in sys.modules:
        return
    stub = _types.ModuleType(dotted)
    stub.__package__ = dotted
    path = rel_dir or dotted.replace(".", "/")
    stub.__path__ = [str(_ROOT / path)]  # type: ignore[assignment]
    sys.modules[dotted] = stub


def _load(dotted: str, rel: str) -> _types.ModuleType:
    """Load a single .py file under *dotted* name, bypassing __init__.py."""
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, _ROOT / rel)
    assert spec and spec.loader, f"Cannot find {rel}"
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Bootstrap package namespaces
# ---------------------------------------------------------------------------

for _pkg, _rel in (
    ("common", None),
    ("common.auth", None),
    ("common.logging", None),
    ("it_server", "it_server"),
    ("it_server.auth", "it_server/auth"),
    ("it_server.mcp", "it_server/mcp"),
    ("it_server.rest_api", "it_server/rest_api"),
):
    _ensure_pkg(_pkg, _rel)

# ---------------------------------------------------------------------------
# Load modules that have NO jwt dependency
# ---------------------------------------------------------------------------

_errors_mod = _load("common.auth.errors", "common/auth/errors.py")
_wso2_is_client_mod = _load("common.auth.wso2_is_client", "common/auth/wso2_is_client.py")
_correlation_mod = _load("common.logging.correlation", "common/logging/correlation.py")
_redaction_mod = _load("common.logging.redaction", "common/logging/redaction.py")
_it_config_mod = _load("it_server.config", "it_server/config.py")

# ---------------------------------------------------------------------------
# Stub it_server.auth.validators  (bypasses jwt dependency)
# ---------------------------------------------------------------------------

class _MockITValidator:
    """Minimal validator stub: log_startup_assertion emits an INFO log."""

    def __init__(self, expected_aud: str) -> None:
        self._expected_aud = expected_aud

    def log_startup_assertion(self) -> None:
        logging.getLogger("it_server.auth.validators").info(
            "token_validator.startup expected_aud=%s trusted_act_subs=%s",
            self._expected_aud,
            frozenset(),
        )

    def attach_revocation(self, state) -> None:  # noqa: D401, ARG002 — Sprint 3 3A.3 stub
        """No-op for create_app() smoke tests; real wiring is in validator."""
        return None


class _MockITValidatorClass:
    """Mimics the ITServerTokenValidator class (from_config classmethod)."""

    @classmethod
    def from_config(cls, server_config: object) -> "_MockITValidator":
        return _MockITValidator(expected_aud=getattr(server_config, "expected_aud", ""))


_validators_stub = _types.ModuleType("it_server.auth.validators")
_validators_stub.__package__ = "it_server.auth"
_validators_stub.ITServerTokenValidator = _MockITValidatorClass  # type: ignore[attr-defined]
sys.modules["it_server.auth.validators"] = _validators_stub

# ---------------------------------------------------------------------------
# Stub it_server.mcp.tools  (build_it_mcp_router returns a plain APIRouter)
# ---------------------------------------------------------------------------

def _stub_build_it_mcp_router(deps: object) -> APIRouter:  # noqa: ARG001
    """Return a minimal router with the two expected tool route paths."""
    router = APIRouter()

    @router.post("/list_available_assets")
    async def _list_available_assets() -> dict:
        return {}

    @router.post("/get_my_assets")
    async def _get_my_assets() -> dict:
        return {}

    return router


class _StubITMcpToolRouterDeps:
    def __init__(self, *, validator: object) -> None:
        self.validator = validator


_tools_stub = _types.ModuleType("it_server.mcp.tools")
_tools_stub.__package__ = "it_server.mcp"
_tools_stub.build_it_mcp_router = _stub_build_it_mcp_router  # type: ignore[attr-defined]
_tools_stub.ITMcpToolRouterDeps = _StubITMcpToolRouterDeps  # type: ignore[attr-defined]
sys.modules["it_server.mcp.tools"] = _tools_stub

# ---------------------------------------------------------------------------
# Stub it_server.auth.jwt_validator  (bypass pyjwt + httpx import)
# ---------------------------------------------------------------------------


class _MockRestValidator:
    """Minimal REST validator stub. validate_token() is unreachable here."""

    def __init__(self, audiences: list) -> None:
        self.audience = audiences


def _stub_build_validator_from_config(cfg: object) -> _MockRestValidator:
    aud = getattr(cfg, "expected_aud", "")
    return _MockRestValidator(audiences=[aud] if aud else [])


_jwt_validator_stub = _types.ModuleType("it_server.auth.jwt_validator")
_jwt_validator_stub.__package__ = "it_server.auth"
_jwt_validator_stub.build_validator_from_config = _stub_build_validator_from_config  # type: ignore[attr-defined]
_jwt_validator_stub.JWTValidator = _MockRestValidator  # type: ignore[attr-defined]
sys.modules["it_server.auth.jwt_validator"] = _jwt_validator_stub

# ---------------------------------------------------------------------------
# Stub it_server.rest_api.server  (build_rest_router returns a plain APIRouter)
# ---------------------------------------------------------------------------


def _stub_build_rest_router(deps: object) -> APIRouter:  # noqa: ARG001
    router = APIRouter()

    @router.get("/health")
    async def _rest_health() -> dict:
        return {"status": "ok"}

    return router


class _StubITRestRouterDeps:
    def __init__(self, *, validator: object) -> None:
        self.validator = validator


_rest_stub = _types.ModuleType("it_server.rest_api.server")
_rest_stub.__package__ = "it_server.rest_api"
_rest_stub.build_rest_router = _stub_build_rest_router  # type: ignore[attr-defined]
_rest_stub.ITRestRouterDeps = _StubITRestRouterDeps  # type: ignore[attr-defined]
sys.modules["it_server.rest_api.server"] = _rest_stub

# ---------------------------------------------------------------------------
# NOW load main.py  (its imports are satisfied by stubs above)
# ---------------------------------------------------------------------------

_it_main_mod = _load("it_server.main", "it_server/main.py")

ITServerConfig: type = _it_config_mod.ITServerConfig
create_app = _it_main_mod.create_app

# ---------------------------------------------------------------------------
# Minimal env dict — avoids touching os.environ
# ---------------------------------------------------------------------------

_MIN_ENV: dict[str, str] = {
    "WSO2_IS_BASE_URL": "https://is.example.com:9443",
    "IT_SERVER_EXPECTED_AUD": "it_agent-oauth-client-id-test",
    "IT_SERVER_TRUSTED_PEER_AGENTS": "it_agent-uuid-0001",
    "IT_SERVER_REQUIRED_SCOPES": "it.read",
    "IT_SERVER_HOST": "127.0.0.1",
    "IT_SERVER_PORT": "8004",
}


def _make_config() -> object:
    return ITServerConfig.from_env(environ=_MIN_ENV)


# ---------------------------------------------------------------------------
# T-IT-MAIN-01: create_app returns a FastAPI instance
# ---------------------------------------------------------------------------


def test_create_app_returns_fastapi_instance() -> None:
    """T-IT-MAIN-01: create_app() with an explicit config returns a FastAPI app."""
    cfg = _make_config()
    app = create_app(config=cfg)
    assert isinstance(app, FastAPI)


# ---------------------------------------------------------------------------
# T-IT-MAIN-02: MCP tool routes are mounted
# ---------------------------------------------------------------------------


def test_mcp_tool_routes_are_mounted() -> None:
    """T-IT-MAIN-02: Both IT MCP tool endpoints appear in the route table."""
    cfg = _make_config()
    app = create_app(config=cfg)

    route_paths = {getattr(r, "path", "") for r in app.routes}

    assert "/mcp/tools/list_available_assets" in route_paths
    assert "/mcp/tools/get_my_assets" in route_paths


# ---------------------------------------------------------------------------
# T-IT-MAIN-03: GET /healthz returns 200 with correct service name
# ---------------------------------------------------------------------------


def test_healthz_returns_200_with_service_name() -> None:
    """T-IT-MAIN-03: /healthz returns HTTP 200 and {"ok": True, "service": "it_server"}."""
    cfg = _make_config()
    app = create_app(config=cfg)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["service"] == "it_server"


# ---------------------------------------------------------------------------
# T-IT-MAIN-04: F-15 startup log captures expected_aud at INFO
# ---------------------------------------------------------------------------


def test_f15_startup_log_captures_expected_aud(caplog: pytest.LogCaptureFixture) -> None:
    """T-IT-MAIN-04: validator.log_startup_assertion() fires and emits expected_aud at INFO (F-15/N28)."""
    cfg = _make_config()

    with caplog.at_level(logging.INFO):
        create_app(config=cfg)

    startup_records = [
        r for r in caplog.records
        if r.levelno == logging.INFO and getattr(cfg, "expected_aud", "") in r.getMessage()
    ]
    assert startup_records, (
        f"No INFO record containing expected_aud={getattr(cfg, 'expected_aud', '')!r}. "
        f"All records: {[r.getMessage() for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# T-IT-MAIN-05: CorrelationIdMiddleware adds X-Request-ID to responses
# ---------------------------------------------------------------------------


def test_correlation_middleware_adds_x_request_id() -> None:
    """T-IT-MAIN-05: Responses include X-Request-ID echo from CorrelationIdMiddleware (F-13)."""
    cfg = _make_config()
    app = create_app(config=cfg)
    client = TestClient(app, raise_server_exceptions=True)

    custom_rid = "test-correlation-it-main-001"
    resp = client.get("/healthz", headers={"X-Request-ID": custom_rid})

    assert resp.status_code == 200
    assert resp.headers.get("x-request-id") == custom_rid


# ---------------------------------------------------------------------------
# T-IT-MAIN-06: create_app with explicit config bypasses from_env()
# ---------------------------------------------------------------------------


def test_create_app_explicit_config_skips_from_env() -> None:
    """T-IT-MAIN-06: Supplying config to create_app() must not trigger from_env()."""
    cfg = _make_config()

    original_from_env = ITServerConfig.from_env
    call_count: list[int] = [0]

    def _spy(*args: object, **kwargs: object) -> object:
        call_count[0] += 1
        return original_from_env(*args, **kwargs)  # type: ignore[arg-type]

    with patch.object(ITServerConfig, "from_env", side_effect=_spy):
        create_app(config=cfg)

    assert call_count[0] == 0, "from_env() was called even though config was supplied"
