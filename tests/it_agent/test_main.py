"""Tests for it_agent/main.py — Wave 8, Sprint 1.

Coverage (6 tests)
------------------
 1. ``create_app(mock_config)`` returns a ``FastAPI`` instance.
 2. All expected routes are mounted (A2A three-pack + /healthz).
 3. ``GET /healthz`` returns ``{"ok": True, "service": "it_agent"}``.
 4. ``CorrelationIdMiddleware`` echoes / generates ``X-Request-ID`` on responses.
 5. App lifespan (startup + shutdown) completes without error.
 6. ``X-Request-ID`` generated when header is absent (covers the WARN branch).

Design notes
------------
- No live IS / it_server / CIBA traffic — all heavy clients are replaced with
  ``AsyncMock`` / ``MagicMock`` objects before ``create_app`` is called.
- ``common.auth.jwt_validator`` is stubbed (PyJWT may not be installed in CI).
- Module bootstrap mirrors the ``_load`` / ``_ensure_pkg`` pattern used by all
  other Sprint 1 test files in this repo.
- The ``httpx.AsyncClient`` (ASGI transport) communicates with the app in-process.
- Lifespan is exercised via ``httpx``'s ``ASGITransport`` which triggers FastAPI's
  lifespan context on the first request; we also drive it explicitly with
  ``asgi_lifespan.LifespanManager`` where available, and fall back to a direct
  ``async with app.router.lifespan_context(app)`` call (FastAPI 0.100+).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Module bootstrap
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted: str) -> None:
    """Register a bare package namespace in sys.modules if not already present."""
    if dotted not in sys.modules:
        stub = types.ModuleType(dotted)
        stub.__package__ = dotted
        stub.__path__ = [str(_ROOT / dotted.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[dotted] = stub


def _load(dotted: str, rel: str) -> types.ModuleType:
    """Load a .py file under *dotted* name; skip if already present."""
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


# ── Package namespaces ────────────────────────────────────────────────────────

for _pkg in (
    "common",
    "common.auth",
    "common.a2a",
    "common.logging",
    "it_agent",
    "it_agent.a2a",
    "it_agent.ciba",
    "it_agent.mcp",
):
    _ensure_pkg(_pkg)

# ── Real common modules ───────────────────────────────────────────────────────

_load("common.auth.models", "common/auth/models.py")
_load("common.auth.errors", "common/auth/errors.py")
_load("common.auth.peer_trust", "common/auth/peer_trust.py")
_load("common.a2a.jsonrpc", "common/a2a/jsonrpc.py")
_load("common.a2a.models", "common/a2a/models.py")
_load("common.logging.correlation", "common/logging/correlation.py")

# ── jwt_validator stub (PyJWT may not be installed in CI) ─────────────────────

if "common.auth.jwt_validator" not in sys.modules:
    from dataclasses import dataclass as _dc, field as _f

    _jv_mod = types.ModuleType("common.auth.jwt_validator")
    _jv_mod.__package__ = "common.auth"

    @_dc(frozen=True, slots=True)
    class _ValidatorConfig:  # type: ignore[no-redef]
        expected_iss: str
        jwks_url: str
        expected_aud: str | None = None
        required_scopes: frozenset = _f(default_factory=frozenset)
        leeway_seconds: int = 30
        insecure_tls: bool = False

    @_dc
    class _JWKSCache:  # type: ignore[no-redef]
        jwks_url: str
        ttl_seconds: int = 3600
        insecure_tls: bool = False

    async def _validate(token, config, *, jwks_cache=None):  # type: ignore[return]
        raise RuntimeError("jwt_validator stub: patch validate before use")

    _jv_mod.ValidatorConfig = _ValidatorConfig  # type: ignore[attr-defined]
    _jv_mod.JWKSCache = _JWKSCache  # type: ignore[attr-defined]
    _jv_mod.validate = _validate  # type: ignore[attr-defined]
    sys.modules["common.auth.jwt_validator"] = _jv_mod

_load("common.a2a.server", "common/a2a/server.py")

# ── Load real common modules that it_agent/main.py imports at module level ────
# Must be registered before any stub for these names, and before loading
# it_agent.config or it_agent.main.
#
# Load order: models → errors → wso2_is_client → actor_token_provider →
#   ciba_client → binding_messages → mcp/client

_load("common.auth.wso2_is_client", "common/auth/wso2_is_client.py")
_load("common.auth.actor_token_provider", "common/auth/actor_token_provider.py")
_load("common.auth.ciba_client", "common/auth/ciba_client.py")
_load("common.auth.binding_messages", "common/auth/binding_messages.py")

# ── Stub it_agent.mcp.client (only used at runtime, not at import time in main)

if "it_agent.mcp.client" not in sys.modules:
    _mcp_stub = types.ModuleType("it_agent.mcp.client")
    _mcp_stub.__package__ = "it_agent.mcp"

    from dataclasses import dataclass as _dc2, field as _f2

    @_dc2(frozen=True, slots=True)
    class _ITMcpClientConfig:  # type: ignore[no-redef]
        base_url: str
        timeout_seconds: float = 30.0

    class _ITMcpClient:  # type: ignore[no-redef]
        def __init__(self, config: object) -> None:  # type: ignore[override]
            pass

        async def aclose(self) -> None:
            pass

    _mcp_stub.ITMcpClientConfig = _ITMcpClientConfig  # type: ignore[attr-defined]
    _mcp_stub.ITMcpClient = _ITMcpClient  # type: ignore[attr-defined]
    sys.modules["it_agent.mcp.client"] = _mcp_stub

# it_agent.config needs ITAgentConfig exported; load the real file.
_load("it_agent.config", "it_agent/config.py")

# it_agent.ciba.orchestrator needs ITDispatcher + ITDispatcherDeps.
if "it_agent.ciba.orchestrator" not in sys.modules:
    _oc = types.ModuleType("it_agent.ciba.orchestrator")
    _oc.__package__ = "it_agent.ciba"

    class _ITDispatcher:  # type: ignore[no-redef]
        """Sentinel replaced by _FakeITDispatcher in tests."""

    _oc.ITDispatcher = _ITDispatcher  # type: ignore[attr-defined]
    _oc.ITDispatcherDeps = object  # type: ignore[attr-defined]
    sys.modules["it_agent.ciba.orchestrator"] = _oc

_load("it_agent.a2a.handler", "it_agent/a2a/handler.py")

# Now load the module under test.
_load("it_agent.main", "it_agent/main.py")

# ---------------------------------------------------------------------------
# Imports after bootstrap
# ---------------------------------------------------------------------------

from it_agent.a2a.handler import ITA2AHandlerDeps  # noqa: E402
from it_agent.config import ITAgentConfig  # noqa: E402
from it_agent.main import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


def _base_env() -> dict[str, str]:
    """Minimal valid environment dict for ITAgentConfig.from_env()."""
    return {
        "WSO2_IS_BASE_URL": "https://is.example.com:9443",
        "IT_AGENT_ID": "it_agent-test-uuid",
        "IT_AGENT_SECRET": "it_agent-secret",
        "IT_AGENT_OAUTH_CLIENT_ID": "it-oauth-client-id",
        "IT_AGENT_OAUTH_CLIENT_SECRET": "it-oauth-client-secret",
        "IT_MCP_SERVER_URL": "http://it_server:8004",
        "IT_EXPECTED_INBOUND_AUD": "orch-mcp-client-id",
        "IT_AGENT_HOST": "127.0.0.1",
        "IT_AGENT_PORT": "8002",
    }


@dataclass
class _FakeITDispatcher:
    """Minimal dispatcher stub — never invoked by these smoke tests."""


def _make_mock_config() -> ITAgentConfig:
    """Return a real ITAgentConfig built from a minimal env dict."""
    return ITAgentConfig.from_env(_base_env())


def _build_stub_a2a_router():  # type: ignore[return]
    """Return a minimal APIRouter with the three required A2A POST routes."""
    from fastapi import APIRouter

    stub_router = APIRouter()

    @stub_router.post("/a2a/message/send")
    async def _send():  # type: ignore[return]
        pass

    @stub_router.post("/a2a/await")
    async def _await():  # type: ignore[return]
        pass

    @stub_router.post("/a2a/cancel")
    async def _cancel():  # type: ignore[return]
        pass

    return stub_router


def _make_app_with_mocked_deps() -> FastAPI:
    """Build the app with all heavy clients replaced by mocks.

    Patches applied on the ``it_agent.main`` module:
    - ``WSO2ISClient``        → ``MagicMock`` (no httpx connections)
    - ``ActorTokenProvider``  → ``MagicMock``
    - ``CIBAClient``          → ``MagicMock`` with async ``aclose``
    - ``ITMcpClient``         → ``MagicMock`` with async ``aclose``
    - ``ITDispatcherDeps``    → ``MagicMock`` (accepts any kwargs)
    - ``ITDispatcher``        → returns a ``_FakeITDispatcher`` instance
    - ``build_it_a2a_router`` → returns a stub ``APIRouter``
    """
    from it_agent import main as main_mod

    cfg = _make_mock_config()

    mock_is_client = MagicMock()
    mock_is_client.aclose = AsyncMock()

    mock_atp = MagicMock()

    mock_ciba_client = MagicMock()
    mock_ciba_client.aclose = AsyncMock()

    mock_mcp_client = MagicMock()
    mock_mcp_client.aclose = AsyncMock()

    mock_dispatcher = _FakeITDispatcher()

    with (
        patch.object(main_mod, "WSO2ISClient", return_value=mock_is_client),
        patch.object(main_mod, "ActorTokenProvider", return_value=mock_atp),
        patch.object(main_mod, "CIBAClient", return_value=mock_ciba_client),
        patch.object(main_mod, "ITMcpClient", return_value=mock_mcp_client),
        patch.object(main_mod, "ITDispatcherDeps", MagicMock()),
        patch.object(main_mod, "ITDispatcher", return_value=mock_dispatcher),
        patch.object(main_mod, "build_it_a2a_router", return_value=_build_stub_a2a_router()),
    ):
        app = create_app(cfg)

    return app


# ---------------------------------------------------------------------------
# Test 1 — create_app returns a FastAPI instance
# ---------------------------------------------------------------------------


class TestCreateAppReturnsInstance:

    def test_returns_fastapi(self) -> None:
        """``create_app(mock_config)`` must return a ``FastAPI`` instance."""
        app = _make_app_with_mocked_deps()
        assert isinstance(app, FastAPI)

    def test_title_is_it_agent(self) -> None:
        """App title must identify the service."""
        app = _make_app_with_mocked_deps()
        assert app.title == "it_agent"


# ---------------------------------------------------------------------------
# Test 2 — All expected routes are mounted
# ---------------------------------------------------------------------------


class TestExpectedRoutesMounted:

    def _route_paths(self, app: FastAPI) -> set[str]:
        return {r.path for r in app.routes}  # type: ignore[attr-defined]

    def test_healthz_route_present(self) -> None:
        """``GET /healthz`` must be mounted."""
        app = _make_app_with_mocked_deps()
        assert "/healthz" in self._route_paths(app)

    def test_a2a_send_route_present(self) -> None:
        """``POST /a2a/message/send`` must be mounted."""
        app = _make_app_with_mocked_deps()
        assert "/a2a/message/send" in self._route_paths(app)

    def test_a2a_await_route_present(self) -> None:
        """``POST /a2a/await`` must be mounted."""
        app = _make_app_with_mocked_deps()
        assert "/a2a/await" in self._route_paths(app)

    def test_a2a_cancel_route_present(self) -> None:
        """``POST /a2a/cancel`` must be mounted."""
        app = _make_app_with_mocked_deps()
        assert "/a2a/cancel" in self._route_paths(app)


# ---------------------------------------------------------------------------
# Test 3 — /healthz returns the correct service name
# ---------------------------------------------------------------------------


class TestHealthz:

    @pytest.mark.asyncio
    async def test_healthz_ok_true(self) -> None:
        """``GET /healthz`` must return ``{"ok": True, "service": "it_agent"}``."""
        app = _make_app_with_mocked_deps()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["service"] == "it_agent"

    @pytest.mark.asyncio
    async def test_healthz_service_name_is_it_agent_not_hr(self) -> None:
        """The service name must be 'it_agent', not 'hr_agent'."""
        app = _make_app_with_mocked_deps()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")

        assert resp.json()["service"] == "it_agent"


# ---------------------------------------------------------------------------
# Test 4 — CorrelationIdMiddleware adds X-Request-ID to responses
# ---------------------------------------------------------------------------


class TestCorrelationIdMiddleware:

    @pytest.mark.asyncio
    async def test_request_id_echoed_when_provided(self) -> None:
        """When ``X-Request-ID`` is in the request, it must appear in the response."""
        app = _make_app_with_mocked_deps()
        sent_id = "test-correlation-id-0001"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/healthz", headers={"X-Request-ID": sent_id}
            )

        assert resp.headers.get("x-request-id") == sent_id

    @pytest.mark.asyncio
    async def test_request_id_generated_when_absent(self) -> None:
        """When ``X-Request-ID`` is absent, the middleware must generate one."""
        app = _make_app_with_mocked_deps()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/healthz")

        # A UUID4 was auto-generated; must be a non-empty string.
        generated_id = resp.headers.get("x-request-id")
        assert generated_id is not None
        assert len(generated_id) > 0

    @pytest.mark.asyncio
    async def test_distinct_ids_per_request(self) -> None:
        """Two requests without X-Request-ID must receive distinct generated IDs."""
        app = _make_app_with_mocked_deps()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp1 = await client.get("/healthz")
            resp2 = await client.get("/healthz")

        id1 = resp1.headers.get("x-request-id")
        id2 = resp2.headers.get("x-request-id")
        assert id1 != id2


# ---------------------------------------------------------------------------
# Test 5 — App lifespan completes without error
# ---------------------------------------------------------------------------


class TestLifespan:

    def test_lifespan_startup_and_shutdown(self) -> None:
        """Lifespan context (startup + shutdown) must complete without raising.

        Uses Starlette's synchronous ``TestClient`` which correctly fires both
        the startup and shutdown lifespan events.
        """
        from starlette.testclient import TestClient

        app = _make_app_with_mocked_deps()
        # TestClient.__enter__ fires startup; __exit__ fires shutdown.
        with TestClient(app) as client:
            resp = client.get("/healthz")

        assert resp.status_code == 200
        assert resp.json()["service"] == "it_agent"

    def test_lifespan_aclose_called_on_shutdown(self) -> None:
        """``aclose()`` must be called on all three clients at lifespan shutdown."""
        from starlette.testclient import TestClient
        from it_agent import main as main_mod

        cfg = _make_mock_config()

        mock_is_client = MagicMock()
        mock_is_client.aclose = AsyncMock()
        mock_atp = MagicMock()
        mock_ciba = MagicMock()
        mock_ciba.aclose = AsyncMock()
        mock_mcp = MagicMock()
        mock_mcp.aclose = AsyncMock()

        with (
            patch.object(main_mod, "WSO2ISClient", return_value=mock_is_client),
            patch.object(main_mod, "ActorTokenProvider", return_value=mock_atp),
            patch.object(main_mod, "CIBAClient", return_value=mock_ciba),
            patch.object(main_mod, "ITMcpClient", return_value=mock_mcp),
            patch.object(main_mod, "ITDispatcherDeps", MagicMock()),
            patch.object(main_mod, "ITDispatcher", return_value=_FakeITDispatcher()),
            patch.object(main_mod, "build_it_a2a_router", return_value=_build_stub_a2a_router()),
        ):
            app = create_app(cfg)

        # TestClient context manager drives the full lifespan (startup + shutdown).
        with TestClient(app) as client:
            client.get("/healthz")

        # Shutdown branch of the lifespan must have called aclose on all three clients.
        mock_mcp.aclose.assert_called_once()
        mock_ciba.aclose.assert_called_once()
        mock_is_client.aclose.assert_called_once()
