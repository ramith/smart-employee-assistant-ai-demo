"""Tests for orchestrator/chat/routes.py — Wave 7, Sprint 1.

Coverage (13 tests)
-------------------
 1.  POST /api/chat — no cookie → 401
 2.  POST /api/chat — cookie with unknown session_id → 401
 3.  POST /api/chat — no keyword match → "I don't know" ChatMessageEvent pushed; ChatAck returned
 4.  POST /api/chat — single specialist, message_send returns ResultPayload → ChatMessageEvent pushed
 5.  POST /api/chat — single specialist with CIBA:
       message_send → ConsentRequiredPayload → CibaUrlEvent pushed
       → await_completion → ResultPayload → DONE state → ChatMessageEvent pushed
 6.  POST /api/chat — two specialists, serial fan-out: second only starts after first resolves;
       both RoutingEvents and both ChatMessageEvents delivered in order
 7.  POST /api/chat — mid-flow denial (UC-04 EX-3):
       first specialist returns ResultPayload OK,
       second specialist returns ErrorPayload(ERR-CIBA-005) → graceful degradation;
       final ChatMessage explains partial result
 8.  POST /api/chat — first specialist returns ErrorPayload → continues to second specialist
 9.  POST /api/chat — returns ChatAck immediately (fan-out runs async in background)
10.  POST /api/ciba/cancel — known auth_req_id → cancel called on A2AClient + cancel_event set
11.  POST /api/ciba/cancel — unknown auth_req_id → cancelled=False, reason="not_found"
12.  After successful CIBA, session.completed_ciba_log has the new IssuedTokenRecord
13.  POST /api/chat — agent_id not in registry → error event pushed; continues gracefully
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Module isolation helpers
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str) -> None:
    """Register a bare package stub in sys.modules if not already present."""
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    stub.__path__ = [str(_ROOT / dotted_name.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules under *dotted_name*."""
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


# Ensure all intermediate package namespaces are registered before loading.
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

# Load dependency modules in topological order.
_auth_models_mod = _load_module("common.auth.models", "common/auth/models.py")
_correlation_mod = _load_module(
    "common.logging.correlation", "common/logging/correlation.py"
)
_a2a_models_mod = _load_module("common.a2a.models", "common/a2a/models.py")
_session_store_mod = _load_module(
    "orchestrator.auth.session_store", "orchestrator/auth/session_store.py"
)
_sse_mod = _load_module("orchestrator.events.sse", "orchestrator/events/sse.py")
_keyword_mod = _load_module(
    "orchestrator.chat.keyword_fallback", "orchestrator/chat/keyword_fallback.py"
)

# Bind public names.
OAuthToken = _auth_models_mod.OAuthToken
SessionStore = _session_store_mod.SessionStore
Session = _session_store_mod.Session
PendingCIBA = _session_store_mod.PendingCIBA
IssuedTokenRecord = _session_store_mod.IssuedTokenRecord
ConsentRequiredPayload = _a2a_models_mod.ConsentRequiredPayload
ResultPayload = _a2a_models_mod.ResultPayload
ErrorPayload = _a2a_models_mod.ErrorPayload
CancelResponse = _a2a_models_mod.CancelResponse
ToolCall = _keyword_mod.ToolCall
ChatMessageEvent = _sse_mod.ChatMessageEvent
CibaUrlEvent = _sse_mod.CibaUrlEvent
CibaStateChangeEvent = _sse_mod.CibaStateChangeEvent
RoutingEvent = _sse_mod.RoutingEvent
SseErrorEvent = _sse_mod.SseErrorEvent

# Load routes module last (depends on everything above).
_routes_mod = _load_module("orchestrator.chat.routes", "orchestrator/chat/routes.py")
ChatRouterDeps = _routes_mod.ChatRouterDeps
ChatRequest = _routes_mod.ChatRequest
ChatAck = _routes_mod.ChatAck
CibaCancelRequest = _routes_mod.CibaCancelRequest
CibaCancelResponse = _routes_mod.CibaCancelResponse
build_chat_router = _routes_mod.build_chat_router


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_oauth_token(access_token: str = "tok-access") -> OAuthToken:
    now = _utc_now()
    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=3600,
        expires_at=now + timedelta(seconds=3600),
        refresh_token=None,
        scope="openid orchestrate",
        id_token=None,
    )


def _make_session(session_id: str = "sess-001") -> Session:
    return Session(
        session_id=session_id,
        user_sub="user-sub-123",
        user_label="Alice",
        token_a=_make_oauth_token(),
        pkce_state=None,
        code_verifier=None,
        sse_queue=asyncio.Queue(),
    )


def _make_agent_card(agent_id: str, label: str) -> MagicMock:
    card = MagicMock()
    card.id = agent_id
    card.label = label
    return card


def _result_payload(data: dict | None = None) -> ResultPayload:
    return ResultPayload(
        data=data or {"leave_days": 12},
        token_jti="jti-abc-123",
        token_exp=9999999999,
        token_iat=1000000000,
    )


def _consent_payload(
    auth_req_id: str = "auth-req-001",
    auth_url: str = "https://is.example.com/consent?auth_req_id=auth-req-001",
) -> ConsentRequiredPayload:
    return ConsentRequiredPayload(
        auth_req_id=auth_req_id,
        auth_url=auth_url,
        agent_label="HR Agent",
        action="View your leave balance",
        scope="openid hr.read",
        binding_message="HR Agent wants to view your leave balance — request abcd1234",
        expires_in=300,
        is_refresh=False,
        prior_consent_at=None,
    )


def _error_payload(error_id: str = "ERR-CIBA-005") -> ErrorPayload:
    return ErrorPayload(error_id=error_id, reason="user_denied")


def _make_config(session_cookie_name: str = "orch_sid") -> MagicMock:
    cfg = MagicMock()
    cfg.session_cookie_name = session_cookie_name
    return cfg


# ---------------------------------------------------------------------------
# App builder — constructs a minimal FastAPI instance for each test
# ---------------------------------------------------------------------------


def _build_app(
    *,
    session: Session | None = None,
    session_id: str = "sess-001",
    tool_calls: list[ToolCall] | None = None,
    hr_a2a_client: MagicMock | None = None,
    it_a2a_client: MagicMock | None = None,
    hr_card_present: bool = True,
    it_card_present: bool = True,
) -> tuple[FastAPI, Session]:
    """Build a FastAPI test app wired with controlled mocks.

    Returns (app, session) so tests can inspect session state post-request.
    """
    if session is None:
        session = _make_session(session_id)

    # Mock SessionStore.
    mock_store = MagicMock(spec=SessionStore)

    async def _get_or_404(sid: str) -> Session:
        if sid == session.session_id:
            return session
        raise KeyError(sid)

    mock_store.get_or_404 = AsyncMock(side_effect=_get_or_404)

    # Mock KeywordRouter.
    mock_router = MagicMock()
    mock_router.route.return_value = tool_calls if tool_calls is not None else []

    # Mock AgentRegistry.
    mock_registry = MagicMock()

    def _registry_get(aid: str) -> MagicMock | None:
        if aid == "hr_agent" and hr_card_present:
            return _make_agent_card("hr_agent", "HR Agent")
        if aid == "it_agent" and it_card_present:
            return _make_agent_card("it_agent", "IT Agent")
        return None

    mock_registry.get.side_effect = _registry_get

    # Wire A2A clients.
    a2a_clients: dict[str, MagicMock] = {}
    if hr_a2a_client is not None:
        a2a_clients["hr_agent"] = hr_a2a_client
    if it_a2a_client is not None:
        a2a_clients["it_agent"] = it_a2a_client

    deps = ChatRouterDeps(
        config=_make_config(),
        session_store=mock_store,
        keyword_router=mock_router,
        agent_registry=mock_registry,
        a2a_clients=a2a_clients,
    )

    app = FastAPI()
    app.include_router(build_chat_router(deps))
    return app, session


# ---------------------------------------------------------------------------
# Helper: drain all events from the session SSE queue (synchronous)
# ---------------------------------------------------------------------------


def _drain_queue(session: Session) -> list:
    """Drain and return all items currently in the session's SSE queue."""
    items = []
    while not session.sse_queue.empty():
        items.append(session.sse_queue.get_nowait())
    return items


# ---------------------------------------------------------------------------
# Test 1 — no cookie → 401
# ---------------------------------------------------------------------------


def test_chat_no_cookie_returns_401() -> None:
    """POST /api/chat with no cookie must return 401."""
    app, _ = _build_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/chat", json={"message": "What is my leave?"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test 2 — unknown session_id cookie → 401
# ---------------------------------------------------------------------------


def test_chat_unknown_session_returns_401() -> None:
    """POST /api/chat with an unrecognised session cookie must return 401."""
    app, _ = _build_app(session_id="sess-known")
    client = TestClient(app, raise_server_exceptions=False)
    # Send a different session_id that does not exist in the store.
    resp = client.post(
        "/api/chat",
        json={"message": "leave"},
        cookies={"orch_sid": "sess-unknown"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test 3 — empty keyword route → "I don't know" event pushed; ChatAck returned
# ---------------------------------------------------------------------------


def test_chat_no_match_pushes_dont_know_event() -> None:
    """When no tool is matched, a ChatMessageEvent with the fallback message is pushed."""
    app, session = _build_app(tool_calls=[])
    client = TestClient(app)

    resp = client.post(
        "/api/chat",
        json={"message": "what is the weather today?"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200
    ack = resp.json()
    assert ack["ok"] is True
    assert "request_id" in ack

    # The "I don't know" ChatMessageEvent must be in the SSE queue.
    events = _drain_queue(session)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ChatMessageEvent)
    assert "don't know" in event.content.lower()


# ---------------------------------------------------------------------------
# Test 4 — single specialist, ResultPayload on first call → ChatMessage pushed
# ---------------------------------------------------------------------------


def test_chat_single_specialist_result_direct() -> None:
    """message_send returning ResultPayload immediately must push a ChatMessageEvent."""
    result = _result_payload({"leave_days": 12})

    hr_client = MagicMock()
    hr_client.message_send = AsyncMock(return_value=result)

    tool_calls = [ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})]
    app, session = _build_app(tool_calls=tool_calls, hr_a2a_client=hr_client)

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "What is my leave balance?"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200

    # Give the background task a chance to run in the TestClient's event loop.
    # TestClient uses a real event loop under anyio; the task runs synchronously
    # within the test's scope because TestClient drains tasks on exit.
    events = _drain_queue(session)

    # Expected events: RoutingEvent + ChatMessageEvent (final).
    types_seen = [type(e).__name__ for e in events]
    assert "RoutingEvent" in types_seen
    chat_events = [e for e in events if isinstance(e, ChatMessageEvent)]
    assert len(chat_events) == 1
    assert "12" in chat_events[0].content or "leave" in chat_events[0].content.lower()


# ---------------------------------------------------------------------------
# Test 5 — single specialist with CIBA flow
# ---------------------------------------------------------------------------


def test_chat_single_specialist_ciba_flow() -> None:
    """ConsentRequiredPayload → CibaUrlEvent + VERIFYING → ResultPayload → DONE + ChatMessage."""
    consent = _consent_payload()
    result = _result_payload({"leave_days": 7})

    hr_client = MagicMock()
    hr_client.message_send = AsyncMock(return_value=consent)
    hr_client.await_completion = AsyncMock(return_value=result)

    tool_calls = [ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})]
    app, session = _build_app(tool_calls=tool_calls, hr_a2a_client=hr_client)

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "What is my leave?"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200

    events = _drain_queue(session)
    event_types = [type(e).__name__ for e in events]

    assert "RoutingEvent" in event_types
    assert "CibaUrlEvent" in event_types
    assert "CibaStateChangeEvent" in event_types

    # CibaUrlEvent must carry auth_url.
    ciba_url_events = [e for e in events if isinstance(e, CibaUrlEvent)]
    assert len(ciba_url_events) == 1
    assert ciba_url_events[0].auth_url == consent.auth_url

    # At least one CibaStateChange should be VERIFYING, one DONE.
    state_events = [e for e in events if isinstance(e, CibaStateChangeEvent)]
    states = {e.state for e in state_events}
    assert "VERIFYING" in states
    assert "DONE" in states

    # Final ChatMessageEvent must be present.
    chat_events = [e for e in events if isinstance(e, ChatMessageEvent)]
    assert len(chat_events) == 1


# ---------------------------------------------------------------------------
# Test 6 — two specialists, serial fan-out order
# ---------------------------------------------------------------------------


def test_chat_two_specialists_serial_order() -> None:
    """Both tool calls execute serially; routing and result events appear in order."""
    hr_result = _result_payload({"leave_days": 12})
    it_result = _result_payload({"assets": ["MBP-14", "XPS-13"]})

    hr_client = MagicMock()
    hr_client.message_send = AsyncMock(return_value=hr_result)

    it_client = MagicMock()
    it_client.message_send = AsyncMock(return_value=it_result)

    tool_calls = [
        ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={}),
        ToolCall(agent_id="it_agent", tool_id="it.list_available_assets", args={}),
    ]
    app, session = _build_app(
        tool_calls=tool_calls,
        hr_a2a_client=hr_client,
        it_a2a_client=it_client,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "leave and laptops"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200

    events = _drain_queue(session)
    routing_events = [e for e in events if isinstance(e, RoutingEvent)]

    # Both agents must be routed to, HR first then IT.
    assert len(routing_events) == 2
    assert routing_events[0].agent_id == "hr_agent"
    assert routing_events[1].agent_id == "it_agent"

    # Verify the HR routing event appears before IT routing event in the full list.
    all_types = [(type(e).__name__, getattr(e, "agent_id", None)) for e in events]
    hr_idx = next(i for i, (t, a) in enumerate(all_types) if t == "RoutingEvent" and a == "hr_agent")
    it_idx = next(i for i, (t, a) in enumerate(all_types) if t == "RoutingEvent" and a == "it_agent")
    assert hr_idx < it_idx

    # Final message contains both outputs concatenated.
    chat_events = [e for e in events if isinstance(e, ChatMessageEvent)]
    assert len(chat_events) == 1
    final_content = chat_events[0].content
    assert "12" in final_content
    assert "MBP-14" in final_content or "assets" in final_content.lower()


# ---------------------------------------------------------------------------
# Test 7 — mid-flow denial: first specialist OK, second denied (UC-04 EX-3)
# ---------------------------------------------------------------------------


def test_chat_mid_flow_denial_partial_result() -> None:
    """First specialist succeeds; second returns ErrorPayload(ERR-CIBA-005) → partial reply."""
    hr_result = _result_payload({"leave_days": 5})
    it_consent = _consent_payload("auth-req-it", "https://is.example.com/it-consent")
    it_denial = _error_payload("ERR-CIBA-005")

    hr_client = MagicMock()
    hr_client.message_send = AsyncMock(return_value=hr_result)

    it_client = MagicMock()
    it_client.message_send = AsyncMock(return_value=it_consent)
    it_client.await_completion = AsyncMock(return_value=it_denial)

    tool_calls = [
        ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={}),
        ToolCall(agent_id="it_agent", tool_id="it.list_available_assets", args={}),
    ]
    app, session = _build_app(
        tool_calls=tool_calls,
        hr_a2a_client=hr_client,
        it_a2a_client=it_client,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "leave and laptops"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200

    events = _drain_queue(session)

    # DENIED state change must be present.
    state_events = [e for e in events if isinstance(e, CibaStateChangeEvent)]
    states = {e.state for e in state_events}
    assert "DENIED" in states

    # Final ChatMessageEvent must mention the successful HR result AND the denial.
    chat_events = [e for e in events if isinstance(e, ChatMessageEvent)]
    assert len(chat_events) == 1
    content = chat_events[0].content
    # HR success info must be present.
    assert "5" in content  # leave_days
    # Denial message must also be present.
    assert len(content) > 20  # non-trivial combined message


# ---------------------------------------------------------------------------
# Test 8 — first specialist returns ErrorPayload → continues to second
# ---------------------------------------------------------------------------


def test_chat_first_specialist_error_continues_to_second() -> None:
    """If the first specialist returns an ErrorPayload, fan-out continues to second."""
    hr_error = _error_payload("ERR-AGENT-001")
    it_result = _result_payload({"assets": ["MBP-16"]})

    hr_client = MagicMock()
    hr_client.message_send = AsyncMock(return_value=hr_error)

    it_client = MagicMock()
    it_client.message_send = AsyncMock(return_value=it_result)

    tool_calls = [
        ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={}),
        ToolCall(agent_id="it_agent", tool_id="it.list_available_assets", args={}),
    ]
    app, session = _build_app(
        tool_calls=tool_calls,
        hr_a2a_client=hr_client,
        it_a2a_client=it_client,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "leave and laptops"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200

    events = _drain_queue(session)

    # Both routing events must have been emitted.
    routing_events = [e for e in events if isinstance(e, RoutingEvent)]
    agent_ids = [e.agent_id for e in routing_events]
    assert "hr_agent" in agent_ids
    assert "it_agent" in agent_ids

    # it_client.message_send must have been called (fan-out continued).
    it_client.message_send.assert_called_once()

    # Final chat message must mention IT result.
    chat_events = [e for e in events if isinstance(e, ChatMessageEvent)]
    assert len(chat_events) == 1
    assert "MBP-16" in chat_events[0].content or "assets" in chat_events[0].content.lower()


# ---------------------------------------------------------------------------
# Test 9 — ChatAck returned immediately (fan-out is async)
# ---------------------------------------------------------------------------


def test_chat_returns_ack_immediately() -> None:
    """POST /api/chat must return ChatAck without waiting for fan-out to complete."""
    import time

    # A2A client that would block forever if fan-out were synchronous.
    async def _slow_send(*_args: object, **_kwargs: object) -> ResultPayload:
        await asyncio.sleep(60)  # 60s — would fail the test if awaited synchronously
        return _result_payload()

    hr_client = MagicMock()
    hr_client.message_send = AsyncMock(side_effect=_slow_send)

    tool_calls = [ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})]
    app, session = _build_app(tool_calls=tool_calls, hr_a2a_client=hr_client)

    client = TestClient(app)

    start = time.monotonic()
    resp = client.post(
        "/api/chat",
        json={"message": "leave"},
        cookies={"orch_sid": session.session_id},
    )
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Must complete well under 2 seconds (the 60s sleep must NOT block the response).
    assert elapsed < 2.0


# ---------------------------------------------------------------------------
# Test 10 — /api/ciba/cancel known auth_req_id
# ---------------------------------------------------------------------------


def test_ciba_cancel_known_auth_req_id() -> None:
    """Cancelling a known auth_req_id calls A2AClient.cancel and sets cancel_event."""
    session = _make_session()

    # Register a PendingCIBA on the session.
    pending = PendingCIBA(
        auth_req_id="auth-req-cancel",
        agent_id="hr_agent",
        request_id="req-123",
        started_at=_utc_now(),
    )
    session.pending_ciba["auth-req-cancel"] = pending

    hr_client = MagicMock()
    hr_client.cancel = AsyncMock(return_value=CancelResponse(cancelled=True))

    app, session = _build_app(
        session=session,
        hr_a2a_client=hr_client,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/ciba/cancel",
        json={"auth_req_id": "auth-req-cancel"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] is True

    # A2AClient.cancel must have been called with the correct auth_req_id.
    hr_client.cancel.assert_called_once()
    call_args = hr_client.cancel.call_args
    assert "auth-req-cancel" in str(call_args)

    # The local cancel_event must have been set.
    assert pending.cancel_event.is_set()


# ---------------------------------------------------------------------------
# Test 11 — /api/ciba/cancel unknown auth_req_id
# ---------------------------------------------------------------------------


def test_ciba_cancel_unknown_returns_not_found() -> None:
    """Cancelling an unknown auth_req_id returns cancelled=False, reason=not_found."""
    session = _make_session()
    app, session = _build_app(session=session)

    client = TestClient(app)
    resp = client.post(
        "/api/ciba/cancel",
        json={"auth_req_id": "nonexistent-req-id"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] is False
    assert body["reason"] == "not_found"


# ---------------------------------------------------------------------------
# Test 12 — session.completed_ciba_log has IssuedTokenRecord after CIBA success
# ---------------------------------------------------------------------------


def test_completed_ciba_log_updated_after_success() -> None:
    """After a successful CIBA flow, session.completed_ciba_log has one IssuedTokenRecord."""
    consent = _consent_payload()
    result = ResultPayload(
        data={"leave_days": 9},
        token_jti="jti-unique-567",
        token_exp=9999999999,
        token_iat=1000000000,
    )

    hr_client = MagicMock()
    hr_client.message_send = AsyncMock(return_value=consent)
    hr_client.await_completion = AsyncMock(return_value=result)

    tool_calls = [ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})]
    app, session = _build_app(tool_calls=tool_calls, hr_a2a_client=hr_client)

    client = TestClient(app)
    client.post(
        "/api/chat",
        json={"message": "leave balance"},
        cookies={"orch_sid": session.session_id},
    )

    # Fan-out runs synchronously within TestClient's event loop.
    assert len(session.completed_ciba_log) == 1
    record = session.completed_ciba_log[0]
    assert record.jti == "jti-unique-567"
    assert record.agent_id == "hr_agent"
    assert record.auth_req_id == consent.auth_req_id
    assert record.exp == 9999999999
    assert record.iat == 1000000000


# ---------------------------------------------------------------------------
# Test 13 — agent_id not in registry → SseErrorEvent pushed; graceful continuation
# ---------------------------------------------------------------------------


def test_chat_missing_agent_card_pushes_error_event() -> None:
    """When agent_id is not in the registry, an SseErrorEvent is pushed; fan-out continues."""
    it_result = _result_payload({"assets": ["Dell-XPS"]})

    it_client = MagicMock()
    it_client.message_send = AsyncMock(return_value=it_result)

    # hr_agent is NOT in the registry (hr_card_present=False).
    tool_calls = [
        ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={}),
        ToolCall(agent_id="it_agent", tool_id="it.list_available_assets", args={}),
    ]
    app, session = _build_app(
        tool_calls=tool_calls,
        it_a2a_client=it_client,
        hr_card_present=False,   # HR card absent from registry
        hr_a2a_client=None,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "leave and laptops"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200

    events = _drain_queue(session)

    # SseErrorEvent must have been emitted for the missing hr_agent.
    error_events = [e for e in events if isinstance(e, SseErrorEvent)]
    assert len(error_events) >= 1

    # IT agent must still have been called (fan-out continued after error).
    it_client.message_send.assert_called_once()

    # Final ChatMessageEvent must reference IT result.
    chat_events = [e for e in events if isinstance(e, ChatMessageEvent)]
    assert len(chat_events) == 1
    assert "Dell-XPS" in chat_events[0].content or "assets" in chat_events[0].content.lower()


# ---------------------------------------------------------------------------
# Sprint 2B.1a — D2.1 deny UX polish: agent-aware copy for denial / expiry
# ---------------------------------------------------------------------------

_friendly_error = _routes_mod._friendly_error


def test_friendly_error_denied_with_agent_label() -> None:
    """ERR-CIBA-005..008 with agent_label produces agent-aware decline copy."""
    for err in ("ERR-CIBA-005", "ERR-CIBA-006", "ERR-CIBA-007", "ERR-CIBA-008"):
        msg = _friendly_error(err, "user_denied", agent_label="HR Agent")
        assert "HR Agent" in msg
        assert "declined" in msg.lower()
        assert "ask again" in msg.lower()


def test_friendly_error_expired_with_agent_label() -> None:
    """ERR-CIBA-009 with agent_label names the agent in the timeout copy."""
    msg = _friendly_error("ERR-CIBA-009", "expired", agent_label="IT Agent")
    assert "IT Agent" in msg
    assert "timed out" in msg.lower()
    assert "ask again" in msg.lower()


def test_friendly_error_falls_back_to_generic_without_label() -> None:
    """Without agent_label, denied/expired errors use the static map copy."""
    denied = _friendly_error("ERR-CIBA-005", "user_denied")
    expired = _friendly_error("ERR-CIBA-009", "expired")
    # The agent-aware sentence is *not* used.
    assert "Agent" not in denied  # no specific specialist named
    assert "Agent" not in expired


def test_friendly_error_non_consent_errors_unaffected_by_label() -> None:
    """Non-consent errors (MCP / AGENT) ignore agent_label and use static copy."""
    msg = _friendly_error(
        "ERR-MCP-003", "missing required scope", agent_label="HR Agent"
    )
    assert "permission" in msg.lower()
    # ERR-MCP-003 copy mentions admin, not the specialist label.
    assert "HR Agent" not in msg


def test_chat_mid_flow_denial_includes_agent_label_in_copy() -> None:
    """Mid-flow deny: final chat must mention IT Agent + the decline phrasing."""
    hr_result = _result_payload({"leave_days": 12})
    it_consent = _consent_payload("auth-req-it", "https://is.example.com/it")
    it_denial = _error_payload("ERR-CIBA-005")

    hr_client = MagicMock()
    hr_client.message_send = AsyncMock(return_value=hr_result)

    it_client = MagicMock()
    it_client.message_send = AsyncMock(return_value=it_consent)
    it_client.await_completion = AsyncMock(return_value=it_denial)

    tool_calls = [
        ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={}),
        ToolCall(agent_id="it_agent", tool_id="it.list_available_assets", args={}),
    ]
    app, session = _build_app(
        tool_calls=tool_calls,
        hr_a2a_client=hr_client,
        it_a2a_client=it_client,
    )

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "leave and laptops"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200

    events = _drain_queue(session)
    chat_events = [e for e in events if isinstance(e, ChatMessageEvent)]
    assert len(chat_events) == 1
    content = chat_events[0].content

    # HR's success fragment must be present (12 days of leave).
    assert "12" in content
    # IT denial fragment must name IT Agent and include "declined".
    assert "IT Agent" in content
    assert "declined" in content.lower()


def test_chat_auth_req_id_expiry_emits_expired_state_and_friendly_copy() -> None:
    """auth_req_id expired before approval (ERR-CIBA-009) →
    CibaStateChangeEvent with state=EXPIRED is published and the final
    ChatMessage uses the agent-aware timeout copy (D2.3 / UC-05 N20)."""
    consent = _consent_payload("auth-req-expire", "https://is.example.com/x")
    expired_err = _error_payload("ERR-CIBA-009")

    hr_client = MagicMock()
    hr_client.message_send = AsyncMock(return_value=consent)
    hr_client.await_completion = AsyncMock(return_value=expired_err)

    tool_calls = [ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})]
    app, session = _build_app(tool_calls=tool_calls, hr_a2a_client=hr_client)

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "leave"},
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200

    events = _drain_queue(session)

    state_events = [e for e in events if isinstance(e, CibaStateChangeEvent)]
    states = {e.state for e in state_events}
    assert "EXPIRED" in states

    chat_events = [e for e in events if isinstance(e, ChatMessageEvent)]
    assert len(chat_events) == 1
    content = chat_events[0].content
    assert "HR Agent" in content
    assert "timed out" in content.lower()
