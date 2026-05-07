"""Tests for orchestrator/main.py — Wave 8, Sprint 1.

Covers:
1.  create_app() returns a FastAPI instance with all expected routes mounted.
2.  GET /healthz returns 200 with {"ok": true, "service": "orchestrator"}.
3.  CorrelationIdMiddleware adds X-Request-ID to responses.
4.  CORS preflight from a configured origin succeeds.
5.  CORS credentials header is present.
6.  Logging is configured at INFO level (root logger level check).
7.  App startup and shutdown complete without error (lifespan test).
8.  Explicit config bypasses os.environ.
9.  Two independent create_app() calls do not interfere (no shared global state).
10. Supplied X-Request-ID is echoed unchanged in the response.

Design notes
------------
- ``jwt_validator`` imports PyJWT (``import jwt``) which is not installed in
  the test environment.  We stub it as a bare ModuleType and provide the
  minimal symbols (``JWKSCache``, ``ValidatorConfig``, ``validate``) needed by
  the modules that import from it.
- ``WSO2ISClient`` and ``ActorTokenProvider`` are also stubbed so the
  ``PatternCExchanger`` constructor and main.py lifespan can instantiate them
  without network access.
- All stubs follow the existing codebase pattern: bare ``types.ModuleType``
  with dataclass or no-op class attributes added only where needed.
- ``TestClient`` is used synchronously; its context manager runs both startup
  and shutdown of the lifespan, fully exercising ``create_app()``.
"""

from __future__ import annotations

# ── Bootstrap ─────────────────────────────────────────────────────────────────
# Load all transitive dependencies via importlib to bypass broken __init__.py
# files and to inject stubs for modules with unavailable system packages.

import importlib.util
import logging
import pathlib
import sys
import types
from dataclasses import dataclass as _dc
from unittest.mock import AsyncMock

_ROOT = pathlib.Path(__file__).parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str) -> None:
    """Create a stub package namespace in sys.modules if not already present."""
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    parts = dotted_name.replace(".", "/")
    stub.__path__ = [str(_ROOT / parts)]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules under dotted_name."""
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]
    file_path = _ROOT / rel_path
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {file_path}"
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted_name.rsplit(".", 1)[0] if "." in dotted_name else ""
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── Intermediate package stubs ────────────────────────────────────────────────

for _pkg in (
    "common",
    "common.auth",
    "common.a2a",
    "common.logging",
    "orchestrator",
    "orchestrator.auth",
    "orchestrator.chat",
    "orchestrator.events",
    "orchestrator.agent_registry",
):
    _ensure_pkg(_pkg)

# ── Stubs for packages with unavailable system dependencies ──────────────────
# jwt_validator requires PyJWT (import jwt) which is not installed.
# Provide minimal stand-ins so importing code does not raise ImportError.

# ── Additive stub helpers ─────────────────────────────────────────────────────
# Each stub is registered in sys.modules ONLY if absent.  If already present
# (because another test file in the same session loaded a real or partial
# module), we backfill only the symbols that are missing.  This preserves the
# pre-existing codebase pattern of "if not in sys.modules → create stub".
# It also avoids the sys.modules race described in conftest.py by never
# overwriting a module that a prior test already installed.


def _ensure_module(dotted_name: str, package: str) -> types.ModuleType:
    """Return existing entry or create and register a bare stub."""
    if dotted_name not in sys.modules:
        mod = types.ModuleType(dotted_name)
        mod.__package__ = package
        sys.modules[dotted_name] = mod
    return sys.modules[dotted_name]


def _backfill(mod: types.ModuleType, **attrs: object) -> None:
    """Add *attrs* to *mod* if they are not already present."""
    for name, value in attrs.items():
        if not hasattr(mod, name):
            setattr(mod, name, value)


# ── jwt_validator stub ────────────────────────────────────────────────────────
# jwt_validator imports PyJWT (``import jwt``) which is not installed in the
# test environment.  We inject the three symbols that orchestrator/main.py and
# orchestrator/auth/pattern_c.py import from it.

@_dc(frozen=True)
class _ValidatorConfig:
    expected_iss: str = ""
    jwks_url: str = ""
    expected_aud: str | None = None
    required_scopes: frozenset = frozenset()
    leeway_seconds: int = 30
    insecure_tls: bool = False


class _JWKSCache:
    def __init__(self, *, jwks_url: str = "", insecure_tls: bool = False, **kw):
        self.jwks_url = jwks_url
        self.insecure_tls = insecure_tls


async def _validate(token, config, *, jwks_cache=None):
    raise NotImplementedError("jwt_validator stub — not used in unit tests")


_backfill(
    _ensure_module("common.auth.jwt_validator", "common.auth"),
    ValidatorConfig=_ValidatorConfig,
    JWKSCache=_JWKSCache,
    validate=_validate,
)

# ── WSO2ISClient stub ─────────────────────────────────────────────────────────

@_dc(frozen=True)
class _WSO2ISClientConfig:
    base_url: str = "https://is.example.com"
    insecure_tls: bool = False


class _WSO2ISClient:
    def __init__(self, config=None, *, http=None):
        pass

    async def aclose(self):
        pass


_backfill(
    _ensure_module("common.auth.wso2_is_client", "common.auth"),
    WSO2ISClientConfig=_WSO2ISClientConfig,
    WSO2ISClient=_WSO2ISClient,
)

# ── ActorTokenProvider stub ───────────────────────────────────────────────────

@_dc(frozen=True)
class _AgentCredentials:
    agent_id: str = ""
    agent_secret: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    redirect_uri: str = "http://localhost:9999/agent-callback"


class _ActorTokenProvider:
    def __init__(self, *, credentials=None, is_client=None, **kw):
        pass

    async def ensure_valid_token(self):
        raise NotImplementedError("ActorTokenProvider stub")


_backfill(
    _ensure_module("common.auth.actor_token_provider", "common.auth"),
    AgentCredentials=_AgentCredentials,
    ActorTokenProvider=_ActorTokenProvider,
)

# ── Load real modules bottom-up ───────────────────────────────────────────────

_load_module("common.auth.models", "common/auth/models.py")
_load_module("common.auth.errors", "common/auth/errors.py")
_load_module("common.a2a.agent_card", "common/a2a/agent_card.py")
_load_module("common.a2a.models", "common/a2a/models.py")
_load_module("common.a2a.jsonrpc", "common/a2a/jsonrpc.py")
_load_module("common.a2a.client", "common/a2a/client.py")
_load_module("common.logging.correlation", "common/logging/correlation.py")
_load_module("common.logging.redaction", "common/logging/redaction.py")
_load_module("orchestrator.config", "orchestrator/config.py")
_load_module("orchestrator.auth.session_store", "orchestrator/auth/session_store.py")
_load_module("orchestrator.auth.pattern_c", "orchestrator/auth/pattern_c.py")
_load_module("orchestrator.auth.routes", "orchestrator/auth/routes.py")
_load_module("orchestrator.chat.keyword_fallback", "orchestrator/chat/keyword_fallback.py")
_load_module("orchestrator.events.sse", "orchestrator/events/sse.py")
_load_module("orchestrator.events.sse_router", "orchestrator/events/sse_router.py")
_load_module("orchestrator.agent_registry.cards", "orchestrator/agent_registry/cards.py")
_load_module("orchestrator.agent_registry.discovery", "orchestrator/agent_registry/discovery.py")
_load_module("orchestrator.chat.routes", "orchestrator/chat/routes.py")
_load_module("orchestrator.main", "orchestrator/main.py")

# ── Real imports (sys.modules is now populated) ───────────────────────────────

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.config import OrchestratorConfig
from orchestrator.main import create_app

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _base_env() -> dict[str, str]:
    """Return a complete env dict satisfying all required OrchestratorConfig vars."""
    return {
        "WSO2_IS_BASE_URL": "https://is.example.com:9443",
        "IDP_INSECURE_TLS": "false",
        "ORCHESTRATOR_APP_CLIENT_ID": "spa-client-id",
        "ORCHESTRATOR_MCP_CLIENT_ID": "mcp-client-id",
        "ORCHESTRATOR_MCP_CLIENT_SECRET": "mcp-client-secret",
        "ORCHESTRATOR_AGENT_ID": "orch-agent-uuid",
        "ORCHESTRATOR_AGENT_SECRET": "orch-agent-secret",
        "ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID": "orch-oauth-client-id",
        "ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET": "orch-oauth-client-secret",
        "HR_AGENT_URL": "http://hr_agent:8001",
        "IT_AGENT_URL": "http://it_agent:8002",
        "HR_AGENT_OAUTH_CLIENT_ID": "hr-oauth-client-id",
        "IT_AGENT_OAUTH_CLIENT_ID": "it-oauth-client-id",
        "ALLOWED_ORIGINS": "http://localhost:3001,http://127.0.0.1:3001",
    }


def _make_config() -> OrchestratorConfig:
    """Return an OrchestratorConfig built from a mock env dict."""
    return OrchestratorConfig.from_env(_base_env())


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Build a fully-wired FastAPI app once per test module.

    Using module scope avoids re-running the module-level bootstrap on every
    test.  Each test that needs the lifespan wraps ``TestClient`` in its own
    ``with`` block.
    """
    return create_app(_make_config())


# ── Test 1: create_app returns a FastAPI instance with all routes ──────────────


class TestRoutesMounted:
    """Verify every public endpoint is registered in the route table."""

    _EXPECTED_ROUTES: list[tuple[str, str]] = [
        ("GET",  "/auth/login"),
        ("GET",  "/auth/callback"),
        ("POST", "/auth/exchange"),
        ("POST", "/auth/logout"),
        ("POST", "/api/chat"),
        ("POST", "/api/ciba/cancel"),
        ("GET",  "/events/{session_id}"),
        ("GET",  "/healthz"),
    ]

    def test_create_app_returns_fastapi_instance(self, app: FastAPI) -> None:
        assert isinstance(app, FastAPI)

    def test_all_expected_routes_present(self, app: FastAPI) -> None:
        """All eight public routes must appear in the mounted route set."""
        mounted: set[tuple[str, str]] = set()
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "")
            for method in methods:
                mounted.add((method.upper(), path))

        for method, path in self._EXPECTED_ROUTES:
            assert (method, path) in mounted, (
                f"Route {method} {path} not found in app routes. "
                f"Mounted routes: {sorted(mounted)}"
            )

    def test_app_title_is_orchestrator(self, app: FastAPI) -> None:
        assert app.title == "Orchestrator"


# ── Test 2: /healthz returns 200 with the correct body ────────────────────────


class TestHealthEndpoint:

    def test_healthz_returns_200(self, app: FastAPI) -> None:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_body_shape(self, app: FastAPI) -> None:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/healthz")
        assert resp.json() == {"ok": True, "service": "orchestrator"}

    def test_healthz_content_type_json(self, app: FastAPI) -> None:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/healthz")
        assert "application/json" in resp.headers.get("content-type", "")


# ── Test 3: CorrelationIdMiddleware adds X-Request-ID to responses ────────────


class TestCorrelationIdMiddleware:

    def test_response_carries_x_request_id_header(self, app: FastAPI) -> None:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/healthz")
        assert "x-request-id" in resp.headers, (
            "Expected X-Request-ID header in response, got none"
        )

    def test_x_request_id_is_non_empty(self, app: FastAPI) -> None:
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/healthz")
        assert resp.headers["x-request-id"].strip(), "X-Request-ID must not be empty"

    def test_supplied_x_request_id_echoed_unchanged(self, app: FastAPI) -> None:
        """When the caller sends X-Request-ID, it must be echoed back verbatim."""
        custom_id = "test-correlation-id-12345"
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/healthz", headers={"X-Request-ID": custom_id})
        assert resp.headers.get("x-request-id") == custom_id


# ── Test 4 & 5: CORS preflight and credentials header ─────────────────────────


class TestCors:

    _ALLOWED_ORIGIN = "http://localhost:3001"

    def test_cors_preflight_from_allowed_origin_returns_200(
        self, app: FastAPI
    ) -> None:
        """OPTIONS preflight from a configured origin must return 200."""
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.options(
                "/api/chat",
                headers={
                    "Origin": self._ALLOWED_ORIGIN,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )
        assert resp.status_code == 200, (
            f"Expected 200 for CORS preflight from {self._ALLOWED_ORIGIN!r}, "
            f"got {resp.status_code}"
        )

    def test_cors_allow_origin_header_set(self, app: FastAPI) -> None:
        """Access-Control-Allow-Origin must include the allowed origin."""
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.options(
                "/healthz",
                headers={
                    "Origin": self._ALLOWED_ORIGIN,
                    "Access-Control-Request-Method": "GET",
                },
            )
        acao = resp.headers.get("access-control-allow-origin", "")
        assert self._ALLOWED_ORIGIN in acao or acao == "*", (
            f"Expected {self._ALLOWED_ORIGIN!r} in Access-Control-Allow-Origin, "
            f"got {acao!r}"
        )

    def test_cors_credentials_allowed(self, app: FastAPI) -> None:
        """Access-Control-Allow-Credentials must be 'true' for cookie-based auth."""
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.options(
                "/healthz",
                headers={
                    "Origin": self._ALLOWED_ORIGIN,
                    "Access-Control-Request-Method": "GET",
                },
            )
        acac = resp.headers.get("access-control-allow-credentials", "")
        assert acac.lower() == "true", (
            f"Expected access-control-allow-credentials=true, got {acac!r}"
        )


# ── Test 6: Logging configured at INFO level ──────────────────────────────────


class TestLoggingConfiguration:

    def test_root_logger_level_at_or_below_info(self, app: FastAPI) -> None:
        """Root logger level must be INFO (20) or more verbose (DEBUG=10)."""
        with TestClient(app):
            assert logging.getLogger().level <= logging.INFO

    def test_root_logger_has_at_least_one_handler(self, app: FastAPI) -> None:
        with TestClient(app):
            assert len(logging.getLogger().handlers) >= 1


# ── Test 7: Lifespan starts and stops without error ───────────────────────────


class TestLifespanStartup:

    def test_lifespan_completes_cleanly(self) -> None:
        """TestClient context manager triggers startup + shutdown.

        Any exception raised in the lifespan will propagate through TestClient,
        failing this test.
        """
        fresh_app = create_app(_make_config())
        with TestClient(fresh_app, raise_server_exceptions=True) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_explicit_config_bypasses_os_environ(self) -> None:
        """Passing a config object must not require any os.environ look-ups."""
        cfg = _make_config()
        fresh_app = create_app(config=cfg)
        assert isinstance(fresh_app, FastAPI)
        with TestClient(fresh_app) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_two_independent_apps_do_not_interfere(self) -> None:
        """Two concurrent create_app() instances must not share mutable state."""
        app1 = create_app(_make_config())
        app2 = create_app(_make_config())
        # Start both lifespans simultaneously to detect global-state races.
        with TestClient(app1), TestClient(app2):
            pass  # Both lifespans active; no assertion needed beyond no exception.


# ── Test 8: Auth and chat routes are reachable (non-404) ─────────────────────


class TestAuthAndChatRoutesPresence:
    """Lightweight smoke tests confirming routes are mounted and returning
    expected status codes rather than 404 Not Found."""

    def test_login_route_redirects_not_404(self, app: FastAPI) -> None:
        with TestClient(app, raise_server_exceptions=False, follow_redirects=False) as c:
            resp = c.get("/auth/login")
        assert resp.status_code != 404

    def test_logout_route_is_idempotent_200(self, app: FastAPI) -> None:
        """POST /auth/logout without a cookie must return 200 (idempotent per spec)."""
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post("/auth/logout")
        assert resp.status_code == 200

    def test_exchange_without_body_returns_422(self, app: FastAPI) -> None:
        """POST /auth/exchange without a body → 422 Unprocessable Entity."""
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post("/auth/exchange")
        assert resp.status_code == 422

    def test_chat_without_session_returns_401(self, app: FastAPI) -> None:
        """POST /api/chat without an orch_sid cookie → 401 Unauthorized."""
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post("/api/chat", json={"message": "hello"})
        assert resp.status_code == 401
