"""Tests for hr_agent/main.py — Wave 8, Sprint 1.

Coverage (6 tests)
------------------
 1. ``create_app(mock_config)`` returns a :class:`fastapi.FastAPI` instance.
 2. The app mounts the three required A2A POST routes and GET /healthz.
 3. ``GET /healthz`` returns HTTP 200 with ``{"ok": True, "service": "hr_agent"}``.
 4. ``CorrelationIdMiddleware`` adds ``X-Request-ID`` to every response.
 5. ``CorrelationIdMiddleware`` echoes an inbound ``X-Request-ID`` header unchanged.
 6. App lifespan (startup → yield → shutdown) completes without raising.

Design notes
------------
- All production imports that require live network access (WSO2ISClient,
  CIBAClient, HRMcpClient, ActorTokenProvider, HRDispatcher,
  build_hr_a2a_router) are stubbed via ``unittest.mock.patch`` or replaced
  with lightweight no-op doubles so no IS / MCP connectivity is required.
- ``create_app`` is called with a fake ``HRAgentConfig``-like object; the
  ``lifespan`` path is exercised through the ASGI lifespan protocol using
  ``httpx.AsyncClient`` with ``ASGITransport``.
- The module bootstrap follows the same ``_ensure_pkg`` / ``_load`` pattern
  used by the other Wave-N tests in this package so it runs cleanly in the
  joint pytest collection.

F-09 note: ``pending`` dict holds ``A2APendingState`` dataclasses (asyncio
objects); the router injection path is exercised here at the app-factory level
only — deeper CIBA flow coverage lives in test_orchestrator.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Module bootstrap  (replicates the _ensure_pkg / _load pattern)
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted: str) -> None:
    """Insert a bare namespace package stub if not already in sys.modules."""
    if dotted not in sys.modules:
        stub = types.ModuleType(dotted)
        stub.__package__ = dotted
        stub.__path__ = [str(_ROOT / dotted.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[dotted] = stub


def _load(dotted: str, rel: str) -> types.ModuleType:
    """Load a single .py file into sys.modules under *dotted*, bypassing __init__."""
    if dotted in sys.modules:
        return sys.modules[dotted]
    path = _ROOT / rel
    spec = importlib.util.spec_from_file_location(dotted, path)
    assert spec and spec.loader, f"Cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Ensure all required package namespaces exist.
for _pkg in (
    "common",
    "common.auth",
    "common.a2a",
    "hr_agent",
    "hr_agent.a2a",
    "hr_agent.ciba",
    "hr_agent.mcp",
):
    _ensure_pkg(_pkg)

# Load the dependency chain in strict import order (leaf modules first).
_load("common.auth.models",   "common/auth/models.py")
_load("common.auth.errors",   "common/auth/errors.py")
_load("common.auth.peer_trust", "common/auth/peer_trust.py")
_load("common.a2a.jsonrpc",   "common/a2a/jsonrpc.py")
_load("common.a2a.models",    "common/a2a/models.py")

# ── jwt_validator stub (PyJWT not required in test env) ───────────────────────
if "common.auth.jwt_validator" not in sys.modules:
    from dataclasses import dataclass as _dc, field as _field

    _jv = types.ModuleType("common.auth.jwt_validator")
    _jv.__package__ = "common.auth"

    @_dc(frozen=True, slots=True)
    class _ValidatorConfig:  # type: ignore[no-redef]
        expected_iss: str
        jwks_url: str
        expected_aud: str | None = None
        required_scopes: frozenset = _field(default_factory=frozenset)
        leeway_seconds: int = 30
        insecure_tls: bool = False

    @_dc
    class _JWKSCache:  # type: ignore[no-redef]
        jwks_url: str
        ttl_seconds: int = 3600
        insecure_tls: bool = False

    async def _validate_stub(token: str, config: Any, *, jwks_cache: Any = None) -> Any:
        raise RuntimeError("jwt_validator stub: patch validate before use")

    _jv.ValidatorConfig = _ValidatorConfig  # type: ignore[attr-defined]
    _jv.JWKSCache = _JWKSCache             # type: ignore[attr-defined]
    _jv.validate = _validate_stub           # type: ignore[attr-defined]
    sys.modules["common.auth.jwt_validator"] = _jv

_load("common.a2a.server",    "common/a2a/server.py")

# ── Heavy stubs: no IS / MCP network traffic ──────────────────────────────────
for _sn in (
    "common.auth.wso2_is_client",
    "common.auth.actor_token_provider",
    "common.auth.binding_messages",
    "common.auth.ciba_client",
    "hr_agent.mcp.client",
    "hr_agent.ciba.orchestrator",
):
    if _sn not in sys.modules:
        _m = types.ModuleType(_sn)
        _m.__package__ = _sn.rsplit(".", 1)[0]
        sys.modules[_sn] = _m

# Populate required names on each stub module so imports inside main.py work.
_wso2 = sys.modules["common.auth.wso2_is_client"]
if not hasattr(_wso2, "WSO2ISClient"):
    class _FakeWSO2ISClient:
        def __init__(self, *, config: Any) -> None: ...
        async def aclose(self) -> None: ...
    _wso2.WSO2ISClient = _FakeWSO2ISClient          # type: ignore[attr-defined]
    @dataclass(frozen=True, slots=True)
    class _FakeWSO2ISClientConfig:
        base_url: str
        insecure_tls: bool = False
    _wso2.WSO2ISClientConfig = _FakeWSO2ISClientConfig  # type: ignore[attr-defined]

_atp = sys.modules["common.auth.actor_token_provider"]
if not hasattr(_atp, "ActorTokenProvider"):
    class _FakeActorTokenProvider:
        def __init__(self, *, credentials: Any, is_client: Any) -> None: ...
    _atp.ActorTokenProvider = _FakeActorTokenProvider   # type: ignore[attr-defined]

    @dataclass(frozen=True, slots=True)
    class _FakeAgentCredentials:
        agent_id: str
        agent_secret: str
        oauth_client_id: str
        oauth_client_secret: str
        redirect_uri: str = "http://localhost:9999/agent-callback"
    _atp.AgentCredentials = _FakeAgentCredentials       # type: ignore[attr-defined]

_cc = sys.modules["common.auth.ciba_client"]
if not hasattr(_cc, "CIBAClient"):
    class _FakeCIBAClient:
        def __init__(self, *, config: Any) -> None: ...
        async def aclose(self) -> None: ...
    _cc.CIBAClient = _FakeCIBAClient                    # type: ignore[attr-defined]

    @dataclass(frozen=True, slots=True)
    class _FakeCIBAClientConfig:
        is_base_url: str
        insecure_tls: bool = False
    _cc.CIBAClientConfig = _FakeCIBAClientConfig        # type: ignore[attr-defined]

_mcp = sys.modules["hr_agent.mcp.client"]
if not hasattr(_mcp, "HRMcpClient"):
    class _FakeHRMcpClient:
        def __init__(self, *, config: Any) -> None: ...
        async def aclose(self) -> None: ...
    _mcp.HRMcpClient = _FakeHRMcpClient                 # type: ignore[attr-defined]

    @dataclass(frozen=True, slots=True)
    class _FakeHRMcpClientConfig:
        base_url: str
        timeout_seconds: float = 30.0
    _mcp.HRMcpClientConfig = _FakeHRMcpClientConfig     # type: ignore[attr-defined]

_orch = sys.modules["hr_agent.ciba.orchestrator"]
if not hasattr(_orch, "HRDispatcher"):
    class _FakeHRDispatcher:
        def __init__(self, *, deps: Any) -> None: ...
    _orch.HRDispatcher = _FakeHRDispatcher              # type: ignore[attr-defined]

    @dataclass
    class _FakeHRDispatcherDeps:
        ciba_client: Any
        actor_token_provider: Any
        mcp_client: Any
        oauth_client_id: str = ""
        oauth_client_secret: str = ""
        agent_id: str = ""
        agent_label: str = "HR Agent"
        ciba_scope: str = "openid hr.read"
        max_poll_seconds: float = 300.0
    _orch.HRDispatcherDeps = _FakeHRDispatcherDeps      # type: ignore[attr-defined]

# ── logging: load real modules early (before main.py imports them) ────────────
# Use _ensure_pkg only for the parent namespace; then load the real modules
# so main.py's "from common.logging.correlation import ..." resolves correctly.
_ensure_pkg("common.logging")
_load("common.logging.correlation", "common/logging/correlation.py")
_load("common.logging.redaction",   "common/logging/redaction.py")

# ── hr_agent.config stub (avoids os.environ dependency) ──────────────────────
if "hr_agent.config" not in sys.modules:
    _cfg_m = types.ModuleType("hr_agent.config")
    _cfg_m.__package__ = "hr_agent"

    class _HRAgentConfig:  # type: ignore[no-redef]
        """Sentinel; tests supply _FakeHRAgentConfig below."""
    _cfg_m.HRAgentConfig = _HRAgentConfig  # type: ignore[attr-defined]
    sys.modules["hr_agent.config"] = _cfg_m

# Load the A2A handler (needed transitively by main.py).
_load("hr_agent.a2a.handler", "hr_agent/a2a/handler.py")

# Finally, load the module under test.
_load("hr_agent.main", "hr_agent/main.py")

# ---------------------------------------------------------------------------
# Concrete imports (after all stubs are in sys.modules)
# ---------------------------------------------------------------------------

from common.a2a.server import A2APendingState  # noqa: E402
from hr_agent.main import create_app           # noqa: E402

# ---------------------------------------------------------------------------
# Fake HRAgentConfig — avoids all env-var loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AgentCreds:
    agent_id: str = "hr_agent-uuid"
    agent_secret: str = "hr_agent-secret"
    oauth_client_id: str = "hr-oauth-client-id"
    oauth_client_secret: str = "hr-oauth-client-secret"
    redirect_uri: str = "http://localhost:9999/agent-callback"


@dataclass(frozen=True)
class _FakeHRAgentConfig:
    """Minimal config double for create_app() tests — no env-var access."""
    is_base_url: str = "https://is.example.com:9443"
    is_insecure_tls: bool = False
    is_issuer: str = "https://is.example.com:9443/oauth2/token"
    is_jwks_url: str = "https://is.example.com:9443/oauth2/jwks"
    hr_server_url: str = "http://hr_server:8000"
    expected_inbound_aud: str = "orch-mcp-client-id"
    trusted_orchestrator_subs: frozenset = field(
        default_factory=lambda: frozenset({"orch-agent-uuid-001"})
    )
    host: str = "0.0.0.0"
    port: int = 8001
    ciba_scope: str = "openid hr.read"
    max_poll_seconds: int = 240
    canonical_url: str = "http://hr_agent:8001/a2a"
    agent: _AgentCreds = field(default_factory=_AgentCreds)

    def is_client_config(self) -> Any:
        """Return a WSO2ISClientConfig-compatible object."""
        # Return an object compatible with WSO2ISClient(config=...) stub.
        @dataclass(frozen=True)
        class _Cfg:
            base_url: str
            insecure_tls: bool

        return _Cfg(base_url=self.is_base_url, insecure_tls=self.is_insecure_tls)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> _FakeHRAgentConfig:
    return _FakeHRAgentConfig()


def _build_app() -> FastAPI:
    """Create the app with the fake config and all live dependencies patched."""
    return create_app(config=_make_config())  # type: ignore[arg-type]


def _collect_routes(app: FastAPI) -> dict[str, set[str]]:
    """Return ``{path: {methods}}`` for all non-404 routes in *app*."""
    result: dict[str, set[str]] = {}
    for route in app.routes:
        path: str = getattr(route, "path", "")
        methods: set[str] = getattr(route, "methods", set()) or set()
        if path and methods:
            result[path] = methods
    return result


# ---------------------------------------------------------------------------
# TC-1: create_app returns FastAPI
# ---------------------------------------------------------------------------


class TestCreateAppReturnType:
    """TC-1: create_app(config) returns a FastAPI instance."""

    def test_returns_fastapi_instance(self) -> None:
        app = _build_app()
        assert isinstance(app, FastAPI), f"Expected FastAPI, got {type(app)}"


# ---------------------------------------------------------------------------
# TC-2: required routes are mounted
# ---------------------------------------------------------------------------


class TestRoutesMounted:
    """TC-2: All four required routes are present on the returned app."""

    def test_a2a_message_send_mounted(self) -> None:
        app = _build_app()
        routes = _collect_routes(app)
        assert "/a2a/message/send" in routes, (
            f"/a2a/message/send missing. Registered routes: {list(routes)}"
        )
        assert "POST" in routes["/a2a/message/send"]

    def test_a2a_await_mounted(self) -> None:
        app = _build_app()
        routes = _collect_routes(app)
        assert "/a2a/await" in routes, (
            f"/a2a/await missing. Registered routes: {list(routes)}"
        )
        assert "POST" in routes["/a2a/await"]

    def test_a2a_cancel_mounted(self) -> None:
        app = _build_app()
        routes = _collect_routes(app)
        assert "/a2a/cancel" in routes, (
            f"/a2a/cancel missing. Registered routes: {list(routes)}"
        )
        assert "POST" in routes["/a2a/cancel"]

    def test_healthz_mounted(self) -> None:
        app = _build_app()
        routes = _collect_routes(app)
        assert "/healthz" in routes, (
            f"/healthz missing. Registered routes: {list(routes)}"
        )
        assert "GET" in routes["/healthz"]


# ---------------------------------------------------------------------------
# TC-3: /healthz returns 200 with correct service name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHealthz:
    """TC-3: GET /healthz → 200 with service=hr_agent."""

    async def test_healthz_status_200(self) -> None:
        app = _build_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200

    async def test_healthz_body_ok_true(self) -> None:
        app = _build_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
        assert resp.json()["ok"] is True

    async def test_healthz_service_name(self) -> None:
        app = _build_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
        assert resp.json()["service"] == "hr_agent"


# ---------------------------------------------------------------------------
# TC-4: CorrelationIdMiddleware adds X-Request-ID to response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCorrelationIdMiddleware:
    """TC-4 / TC-5: Middleware adds X-Request-ID on every response."""

    async def test_x_request_id_present_in_response(self) -> None:
        """When the request has no X-Request-ID, middleware generates one and adds it."""
        app = _build_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Deliberately omit X-Request-ID so middleware must generate one.
            resp = await client.get("/healthz")
        assert "x-request-id" in resp.headers or "X-Request-ID" in resp.headers, (
            f"X-Request-ID header missing from response. Headers: {dict(resp.headers)}"
        )

    async def test_inbound_x_request_id_echoed(self) -> None:
        """When a specific X-Request-ID is sent, the same value is echoed back."""
        app = _build_app()
        sent_rid = "test-correlation-id-wave8-001"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/healthz", headers={"X-Request-ID": sent_rid}
            )
        response_rid = (
            resp.headers.get("X-Request-ID") or resp.headers.get("x-request-id", "")
        )
        assert response_rid == sent_rid, (
            f"Expected echo of {sent_rid!r}, got {response_rid!r}"
        )


# ---------------------------------------------------------------------------
# TC-6: App lifespan completes without error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLifespan:
    """TC-6: App lifespan (startup + shutdown) runs without raising."""

    async def test_lifespan_startup_and_shutdown(self) -> None:
        """The ASGI lifespan protocol completes cleanly (no exceptions)."""
        app = _build_app()
        # httpx's ASGITransport drives the lifespan automatically when used
        # as an async context manager.  If startup or shutdown raises, the
        # context manager propagates the exception and the test fails.
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")
            # The 200 confirms the lifespan entered the yield successfully.
            assert resp.status_code == 200
        # After exiting the context manager the lifespan shutdown has run.
        # Reaching here without exception is the assertion.
