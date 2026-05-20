"""Tests for common/a2a/server.py — Wave 3, Sprint 1.

Coverage
--------
 1. Valid token + tool=instant  → ResultPayload returned in JSON-RPC result.
 2. Valid token + tool=ciba     → ConsentRequiredPayload; pending dict has entry.
 3. Valid token + tool=error    → ErrorPayload returned in JSON-RPC result.
 4. Bad bearer (validator raises) → JSON-RPC -32002.
 5. Bearer with untrusted act.sub → JSON-RPC -32001.
 6. After ciba ConsentRequired, /a2a/await returns eventual ResultPayload.
 7. /a2a/await on unknown auth_req_id → JSON-RPC -32004.
 8. /a2a/await timeout (await_max_wait_seconds=0.1) → ErrorPayload(ERR-CIBA-010).
 9. /a2a/cancel on existing pending → CancelResponse(cancelled=True); subsequent
    /a2a/await returns ErrorPayload.
10. /a2a/cancel on unknown auth_req_id → CancelResponse(cancelled=False, reason="not_found").
11. Successful /a2a/await deletes entry from pending dict (size shrinks to 0).
12. Missing Authorization header → JSON-RPC -32002.
13. Bad JSON-RPC envelope (wrong method) → JSON-RPC -32600.

Design notes
------------
- ``AsyncClient`` from ``httpx`` drives the tests against the ``FastAPI`` app built
  around the router returned by ``build_a2a_router``.
- JWT validation is stubbed via a module-level ``_validate`` override injected into
  ``common.auth.jwt_validator``; no real JWKS network traffic occurs.
- Peer trust is stubbed by monkey-patching ``common.auth.peer_trust.validate_chain``;
  the stub raises ``PeerTrustError`` when the act.sub is not the trusted UUID.
- The fake dispatcher covers four tool names (instant, ciba, error, will_be_cancelled).
"""

from __future__ import annotations

import asyncio
import sys
import types
import importlib.util
import pathlib
from datetime import datetime, timezone
from typing import Callable
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Bootstrap: ensure the common.* packages are importable without running their
# __init__.py files (mirrors tests/conftest.py pattern).
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
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


for _pkg in (
    "common",
    "common.auth",
    "common.a2a",
):
    _ensure_pkg(_pkg)

_load("common.auth.models", "common/auth/models.py")
_load("common.auth.errors", "common/auth/errors.py")
_load("common.auth.peer_trust", "common/auth/peer_trust.py")
_load("common.a2a.jsonrpc", "common/a2a/jsonrpc.py")
_load("common.a2a.models", "common/a2a/models.py")

# jwt_validator needs httpx + jwt at import time; load it and then stub validate().
_load("common.auth.jwt_validator", "common/auth/jwt_validator.py")
_load("common.a2a.server", "common/a2a/server.py")

# ---------------------------------------------------------------------------
# Import the concrete symbols now that modules are in sys.modules.
# ---------------------------------------------------------------------------

from common.a2a.models import (  # noqa: E402
    A2AMessageResponse,
    CancelResponse,
    ConsentRequiredPayload,
    ErrorPayload,
    MessageSendParams,
    ResultPayload,
)
from common.a2a.server import (  # noqa: E402
    A2APendingState,
    A2ARouterConfig,
    DispatchProtocol,
    build_a2a_router,
)
from common.auth.errors import JWTValidationError, PeerTrustError  # noqa: E402
from common.auth.jwt_validator import ValidatorConfig  # noqa: E402
from common.auth.models import JWTClaims  # noqa: E402

# ---------------------------------------------------------------------------
# Fake JWT claims shared across tests
# ---------------------------------------------------------------------------

_TRUSTED_ORCH_SUB = "orch-agent-uuid-0001"
_USER_SUB = "user-sub-abc123"
_GOOD_BEARER = "Bearer valid.fake.token"
_BAD_BEARER = "Bearer invalid.token"
_UNTRUSTED_BEARER = "Bearer untrusted.act.token"

_GOOD_CLAIMS = JWTClaims(
    sub=_USER_SUB,
    iss="https://is.example.com/oauth2/token",
    aud="hr_agent-client-id",
    exp=9999999999,
    iat=1700000000,
    jti="jti-good-001",
    act={"sub": _TRUSTED_ORCH_SUB},
    scope="openid orchestrate",
    aut="APPLICATION_USER",
)

_UNTRUSTED_CLAIMS = JWTClaims(
    sub=_USER_SUB,
    iss="https://is.example.com/oauth2/token",
    aud="hr_agent-client-id",
    exp=9999999999,
    iat=1700000000,
    jti="jti-untrusted-001",
    act={"sub": "attacker-agent-uuid"},
    scope="openid orchestrate",
    aut="APPLICATION_USER",
)


# ---------------------------------------------------------------------------
# Stub JWT validator
# ---------------------------------------------------------------------------


async def _stub_validate(token: str, config: ValidatorConfig, *, jwks_cache=None) -> JWTClaims:
    """Return synthetic claims for known tokens; raise JWTValidationError otherwise."""
    raw = token.split(" ", 1)[-1]  # strip "Bearer " if present
    if raw in ("valid.fake.token",):
        return _GOOD_CLAIMS
    if raw in ("untrusted.act.token",):
        return _UNTRUSTED_CLAIMS
    raise JWTValidationError("Stub: unrecognised test token", error_id="ERR-AUTH-006")


# ---------------------------------------------------------------------------
# Fake dispatcher
# ---------------------------------------------------------------------------

_CIBA_AUTH_REQ_ID = "ciba-ari-test-001"
_CANCEL_AUTH_REQ_ID = "ciba-ari-cancel-001"


class FakeDispatcher:
    """Test dispatcher covering instant, ciba, error, and will_be_cancelled tools."""

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
        last_logout_reason: str | None = None,
    ) -> A2AMessageResponse:
        self.calls.append(
            {
                "tool": tool,
                "args": args,
                "user_sub": user_sub,
                "request_id": request_id,
                "last_logout_reason": last_logout_reason,
            }
        )

        if tool == "instant":
            return ResultPayload(
                data={"leave_days": 12},
                token_jti="jti-instant-001",
                token_exp=9999999999,
                token_iat=1700000000,
            )

        if tool == "error":
            return ErrorPayload(error_id="ERR-CIBA-005", reason="user_denied_consent")

        if tool == "ciba":
            state = A2APendingState(
                auth_req_id=_CIBA_AUTH_REQ_ID,
                request_id=request_id,
                started_at=datetime.now(tz=timezone.utc),
                poll_task=None,
                completion=asyncio.Event(),
                cancel_event=asyncio.Event(),
            )
            pending_register(state)

            # Schedule completion in 50 ms (simulates background poll task done-callback).
            async def _complete() -> None:
                await asyncio.sleep(0.05)
                state.result = ResultPayload(
                    data={"leave_days": 7},
                    token_jti="jti-ciba-done",
                    token_exp=9999999999,
                    token_iat=1700000000,
                )
                state.completion.set()

            asyncio.ensure_future(_complete())

            return ConsentRequiredPayload(
                auth_req_id=_CIBA_AUTH_REQ_ID,
                auth_url="https://is.example.com/ciba?req=test",
                agent_label="HR Agent",
                action="View your leave balance",
                scope="openid hr.read",
                binding_message="HR Agent wants to View your leave balance — request ciba-ari",
                expires_in=300,
            )

        if tool == "will_be_cancelled":
            state = A2APendingState(
                auth_req_id=_CANCEL_AUTH_REQ_ID,
                request_id=request_id,
                started_at=datetime.now(tz=timezone.utc),
                poll_task=None,
                completion=asyncio.Event(),
                cancel_event=asyncio.Event(),
            )
            pending_register(state)

            # Watch cancel_event; on signal → write ErrorPayload + set completion.
            async def _watch_cancel() -> None:
                await state.cancel_event.wait()
                state.error = ErrorPayload(
                    error_id="ERR-CIBA-010", reason="cancelled_by_orchestrator"
                )
                state.completion.set()

            asyncio.ensure_future(_watch_cancel())

            return ConsentRequiredPayload(
                auth_req_id=_CANCEL_AUTH_REQ_ID,
                auth_url="https://is.example.com/ciba?req=cancel",
                agent_label="HR Agent",
                action="View your leave balance",
                scope="openid hr.read",
                binding_message="HR Agent wants to View your leave balance — request ciba-cancel",
                expires_in=300,
            )

        # Unknown tool — return error
        return ErrorPayload(error_id="ERR-AGENT-001", reason=f"unknown_tool:{tool}")


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pending() -> dict[str, A2APendingState]:
    return {}


@pytest.fixture()
def fake_dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


@pytest.fixture()
def validator_config() -> ValidatorConfig:
    return ValidatorConfig(
        expected_iss="https://is.example.com/oauth2/token",
        jwks_url="https://is.example.com/oauth2/jwks",
        expected_aud="hr_agent-client-id",
    )


@pytest.fixture()
def router_config(
    pending: dict[str, A2APendingState],
    fake_dispatcher: FakeDispatcher,
    validator_config: ValidatorConfig,
) -> A2ARouterConfig:
    return A2ARouterConfig(
        validator_config=validator_config,
        trusted_orchestrator_subs=frozenset({_TRUSTED_ORCH_SUB}),
        pending=pending,
        dispatch=fake_dispatcher,
        await_max_wait_seconds=5.0,
    )


@pytest.fixture()
def fast_timeout_router_config(
    pending: dict[str, A2APendingState],
    fake_dispatcher: FakeDispatcher,
    validator_config: ValidatorConfig,
) -> A2ARouterConfig:
    """Config with a very short await timeout to exercise the timeout path."""
    return A2ARouterConfig(
        validator_config=validator_config,
        trusted_orchestrator_subs=frozenset({_TRUSTED_ORCH_SUB}),
        pending=pending,
        dispatch=fake_dispatcher,
        await_max_wait_seconds=0.1,
    )


def _build_app(config: A2ARouterConfig) -> FastAPI:
    app = FastAPI()
    app.include_router(build_a2a_router(config))
    return app


_SEND_HEADERS = {
    "Authorization": _GOOD_BEARER,
    "X-Request-ID": "req-test-001",
    "Content-Type": "application/json",
}

_AWAIT_HEADERS = {
    "Authorization": _GOOD_BEARER,
    "X-Request-ID": "req-test-await",
    "Content-Type": "application/json",
}

_CANCEL_HEADERS = {
    "Authorization": _GOOD_BEARER,
    "X-Request-ID": "req-test-cancel",
    "Content-Type": "application/json",
}


def _send_body(tool: str, args: dict | None = None, rpc_id: str = "rpc-1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "message/send",
        "params": {"tool": tool, "args": args or {}},
    }


def _await_body(auth_req_id: str, rpc_id: str = "rpc-await-1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "await",
        "params": {"auth_req_id": auth_req_id},
    }


def _cancel_body(auth_req_id: str) -> dict:
    return {"auth_req_id": auth_req_id}


# ---------------------------------------------------------------------------
# Helper: patch validate() in the server module's namespace
# ---------------------------------------------------------------------------


def _patch_validate():
    return patch("common.a2a.server.validate", side_effect=_stub_validate)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMessageSend:
    """Tests for POST /a2a/message/send."""

    async def test_instant_tool_returns_result_payload(
        self, router_config: A2ARouterConfig
    ) -> None:
        """TC-1: Valid token + tool=instant → ResultPayload in JSON-RPC result."""
        app = _build_app(router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/message/send", json=_send_body("instant"), headers=_SEND_HEADERS
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["jsonrpc"] == "2.0"
        assert body["error"] is None
        result = body["result"]
        assert result["type"] == "result"
        assert result["data"] == {"leave_days": 12}
        assert result["token_jti"] == "jti-instant-001"

    async def test_ciba_tool_returns_consent_required_and_pending_populated(
        self, router_config: A2ARouterConfig, pending: dict
    ) -> None:
        """TC-2: Valid token + tool=ciba → ConsentRequiredPayload; pending has entry."""
        app = _build_app(router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/message/send", json=_send_body("ciba"), headers=_SEND_HEADERS
                )
        assert resp.status_code == 200
        body = resp.json()
        result = body["result"]
        assert result["type"] == "consent_required"
        assert result["auth_req_id"] == _CIBA_AUTH_REQ_ID
        assert result["auth_url"].startswith("https://")
        # Pending dict must contain the new entry.
        assert _CIBA_AUTH_REQ_ID in pending

    async def test_error_tool_returns_error_payload(
        self, router_config: A2ARouterConfig
    ) -> None:
        """TC-3: Valid token + tool=error → ErrorPayload in JSON-RPC result."""
        app = _build_app(router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/message/send", json=_send_body("error"), headers=_SEND_HEADERS
                )
        assert resp.status_code == 200
        body = resp.json()
        result = body["result"]
        assert result["type"] == "error"
        assert result["error_id"] == "ERR-CIBA-005"
        assert result["reason"] == "user_denied_consent"

    async def test_bad_bearer_returns_32002(self, router_config: A2ARouterConfig) -> None:
        """TC-4: Invalid JWT → JSON-RPC -32002 (ERR_INVALID_TOKEN_A)."""
        app = _build_app(router_config)
        bad_headers = {**_SEND_HEADERS, "Authorization": _BAD_BEARER}
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/message/send", json=_send_body("instant"), headers=bad_headers
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] is None
        assert body["error"]["code"] == -32002

    async def test_untrusted_act_sub_returns_32001(
        self, router_config: A2ARouterConfig
    ) -> None:
        """TC-5: Token with untrusted act.sub → JSON-RPC -32001 (ERR_PEER_NOT_TRUSTED)."""
        app = _build_app(router_config)
        untrusted_headers = {**_SEND_HEADERS, "Authorization": _UNTRUSTED_BEARER}
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/message/send", json=_send_body("instant"), headers=untrusted_headers
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] is None
        assert body["error"]["code"] == -32001

    async def test_missing_authorization_returns_32002(
        self, router_config: A2ARouterConfig
    ) -> None:
        """TC-12: No Authorization header → JSON-RPC -32002."""
        app = _build_app(router_config)
        no_auth = {k: v for k, v in _SEND_HEADERS.items() if k != "Authorization"}
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/message/send", json=_send_body("instant"), headers=no_auth
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"]["code"] == -32002

    async def test_bad_method_returns_32600(self, router_config: A2ARouterConfig) -> None:
        """TC-13: Wrong JSON-RPC method → JSON-RPC -32600 (INVALID_REQUEST)."""
        app = _build_app(router_config)
        bad_method_body = {
            "jsonrpc": "2.0",
            "id": "rpc-bad",
            "method": "wrong/method",
            "params": {"tool": "instant", "args": {}},
        }
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/message/send", json=bad_method_body, headers=_SEND_HEADERS
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"]["code"] == -32600


@pytest.mark.asyncio
class TestAwait:
    """Tests for POST /a2a/await."""

    async def test_await_after_ciba_returns_result_payload(
        self, router_config: A2ARouterConfig, pending: dict
    ) -> None:
        """TC-6: After ConsentRequired from /message/send, /a2a/await returns ResultPayload."""
        app = _build_app(router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Step 1: initiate CIBA
                send_resp = await client.post(
                    "/a2a/message/send", json=_send_body("ciba"), headers=_SEND_HEADERS
                )
                assert send_resp.json()["result"]["type"] == "consent_required"

                # Step 2: long-poll await — dispatcher will resolve in 50 ms
                await_resp = await client.post(
                    "/a2a/await",
                    json=_await_body(_CIBA_AUTH_REQ_ID),
                    headers=_AWAIT_HEADERS,
                )

        assert await_resp.status_code == 200
        body = await_resp.json()
        result = body["result"]
        assert result["type"] == "result"
        assert result["data"] == {"leave_days": 7}
        assert result["token_jti"] == "jti-ciba-done"

    async def test_await_unknown_auth_req_id_returns_32004(
        self, router_config: A2ARouterConfig
    ) -> None:
        """TC-7: /a2a/await with unknown auth_req_id → JSON-RPC -32004."""
        app = _build_app(router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/await",
                    json=_await_body("nonexistent-ari"),
                    headers=_AWAIT_HEADERS,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] is None
        assert body["error"]["code"] == -32004

    async def test_await_timeout_returns_error_payload_ciba_010(
        self,
        fast_timeout_router_config: A2ARouterConfig,
        pending: dict,
        fake_dispatcher: FakeDispatcher,
    ) -> None:
        """TC-8: /a2a/await with short timeout → ErrorPayload(ERR-CIBA-010)."""
        app = _build_app(fast_timeout_router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Use tool=ciba which completes in 50 ms — but timeout is 0.1 s.
                # We need a tool that NEVER completes; use will_be_cancelled but
                # don't cancel — the timeout fires first.  Use a custom state
                # inserted directly into pending.
                never_event = asyncio.Event()  # never set
                timeout_state = A2APendingState(
                    auth_req_id="timeout-ari-001",
                    request_id="req-timeout",
                    started_at=datetime.now(tz=timezone.utc),
                    poll_task=None,
                    completion=never_event,
                    cancel_event=asyncio.Event(),
                )
                pending["timeout-ari-001"] = timeout_state

                resp = await client.post(
                    "/a2a/await",
                    json=_await_body("timeout-ari-001"),
                    headers=_AWAIT_HEADERS,
                )

        assert resp.status_code == 200
        body = resp.json()
        result = body["result"]
        assert result["type"] == "error"
        assert result["error_id"] == "ERR-CIBA-010"
        assert result["reason"] == "server_await_timeout"
        # Entry must have been cleaned up.
        assert "timeout-ari-001" not in pending

    async def test_successful_await_removes_entry_from_pending(
        self, router_config: A2ARouterConfig, pending: dict
    ) -> None:
        """TC-11: After /a2a/await completes, pending dict shrinks to 0."""
        app = _build_app(router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/a2a/message/send", json=_send_body("ciba"), headers=_SEND_HEADERS
                )
                assert len(pending) == 1

                await client.post(
                    "/a2a/await",
                    json=_await_body(_CIBA_AUTH_REQ_ID),
                    headers=_AWAIT_HEADERS,
                )

        assert len(pending) == 0, f"pending dict should be empty after await; got {pending}"


@pytest.mark.asyncio
class TestCancel:
    """Tests for POST /a2a/cancel."""

    async def test_cancel_existing_pending_returns_true(
        self, router_config: A2ARouterConfig, pending: dict
    ) -> None:
        """TC-9a: /a2a/cancel on existing pending → CancelResponse(cancelled=True)."""
        app = _build_app(router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Start will_be_cancelled flow.
                await client.post(
                    "/a2a/message/send",
                    json=_send_body("will_be_cancelled"),
                    headers=_SEND_HEADERS,
                )
                assert _CANCEL_AUTH_REQ_ID in pending

                # Cancel it.
                resp = await client.post(
                    "/a2a/cancel",
                    json=_cancel_body(_CANCEL_AUTH_REQ_ID),
                    headers=_CANCEL_HEADERS,
                )

        assert resp.status_code == 200
        cr = CancelResponse.model_validate(resp.json())
        assert cr.cancelled is True
        assert cr.reason == "signal_sent"

    async def test_cancel_then_await_returns_error_payload(
        self, router_config: A2ARouterConfig, pending: dict
    ) -> None:
        """TC-9b: After cancel, /a2a/await returns ErrorPayload (cancelled)."""
        app = _build_app(router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/a2a/message/send",
                    json=_send_body("will_be_cancelled"),
                    headers=_SEND_HEADERS,
                )
                await client.post(
                    "/a2a/cancel",
                    json=_cancel_body(_CANCEL_AUTH_REQ_ID),
                    headers=_CANCEL_HEADERS,
                )
                # Give the _watch_cancel coroutine time to react.
                await asyncio.sleep(0.02)

                resp = await client.post(
                    "/a2a/await",
                    json=_await_body(_CANCEL_AUTH_REQ_ID),
                    headers=_AWAIT_HEADERS,
                )

        assert resp.status_code == 200
        body = resp.json()
        result = body["result"]
        assert result["type"] == "error"
        assert result["error_id"] == "ERR-CIBA-010"

    async def test_cancel_unknown_auth_req_id_returns_false(
        self, router_config: A2ARouterConfig
    ) -> None:
        """TC-10: /a2a/cancel on unknown auth_req_id → CancelResponse(cancelled=False, reason="not_found")."""
        app = _build_app(router_config)
        with _patch_validate():
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/a2a/cancel",
                    json=_cancel_body("nonexistent-ari"),
                    headers=_CANCEL_HEADERS,
                )
        assert resp.status_code == 200
        cr = CancelResponse.model_validate(resp.json())
        assert cr.cancelled is False
        assert cr.reason == "not_found"
