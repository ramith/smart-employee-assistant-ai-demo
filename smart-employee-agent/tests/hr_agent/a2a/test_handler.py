"""Tests for hr_agent/a2a/handler.py — Wave 7, Sprint 1.

Coverage (7 tests)
------------------
 1. ``build_hr_a2a_router`` returns a FastAPI ``APIRouter``.
 2. The router exposes exactly the three required POST routes.
 3. Wired ``trusted_orchestrator_subs`` equals ``frozenset(deps.config.trusted_orchestrator_subs)``.
 4. Wired ``validator_config.expected_aud`` equals ``deps.config.expected_inbound_aud``.
 5. Wired ``validator_config.expected_iss`` equals ``deps.config.is_issuer``.
 6. Valid token + trusted ``act.sub`` → dispatcher is called; ``/a2a/message/send``
    returns ``ResultPayload`` (end-to-end wiring smoke test).
 7. Token with ``act.sub`` not in allowlist → JSON-RPC ``-32001``
    (``ERR_PEER_NOT_TRUSTED``).

Design notes
------------
- JWT validation is stubbed by monkey-patching ``common.a2a.server.validate``
  (the server module captures it by name; patching there avoids live JWKS).
- Dispatcher is a minimal ``StubDispatcher`` that returns a canned
  ``ResultPayload`` so we can confirm wiring without the full CIBA path.
- Module bootstrap mirrors the ``test_server.py`` ``_load`` / ``_ensure_pkg``
  pattern because ``__init__.py`` chains are not complete in this test env.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Module bootstrap — replicate the test_server.py _load / _ensure_pkg pattern
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted: str) -> None:
    if dotted not in sys.modules:
        stub = types.ModuleType(dotted)
        stub.__package__ = dotted
        stub.__path__ = [str(_ROOT / dotted.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[dotted] = stub


def _load(dotted: str, rel: str) -> types.ModuleType:
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


# Ensure all required package namespaces exist before loading leaf modules.
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

# Load dependency chain in import order.
_load("common.auth.models", "common/auth/models.py")
_load("common.auth.errors", "common/auth/errors.py")
_load("common.auth.peer_trust", "common/auth/peer_trust.py")
_load("common.a2a.jsonrpc", "common/a2a/jsonrpc.py")
_load("common.a2a.models", "common/a2a/models.py")

# ``common.auth.jwt_validator`` imports PyJWT which is not installed in CI.
# Build a minimal stub that exposes only the names used by common.a2a.server
# (ValidatorConfig, JWKSCache, validate) so the server module loads cleanly.
# The real validate() is replaced by _stub_validate at test time via patch().
if "common.auth.jwt_validator" not in sys.modules:
    from dataclasses import dataclass as _dc, field as _field, fields as _fields

    _jv_mod = types.ModuleType("common.auth.jwt_validator")
    _jv_mod.__package__ = "common.auth"

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

    async def _validate(token, config, *, jwks_cache=None):  # type: ignore[return]
        raise RuntimeError("jwt_validator stub: patch validate before use")

    _jv_mod.ValidatorConfig = _ValidatorConfig  # type: ignore[attr-defined]
    _jv_mod.JWKSCache = _JWKSCache  # type: ignore[attr-defined]
    _jv_mod.validate = _validate  # type: ignore[attr-defined]
    sys.modules["common.auth.jwt_validator"] = _jv_mod

_load("common.a2a.server", "common/a2a/server.py")

# Stub heavy HR-agent dependencies so the handler module can be loaded
# without pulling in httpx-based clients, IS connectivity, etc.
for _stub_name in (
    "common.auth.wso2_is_client",
    "common.auth.actor_token_provider",
    "common.auth.binding_messages",
    "common.auth.ciba_client",
    "hr_agent.mcp.client",
):
    if _stub_name not in sys.modules:
        _m = types.ModuleType(_stub_name)
        _m.__package__ = _stub_name.rsplit(".", 1)[0]
        sys.modules[_stub_name] = _m

# hr_agent.config needs HRAgentConfig exported.
if "hr_agent.config" not in sys.modules:
    _cfg_mod = types.ModuleType("hr_agent.config")
    _cfg_mod.__package__ = "hr_agent"

    class _HRAgentConfig:  # type: ignore[no-redef]
        """Sentinel — tests supply _FakeHRAgentConfig."""

    _cfg_mod.HRAgentConfig = _HRAgentConfig  # type: ignore[attr-defined]
    sys.modules["hr_agent.config"] = _cfg_mod

# hr_agent.ciba.orchestrator needs HRDispatcher exported so handler.py can
# import it at module load time.  We provide a sentinel class; tests use
# StubHRDispatcher for actual dispatch behaviour.
if "hr_agent.ciba.orchestrator" not in sys.modules:
    _orch_mod = types.ModuleType("hr_agent.ciba.orchestrator")
    _orch_mod.__package__ = "hr_agent.ciba"

    class _HRDispatcher:  # type: ignore[no-redef]
        """Sentinel — replaced by StubHRDispatcher in tests."""

    _orch_mod.HRDispatcher = _HRDispatcher  # type: ignore[attr-defined]
    _orch_mod.HRDispatcherDeps = object  # type: ignore[attr-defined]
    sys.modules["hr_agent.ciba.orchestrator"] = _orch_mod

_load("hr_agent.a2a.handler", "hr_agent/a2a/handler.py")

# ---------------------------------------------------------------------------
# Import concrete symbols after bootstrap.
# ---------------------------------------------------------------------------

from common.a2a.models import (  # noqa: E402
    A2AMessageResponse,
    ConsentRequiredPayload,
    ErrorPayload,
    ResultPayload,
)
from common.a2a.server import (  # noqa: E402
    A2APendingState,
    A2ARouterConfig,
    DispatchProtocol,
    build_a2a_router,
)
from common.auth.errors import JWTValidationError, PeerTrustError  # noqa: E402
from common.auth.models import JWTClaims  # noqa: E402
from hr_agent.a2a.handler import HRA2AHandlerDeps, build_hr_a2a_router  # noqa: E402

# ValidatorConfig and JWKSCache come from the stub (or real) jwt_validator module.
ValidatorConfig = sys.modules["common.auth.jwt_validator"].ValidatorConfig  # type: ignore[attr-defined]
JWKSCache = sys.modules["common.auth.jwt_validator"].JWKSCache  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_TRUSTED_ORCH_SUB = "orch-agent-uuid-hr-0001"
_USER_SUB = "user-sub-hr-abc"
_INBOUND_AUD = "hr_agent-oauth-client-id-001"
_ISSUER = "https://is.example.com/oauth2/token"
_JWKS_URL = "https://is.example.com/oauth2/jwks"
_TRUSTED_PEERS = frozenset({_TRUSTED_ORCH_SUB})

_GOOD_BEARER = "Bearer valid.hr.token"
_UNTRUSTED_BEARER = "Bearer untrusted.hr.token"

_GOOD_CLAIMS = JWTClaims(
    sub=_USER_SUB,
    iss=_ISSUER,
    aud=_INBOUND_AUD,
    exp=9999999999,
    iat=1700000000,
    jti="jti-hr-good-001",
    act={"sub": _TRUSTED_ORCH_SUB},
    scope="openid orchestrate",
    aut="APPLICATION_USER",
)

_UNTRUSTED_CLAIMS = JWTClaims(
    sub=_USER_SUB,
    iss=_ISSUER,
    aud=_INBOUND_AUD,
    exp=9999999999,
    iat=1700000000,
    jti="jti-hr-untrusted-001",
    act={"sub": "attacker-uuid-9999"},
    scope="openid orchestrate",
    aut="APPLICATION_USER",
)

_CANNED_RESULT = ResultPayload(
    data={"leave_days": 10},
    token_jti="jti-hr-result-001",
    token_exp=9999999999,
    token_iat=1700000000,
)


# ---------------------------------------------------------------------------
# Stub JWT validator
# ---------------------------------------------------------------------------


async def _stub_validate(
    token: str, config: ValidatorConfig, *, jwks_cache: JWKSCache | None = None
) -> JWTClaims:
    raw = token.split(" ", 1)[-1]
    if raw == "valid.hr.token":
        return _GOOD_CLAIMS
    if raw == "untrusted.hr.token":
        return _UNTRUSTED_CLAIMS
    raise JWTValidationError("Stub: unknown token", error_id="ERR-AUTH-006")


# ---------------------------------------------------------------------------
# Stub dispatcher
# ---------------------------------------------------------------------------


class StubHRDispatcher:
    """Minimal dispatcher that returns a canned ResultPayload for any tool call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(
        self,
        *,
        tool: str,
        args: dict,
        user_sub: str,
        orchestrator_act_sub: str,
        request_id: str,
        pending_register: Callable[[A2APendingState], None],
    ) -> A2AMessageResponse:
        self.calls.append(
            {
                "tool": tool,
                "args": args,
                "user_sub": user_sub,
                "orchestrator_act_sub": orchestrator_act_sub,
                "request_id": request_id,
            }
        )
        return _CANNED_RESULT


# ---------------------------------------------------------------------------
# Fake HRAgentConfig dataclass (avoids env-var loading in tests)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeHRAgentConfig:
    is_issuer: str = _ISSUER
    is_jwks_url: str = _JWKS_URL
    is_insecure_tls: bool = False
    expected_inbound_aud: str = _INBOUND_AUD
    trusted_orchestrator_subs: frozenset[str] = field(
        default_factory=lambda: frozenset({_TRUSTED_ORCH_SUB})
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(
    pending: dict[str, A2APendingState] | None = None,
) -> HRA2AHandlerDeps:
    dispatcher = StubHRDispatcher()
    return HRA2AHandlerDeps(
        config=_FakeHRAgentConfig(),  # type: ignore[arg-type]
        dispatcher=dispatcher,  # type: ignore[arg-type]
        pending=pending if pending is not None else {},
    )


def _build_app(deps: HRA2AHandlerDeps) -> FastAPI:
    app = FastAPI()
    app.include_router(build_hr_a2a_router(deps))
    return app


def _patch_validate():
    return patch("common.a2a.server.validate", side_effect=_stub_validate)


_SEND_HEADERS = {
    "Authorization": _GOOD_BEARER,
    "X-Request-ID": "hr-req-001",
    "Content-Type": "application/json",
}


def _send_body(tool: str = "any_tool", rpc_id: str = "rpc-1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "message/send",
        "params": {"tool": tool, "args": {}},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuildHrA2ARouter:
    """Static structure tests — no async / HTTP needed."""

    def test_returns_api_router(self) -> None:
        """TC-1: build_hr_a2a_router returns a FastAPI APIRouter instance."""
        deps = _make_deps()
        router = build_hr_a2a_router(deps)
        assert isinstance(router, APIRouter)

    def test_router_has_three_post_routes(self) -> None:
        """TC-2: The returned router has POST routes for the three A2A endpoints."""
        deps = _make_deps()
        router = build_hr_a2a_router(deps)

        post_paths = {
            route.path
            for route in router.routes
            if hasattr(route, "methods") and "POST" in route.methods
        }
        assert "/a2a/message/send" in post_paths, f"Missing /a2a/message/send in {post_paths}"
        assert "/a2a/await" in post_paths, f"Missing /a2a/await in {post_paths}"
        assert "/a2a/cancel" in post_paths, f"Missing /a2a/cancel in {post_paths}"

    def test_trusted_orchestrator_subs_matches_config(self) -> None:
        """TC-3: trusted_orchestrator_subs in the assembled router_config equals
        frozenset(deps.config.trusted_orchestrator_subs).
        """
        deps = _make_deps()
        # Capture the A2ARouterConfig by intercepting build_a2a_router.
        captured: list[A2ARouterConfig] = []

        def _capture(cfg: A2ARouterConfig, *, jwks_cache=None) -> APIRouter:
            captured.append(cfg)
            return build_a2a_router(cfg, jwks_cache=jwks_cache)

        with patch("hr_agent.a2a.handler.build_a2a_router", side_effect=_capture):
            build_hr_a2a_router(deps)

        assert len(captured) == 1
        assert captured[0].trusted_orchestrator_subs == frozenset(
            deps.config.trusted_orchestrator_subs
        )

    def test_expected_aud_matches_config(self) -> None:
        """TC-4: validator_config.expected_aud equals deps.config.expected_inbound_aud."""
        deps = _make_deps()
        captured: list[A2ARouterConfig] = []

        def _capture(cfg: A2ARouterConfig, *, jwks_cache=None) -> APIRouter:
            captured.append(cfg)
            return build_a2a_router(cfg, jwks_cache=jwks_cache)

        with patch("hr_agent.a2a.handler.build_a2a_router", side_effect=_capture):
            build_hr_a2a_router(deps)

        assert captured[0].validator_config.expected_aud == deps.config.expected_inbound_aud

    def test_expected_iss_matches_config(self) -> None:
        """TC-5: validator_config.expected_iss equals deps.config.is_issuer."""
        deps = _make_deps()
        captured: list[A2ARouterConfig] = []

        def _capture(cfg: A2ARouterConfig, *, jwks_cache=None) -> APIRouter:
            captured.append(cfg)
            return build_a2a_router(cfg, jwks_cache=jwks_cache)

        with patch("hr_agent.a2a.handler.build_a2a_router", side_effect=_capture):
            build_hr_a2a_router(deps)

        assert captured[0].validator_config.expected_iss == deps.config.is_issuer


@pytest.mark.asyncio
class TestHrA2ARouterEndToEnd:
    """End-to-end HTTP smoke tests through the fully wired router."""

    async def test_valid_token_dispatches_and_returns_result(self) -> None:
        """TC-6: Valid token + trusted act.sub → dispatcher called; ResultPayload returned."""
        deps = _make_deps()
        app = _build_app(deps)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/message/send",
                    json=_send_body("hr.read_balance"),
                    headers=_SEND_HEADERS,
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is None
        result = body["result"]
        assert result["type"] == "result"
        assert result["data"] == {"leave_days": 10}
        assert result["token_jti"] == "jti-hr-result-001"
        # Verify dispatcher was actually invoked with the correct tool.
        dispatcher: StubHRDispatcher = deps.dispatcher  # type: ignore[assignment]
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["tool"] == "hr.read_balance"

    async def test_untrusted_act_sub_returns_json_rpc_32001(self) -> None:
        """TC-7: Token with act.sub not in allowlist → JSON-RPC -32001."""
        deps = _make_deps()
        app = _build_app(deps)
        untrusted_headers = {**_SEND_HEADERS, "Authorization": _UNTRUSTED_BEARER}
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/message/send",
                    json=_send_body("hr.read_balance"),
                    headers=untrusted_headers,
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] is None
        assert body["error"]["code"] == -32001
