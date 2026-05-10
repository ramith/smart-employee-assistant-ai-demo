"""Tests for hr_server/main.py — Sprint 1 Wave 8.

Test count: 6 tests (>= 4 required).

Catalog:
    T-HR-MAIN-01  create_app returns a FastAPI instance
    T-HR-MAIN-02  MCP tool routes are mounted (get_leave_balance, get_leave_history, approve_leave)
    T-HR-MAIN-03  GET /healthz returns 200 with service="hr_server"
    T-HR-MAIN-04  F-15 startup log: caplog captures expected_aud= at INFO level
    T-HR-MAIN-05  CorrelationIdMiddleware adds X-Request-ID to responses
    T-HR-MAIN-06  create_app with explicit config does not call HRServerConfig.from_env()

Strategy
--------
``hr_server/main.py`` imports ``HRServerTokenValidator`` and ``build_hr_mcp_router``
at the top level.  ``HRServerTokenValidator`` transitively imports
``common.auth.jwt_validator`` which requires PyJWT.  Rather than pulling in the
full JWT stack (not installed in the test runner), we:

  1. Load ``hr_server.config`` directly (no JWT dependency).
  2. Stub ``hr_server.auth.validators`` in sys.modules with a lightweight mock
     that satisfies ``from hr_server.auth.validators import HRServerTokenValidator``.
  3. Stub ``hr_server.mcp.tools``'s ``build_hr_mcp_router`` with a no-op router so
     tests focus solely on the wiring in ``main.py``.

This is the same isolation pattern used by test_tools.py (which injects deps
via HRMcpToolRouterDeps) and test_validators.py (which mocks JWKSCache).
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys
import types as _types
from unittest.mock import MagicMock, patch

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
    ("hr_server", "hr_server"),
    ("hr_server.auth", "hr_server/auth"),
    ("hr_server.mcp", "hr_server/mcp"),
):
    _ensure_pkg(_pkg, _rel)

# ---------------------------------------------------------------------------
# Load modules that have NO jwt dependency
# ---------------------------------------------------------------------------

_errors_mod = _load("common.auth.errors", "common/auth/errors.py")
_wso2_is_client_mod = _load("common.auth.wso2_is_client", "common/auth/wso2_is_client.py")
_correlation_mod = _load("common.logging.correlation", "common/logging/correlation.py")
_redaction_mod = _load("common.logging.redaction", "common/logging/redaction.py")
_hr_config_mod = _load("hr_server.config", "hr_server/config.py")

# ---------------------------------------------------------------------------
# Stub hr_server.auth.validators  (bypasses jwt dependency)
# ---------------------------------------------------------------------------

class _MockHRValidator:
    """Minimal validator stub: log_startup_assertion emits an INFO log."""

    def __init__(self, expected_aud: str) -> None:
        self._expected_aud = expected_aud

    def log_startup_assertion(self) -> None:
        logging.getLogger("hr_server.auth.validators").info(
            "token_validator.startup expected_aud=%s trusted_act_subs=%s",
            self._expected_aud,
            frozenset(),
        )

    def attach_revocation(self, state) -> None:  # noqa: D401, ARG002 — Sprint 3 3A.3 stub
        """No-op for create_app() smoke tests; real wiring is in validator."""
        return None


class _MockHRValidatorClass:
    """Mimics the HRServerTokenValidator class (from_config classmethod)."""

    @classmethod
    def from_config(cls, server_config: object) -> "_MockHRValidator":
        return _MockHRValidator(expected_aud=getattr(server_config, "expected_aud", ""))


_validators_stub = _types.ModuleType("hr_server.auth.validators")
_validators_stub.__package__ = "hr_server.auth"
_validators_stub.HRServerTokenValidator = _MockHRValidatorClass  # type: ignore[attr-defined]
sys.modules["hr_server.auth.validators"] = _validators_stub

# ---------------------------------------------------------------------------
# Stub hr_server.mcp.tools  (build_hr_mcp_router returns a plain APIRouter)
# ---------------------------------------------------------------------------

def _stub_build_hr_mcp_router(deps: object) -> APIRouter:  # noqa: ARG001
    """Return a minimal router with the three expected tool route paths."""
    router = APIRouter()

    @router.post("/get_leave_balance")
    async def _get_leave_balance() -> dict:
        return {}

    @router.post("/get_leave_history")
    async def _get_leave_history() -> dict:
        return {}

    @router.post("/approve_leave")
    async def _approve_leave() -> dict:
        return {}

    return router


class _StubHRMcpToolRouterDeps:
    def __init__(self, *, validator: object) -> None:
        self.validator = validator


_tools_stub = _types.ModuleType("hr_server.mcp.tools")
_tools_stub.__package__ = "hr_server.mcp"
_tools_stub.build_hr_mcp_router = _stub_build_hr_mcp_router  # type: ignore[attr-defined]
_tools_stub.HRMcpToolRouterDeps = _StubHRMcpToolRouterDeps  # type: ignore[attr-defined]
sys.modules["hr_server.mcp.tools"] = _tools_stub

# ---------------------------------------------------------------------------
# NOW load main.py  (its imports are satisfied by stubs above)
# ---------------------------------------------------------------------------

_hr_main_mod = _load("hr_server.main", "hr_server/main.py")

HRServerConfig: type = _hr_config_mod.HRServerConfig
create_app = _hr_main_mod.create_app

# ---------------------------------------------------------------------------
# Minimal env dict — avoids touching os.environ
# ---------------------------------------------------------------------------

_MIN_ENV: dict[str, str] = {
    "WSO2_IS_BASE_URL": "https://is.example.com:9443",
    "HR_SERVER_EXPECTED_AUD": "hr_agent-oauth-client-id-test",
    "HR_SERVER_TRUSTED_PEER_AGENTS": "hr_agent-uuid-0001",
    "HR_SERVER_REQUIRED_SCOPES": "hr.read",
    "HR_SERVER_HOST": "127.0.0.1",
    "HR_SERVER_PORT": "8000",
}


def _make_config() -> object:
    return HRServerConfig.from_env(environ=_MIN_ENV)


# ---------------------------------------------------------------------------
# T-HR-MAIN-01: create_app returns a FastAPI instance
# ---------------------------------------------------------------------------


def test_create_app_returns_fastapi_instance() -> None:
    """T-HR-MAIN-01: create_app() with an explicit config returns a FastAPI app."""
    cfg = _make_config()
    app = create_app(config=cfg)
    assert isinstance(app, FastAPI)


# ---------------------------------------------------------------------------
# T-HR-MAIN-02: MCP tool routes are mounted
# ---------------------------------------------------------------------------


def test_mcp_tool_routes_are_mounted() -> None:
    """T-HR-MAIN-02: All three HR MCP tool endpoints appear in the route table."""
    cfg = _make_config()
    app = create_app(config=cfg)

    route_paths = {getattr(r, "path", "") for r in app.routes}

    assert "/mcp/tools/get_leave_balance" in route_paths
    assert "/mcp/tools/get_leave_history" in route_paths
    assert "/mcp/tools/approve_leave" in route_paths


# ---------------------------------------------------------------------------
# T-HR-MAIN-03: GET /healthz returns 200 with correct service name
# ---------------------------------------------------------------------------


def test_healthz_returns_200_with_service_name() -> None:
    """T-HR-MAIN-03: /healthz returns HTTP 200 and {"ok": True, "service": "hr_server"}."""
    cfg = _make_config()
    app = create_app(config=cfg)
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["service"] == "hr_server"


# ---------------------------------------------------------------------------
# T-HR-MAIN-04: F-15 startup log captures expected_aud at INFO
# ---------------------------------------------------------------------------


def test_f15_startup_log_captures_expected_aud(caplog: pytest.LogCaptureFixture) -> None:
    """T-HR-MAIN-04: validator.log_startup_assertion() fires and emits expected_aud at INFO (F-15/N28)."""
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
# T-HR-MAIN-05: CorrelationIdMiddleware adds X-Request-ID to responses
# ---------------------------------------------------------------------------


def test_correlation_middleware_adds_x_request_id() -> None:
    """T-HR-MAIN-05: Responses include X-Request-ID echo from CorrelationIdMiddleware (F-13)."""
    cfg = _make_config()
    app = create_app(config=cfg)
    client = TestClient(app, raise_server_exceptions=True)

    custom_rid = "test-correlation-hr-main-001"
    resp = client.get("/healthz", headers={"X-Request-ID": custom_rid})

    assert resp.status_code == 200
    assert resp.headers.get("x-request-id") == custom_rid


# ---------------------------------------------------------------------------
# T-HR-MAIN-06: create_app with explicit config bypasses from_env()
# ---------------------------------------------------------------------------


def test_create_app_explicit_config_skips_from_env() -> None:
    """T-HR-MAIN-06: Supplying config to create_app() must not trigger from_env()."""
    cfg = _make_config()

    original_from_env = HRServerConfig.from_env
    call_count: list[int] = [0]

    def _spy(*args: object, **kwargs: object) -> object:
        call_count[0] += 1
        return original_from_env(*args, **kwargs)  # type: ignore[arg-type]

    with patch.object(HRServerConfig, "from_env", side_effect=_spy):
        create_app(config=cfg)

    assert call_count[0] == 0, "from_env() was called even though config was supplied"
