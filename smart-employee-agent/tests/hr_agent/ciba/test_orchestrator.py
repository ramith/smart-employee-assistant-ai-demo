"""Tests for hr_agent/ciba/orchestrator.py — Wave 6, Sprint 1.

Test inventory (11 tests):

    1.  tool_not_in_registry_returns_error_payload
    2.  happy_path_returns_consent_required_synchronously
    3.  happy_path_pending_register_called_with_state
    4.  happy_path_completion_set_with_result_payload_after_poll_and_mcp
    5.  ciba_denied_sets_err_ciba_005
    6.  ciba_expired_sets_err_ciba_009
    7.  ciba_timeout_sets_err_ciba_010_polling_timeout
    8.  ciba_timeout_with_cancel_event_sets_reason_cancelled
    9.  mcp_http_error_sets_err_mcp_005
   10.  unexpected_exception_sets_err_agent_internal
   11.  after_done_poll_task_is_none   (F-10 null-out)
   12.  binding_message_rendered_correctly_via_fresh_template

Bootstrap strategy: importlib loading without executing package __init__.py,
mirroring the pattern used in tests/hr_agent/mcp/test_client.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest.mock as mock
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ── Module bootstrap ──────────────────────────────────────────────────────────

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str, rel_dir: str) -> None:
    """Register a stub package namespace if not already in sys.modules."""
    if dotted_name not in sys.modules:
        stub = types.ModuleType(dotted_name)
        stub.__package__ = dotted_name
        stub.__path__ = [str(_ROOT / rel_dir)]  # type: ignore[assignment]
        sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules without executing package __init__."""
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]
    file_path = _ROOT / rel_path
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {file_path}"
    module = importlib.util.module_from_spec(spec)
    module.__package__ = dotted_name.rsplit(".", 1)[0] if "." in dotted_name else ""
    sys.modules[dotted_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# Stub package namespaces
for _pkg, _rel in [
    ("common", "common"),
    ("common.auth", "common/auth"),
    ("common.a2a", "common/a2a"),
    ("common.logging", "common/logging"),
    ("hr_agent", "hr_agent"),
    ("hr_agent.mcp", "hr_agent/mcp"),
    ("hr_agent.ciba", "hr_agent/ciba"),
]:
    _ensure_pkg(_pkg, _rel)

# Load dependency modules in dependency order
_models_mod = _load_module("common.auth.models", "common/auth/models.py")
_errors_mod = _load_module("common.auth.errors", "common/auth/errors.py")
_correlation_mod = _load_module("common.logging.correlation", "common/logging/correlation.py")
_a2a_models_mod = _load_module("common.a2a.models", "common/a2a/models.py")

# Load jsonrpc before server.py
_jsonrpc_mod = _load_module("common.a2a.jsonrpc", "common/a2a/jsonrpc.py")

# jwt_validator and peer_trust needed by server.py — load with stubs for missing deps
_peer_trust_mod = _load_module("common.auth.peer_trust", "common/auth/peer_trust.py")

# jwt_validator requires PyJWT etc — load with a stub if missing
try:
    _jwt_validator_mod = _load_module("common.auth.jwt_validator", "common/auth/jwt_validator.py")
except Exception:
    _jwt_validator_stub = types.ModuleType("common.auth.jwt_validator")
    _jwt_validator_stub.JWKSCache = None  # type: ignore[attr-defined]
    _jwt_validator_stub.ValidatorConfig = None  # type: ignore[attr-defined]
    _jwt_validator_stub.validate = AsyncMock()  # type: ignore[attr-defined]
    sys.modules["common.auth.jwt_validator"] = _jwt_validator_stub

_a2a_server_mod = _load_module("common.a2a.server", "common/a2a/server.py")
_binding_mod = _load_module("common.auth.binding_messages", "common/auth/binding_messages.py")

# Load the MCP client (needed as import in orchestrator)
_hr_mcp_client_mod = _load_module("hr_agent.mcp.client", "hr_agent/mcp/client.py")

# Load the module under test
_orch_mod = _load_module("hr_agent.ciba.orchestrator", "hr_agent/ciba/orchestrator.py")

# Expose names
HRDispatcherDeps = _orch_mod.HRDispatcherDeps
HRDispatcher = _orch_mod.HRDispatcher
A2APendingState = _a2a_server_mod.A2APendingState
ConsentRequiredPayload = _a2a_models_mod.ConsentRequiredPayload
ResultPayload = _a2a_models_mod.ResultPayload
ErrorPayload = _a2a_models_mod.ErrorPayload
OAuthToken = _models_mod.OAuthToken
CIBADeniedError = _errors_mod.CIBADeniedError
CIBAExpiredError = _errors_mod.CIBAExpiredError
CIBATimeoutError = _errors_mod.CIBATimeoutError
FRESH = _binding_mod.FRESH
render = _binding_mod.render

# ── Helpers ───────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


def _make_oauth_token(access_token: str = "token-b") -> OAuthToken:
    """Build a minimal OAuthToken for use as token-B."""
    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=3600,
        expires_at=_NOW,
        refresh_token=None,
        scope="openid hr.read",
        id_token=None,
    )


def _make_ciba_request(auth_req_id: str = "ciba-req-001") -> Any:
    """Build a minimal CIBARequest-like object."""
    req = MagicMock()
    req.auth_req_id = auth_req_id
    req.auth_url = f"https://is.example.com/consent?id={auth_req_id}"
    req.interval_s = 2
    req.expires_in_s = 300
    req.issued_at = _NOW
    return req


def _make_actor_token() -> OAuthToken:
    return _make_oauth_token(access_token="actor-token-value")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def ciba_client() -> MagicMock:
    client = MagicMock()
    client.initiate = AsyncMock(return_value=_make_ciba_request())
    client.poll_for_token = AsyncMock(return_value=_make_oauth_token())
    return client


@pytest.fixture()
def actor_token_provider() -> MagicMock:
    provider = MagicMock()
    provider.ensure_valid_token = AsyncMock(return_value=_make_actor_token())
    return provider


@pytest.fixture()
def mcp_client() -> MagicMock:
    client = MagicMock()
    client.get_leave_balance = AsyncMock(return_value={"leave_days": 12})
    client.get_leave_history = AsyncMock(return_value={"entries": []})
    client.approve_leave = AsyncMock(return_value={"status": "approved"})
    return client


@pytest.fixture()
def deps(
    ciba_client: MagicMock,
    actor_token_provider: MagicMock,
    mcp_client: MagicMock,
) -> HRDispatcherDeps:
    return HRDispatcherDeps(
        ciba_client=ciba_client,
        actor_token_provider=actor_token_provider,
        mcp_client=mcp_client,
        oauth_client_id="hr_agent-client-id",
        oauth_client_secret="hr_agent-client-secret",
        agent_id="hr_agent-uuid",
        agent_label="HR Agent",
        ciba_scope="openid hr.read",
        max_poll_seconds=300.0,
    )


@pytest.fixture()
def dispatcher(deps: HRDispatcherDeps) -> HRDispatcher:
    return HRDispatcher(deps)


def _make_pending_register() -> tuple[list, Any]:
    """Return (captured_states, pending_register) for assertion."""
    captured: list[A2APendingState] = []

    def pending_register(state: A2APendingState) -> None:
        captured.append(state)

    return captured, pending_register


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_not_in_registry_returns_error_payload(
    dispatcher: HRDispatcher,
) -> None:
    """Unknown tool → ErrorPayload with ERR-AGENT-001-tool-not-found."""
    _, pending_register = _make_pending_register()
    result = await dispatcher(
        tool="hr.nonexistent_tool",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-001",
        pending_register=pending_register,
    )
    assert isinstance(result, ErrorPayload)
    assert result.error_id == "ERR-AGENT-001-tool-not-found"
    assert "hr.nonexistent_tool" in result.reason


@pytest.mark.asyncio
async def test_happy_path_returns_consent_required_synchronously(
    dispatcher: HRDispatcher,
) -> None:
    """Happy path: __call__ returns ConsentRequiredPayload without blocking."""
    _, pending_register = _make_pending_register()
    result = await dispatcher(
        tool="hr.read_balance",
        args={"employee_id": "emp-001"},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-002",
        pending_register=pending_register,
    )
    assert isinstance(result, ConsentRequiredPayload)
    assert result.auth_req_id == "ciba-req-001"
    assert result.auth_url.startswith("https://is.example.com/consent")
    assert result.agent_label == "HR Agent"
    assert result.action == "View your leave balance"
    assert result.scope == "openid hr.read"
    assert result.expires_in == 300


@pytest.mark.asyncio
async def test_happy_path_pending_register_called_with_state(
    dispatcher: HRDispatcher,
) -> None:
    """pending_register must be called with an A2APendingState holding the auth_req_id."""
    captured, pending_register = _make_pending_register()
    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-003",
        pending_register=pending_register,
    )
    assert len(captured) == 1
    state = captured[0]
    assert state.auth_req_id == "ciba-req-001"
    assert state.request_id == "req-003"
    assert state.completion is not None
    assert state.cancel_event is not None


@pytest.mark.asyncio
async def test_happy_path_completion_set_with_result_payload(
    dispatcher: HRDispatcher,
    mcp_client: MagicMock,
) -> None:
    """After poll+MCP complete, state.completion is set and state.result is ResultPayload."""
    captured, pending_register = _make_pending_register()
    mcp_client.get_leave_balance = AsyncMock(return_value={"leave_days": 12})

    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-004",
        pending_register=pending_register,
    )
    state = captured[0]

    # Wait for background task
    await asyncio.wait_for(state.completion.wait(), timeout=2.0)

    assert state.result is not None
    assert isinstance(state.result, ResultPayload)
    assert state.result.data == {"leave_days": 12}
    assert state.error is None


@pytest.mark.asyncio
async def test_ciba_denied_sets_err_ciba_005(
    dispatcher: HRDispatcher,
    ciba_client: MagicMock,
) -> None:
    """CIBADeniedError from poll → state.error = ErrorPayload(ERR-CIBA-005)."""
    ciba_client.poll_for_token = AsyncMock(
        side_effect=CIBADeniedError("User denied", details={"auth_req_id": "ciba-req-001"})
    )
    captured, pending_register = _make_pending_register()
    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-005",
        pending_register=pending_register,
    )
    state = captured[0]
    await asyncio.wait_for(state.completion.wait(), timeout=2.0)

    assert state.error is not None
    assert state.error.error_id == "ERR-CIBA-005"
    assert state.error.reason == "user_denied"
    assert state.result is None


@pytest.mark.asyncio
async def test_ciba_expired_sets_err_ciba_009(
    dispatcher: HRDispatcher,
    ciba_client: MagicMock,
) -> None:
    """CIBAExpiredError from poll → state.error = ErrorPayload(ERR-CIBA-009)."""
    ciba_client.poll_for_token = AsyncMock(
        side_effect=CIBAExpiredError("Expired", details={"auth_req_id": "ciba-req-001"})
    )
    captured, pending_register = _make_pending_register()
    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-006",
        pending_register=pending_register,
    )
    state = captured[0]
    await asyncio.wait_for(state.completion.wait(), timeout=2.0)

    assert state.error is not None
    assert state.error.error_id == "ERR-CIBA-009"
    assert state.error.reason == "auth_req_id_expired"


@pytest.mark.asyncio
async def test_ciba_timeout_sets_err_ciba_010_polling_timeout(
    dispatcher: HRDispatcher,
    ciba_client: MagicMock,
) -> None:
    """CIBATimeoutError (no cancel) → ERR-CIBA-010, reason=polling_timeout."""
    ciba_client.poll_for_token = AsyncMock(
        side_effect=CIBATimeoutError("Timeout", details={"auth_req_id": "ciba-req-001"})
    )
    captured, pending_register = _make_pending_register()
    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-007",
        pending_register=pending_register,
    )
    state = captured[0]
    # cancel_event NOT set — should be polling_timeout
    await asyncio.wait_for(state.completion.wait(), timeout=2.0)

    assert state.error is not None
    assert state.error.error_id == "ERR-CIBA-010"
    assert state.error.reason == "polling_timeout"


@pytest.mark.asyncio
async def test_ciba_timeout_with_cancel_event_sets_reason_cancelled(
    dispatcher: HRDispatcher,
    ciba_client: MagicMock,
) -> None:
    """CIBATimeoutError when cancel_event set → reason=cancelled."""
    async def _poll_that_honours_cancel(**kwargs: Any) -> OAuthToken:
        cancel_event = kwargs.get("cancel_event")
        if cancel_event is not None:
            cancel_event.set()
        raise CIBATimeoutError("cancelled", details={"reason": "cancelled"})

    ciba_client.poll_for_token = AsyncMock(side_effect=_poll_that_honours_cancel)
    captured, pending_register = _make_pending_register()
    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-008",
        pending_register=pending_register,
    )
    state = captured[0]
    await asyncio.wait_for(state.completion.wait(), timeout=2.0)

    assert state.error is not None
    assert state.error.error_id == "ERR-CIBA-010"
    assert state.error.reason == "cancelled"


@pytest.mark.asyncio
async def test_mcp_http_error_sets_err_mcp_005(
    dispatcher: HRDispatcher,
    mcp_client: MagicMock,
) -> None:
    """httpx.HTTPStatusError from MCP → state.error = ErrorPayload(ERR-MCP-005)."""
    mock_response = MagicMock()
    mock_response.status_code = 401
    mcp_client.get_leave_balance = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "401 Unauthorized", request=MagicMock(), response=mock_response
        )
    )
    captured, pending_register = _make_pending_register()
    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-009",
        pending_register=pending_register,
    )
    state = captured[0]
    await asyncio.wait_for(state.completion.wait(), timeout=2.0)

    assert state.error is not None
    assert state.error.error_id == "ERR-MCP-005"
    assert "401" in state.error.reason


@pytest.mark.asyncio
async def test_unexpected_exception_sets_err_agent_internal(
    dispatcher: HRDispatcher,
    mcp_client: MagicMock,
) -> None:
    """Any unexpected Exception → state.error = ErrorPayload(ERR-AGENT-INTERNAL)."""
    mcp_client.get_leave_balance = AsyncMock(
        side_effect=RuntimeError("something went very wrong")
    )
    captured, pending_register = _make_pending_register()
    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-010",
        pending_register=pending_register,
    )
    state = captured[0]
    await asyncio.wait_for(state.completion.wait(), timeout=2.0)

    assert state.error is not None
    assert state.error.error_id == "ERR-AGENT-INTERNAL"
    assert "something went very wrong" in state.error.reason


@pytest.mark.asyncio
async def test_after_done_poll_task_is_none(
    dispatcher: HRDispatcher,
) -> None:
    """F-10 rule 3: state.poll_task is None after the background task completes."""
    captured, pending_register = _make_pending_register()
    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="req-011",
        pending_register=pending_register,
    )
    state = captured[0]
    await asyncio.wait_for(state.completion.wait(), timeout=2.0)

    # Allow event loop to fire the done_callback
    await asyncio.sleep(0)

    assert state.poll_task is None, "F-10: poll_task must be None after task completes"


@pytest.mark.asyncio
async def test_binding_message_rendered_correctly_via_fresh_template(
    dispatcher: HRDispatcher,
    ciba_client: MagicMock,
) -> None:
    """CIBAClient.initiate must receive a binding_message rendered via render(FRESH, ...)."""
    _, pending_register = _make_pending_register()
    await dispatcher(
        tool="hr.read_balance",
        args={},
        user_sub="user-sub-001",
        orchestrator_act_sub="orch-sub",
        request_id="abcdef12-long-request-id",
        pending_register=pending_register,
    )

    call_kwargs = ciba_client.initiate.call_args.kwargs
    binding_message: str = call_kwargs["binding_message"]

    expected = render(
        FRESH,
        agent_label="HR Agent",
        action="View your leave balance",
        request_id="abcdef12-long-request-id",
    )
    assert binding_message == expected, (
        f"Expected binding_message={expected!r}, got {binding_message!r}"
    )
    # Verify the short-ID is in the message
    assert "abcdef12" in binding_message
