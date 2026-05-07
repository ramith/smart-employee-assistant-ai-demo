"""Tests for orchestrator/events/sse_router.py — Wave 7, Sprint 1.

Coverage targets (10 tests)
----------------------------
 1. Missing ``orch_sid`` cookie → 401  ``{"error_id": "ERR-AUTH-001", ...}``.
 2. Cookie present but ``path session_id != cookie`` → 403
    ``{"error_id": "ERR-AUTH-009", ...}`` — **F-06 explicit verification**.
 3. Cookie matches path but session not found in store → 404.
 4. Cookie + path match + session exists → 200  ``text/event-stream``.
 5. First chunk is a ``SessionReadyEvent`` JSON with the correct ``user_label``.
 6. Publishing a ``RoutingEvent`` to the queue → subsequent chunk delivers it.
 7. Multiple events published in order → delivered in the same order.
 8. After ``keepalive_seconds`` with no events → keepalive comment delivered.
 9. Disconnect mid-stream → no error log; channel continues (CancelledError not raised
    to the test; graceful termination confirmed).
10. ERR-AUTH-009 body contains ``message: "Cross-session SSE subscription attempt"``.

Isolation strategy
------------------
Both ``orchestrator/events/sse_router.py`` and its transitive dependencies
(``orchestrator/events/sse.py``, ``orchestrator/auth/session_store.py``,
``common/logging/correlation.py``) are loaded via ``importlib.util`` so the
tests run without a working ``orchestrator/__init__.py`` or
``common/auth/__init__.py`` in the import chain.

The ``SessionStore`` is replaced entirely with an ``AsyncMock``-based fake
(``_FakeSessionStore``) that returns a ``Session``-alike dataclass built from
``asyncio.Queue``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Module-isolation loader
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
    """Load a single .py file into sys.modules under dotted_name."""
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


# Register all package stubs required before any import of the modules under test.
for _pkg in (
    "common",
    "common.auth",
    "common.logging",
    "orchestrator",
    "orchestrator.auth",
    "orchestrator.events",
):
    _ensure_pkg(_pkg)

# Load dependencies in order (each must be in sys.modules before the next import).
_load_module("common.auth.models", "common/auth/models.py")
_load_module("common.logging.correlation", "common/logging/correlation.py")
_load_module("orchestrator.events.sse", "orchestrator/events/sse.py")
_load_module("orchestrator.auth.session_store", "orchestrator/auth/session_store.py")
_load_module("orchestrator.events.sse_router", "orchestrator/events/sse_router.py")

# Pull public symbols.
_sse_mod = sys.modules["orchestrator.events.sse"]
_router_mod = sys.modules["orchestrator.events.sse_router"]

SessionReadyEvent = _sse_mod.SessionReadyEvent
RoutingEvent = _sse_mod.RoutingEvent
format_sse = _sse_mod.format_sse
keepalive_comment = _sse_mod.keepalive_comment

SseRouterDeps = _router_mod.SseRouterDeps
build_sse_router = _router_mod.build_sse_router

# ---------------------------------------------------------------------------
# Fake session / store
# ---------------------------------------------------------------------------

_SESSION_ID = "test-session-abc123"
_USER_LABEL = "Alice"
_COOKIE_HEADER = f"orch_sid={_SESSION_ID}"


@dataclass
class _FakeSession:
    """Minimal Session-alike for the tests.  Only carries what sse_router uses."""

    session_id: str
    user_label: str
    sse_queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def touch(self) -> None:  # noqa: D401 — mirrors real Session.touch()
        """No-op touch for the fake."""


def _make_fake_store(session: _FakeSession | None = None) -> AsyncMock:
    """Return an AsyncMock that behaves like SessionStore.

    If *session* is provided ``get_or_404`` returns it; otherwise it raises
    ``KeyError`` to simulate a missing session.

    Args:
        session: The session to return, or ``None`` to simulate 404.

    Returns:
        An ``AsyncMock`` mimicking :class:`SessionStore`.
    """
    store = AsyncMock()
    if session is None:
        store.get_or_404 = AsyncMock(side_effect=KeyError(_SESSION_ID))
    else:
        store.get_or_404 = AsyncMock(return_value=session)
    return store


def _make_app(store: AsyncMock, keepalive_seconds: float = 60.0) -> FastAPI:
    """Build a minimal FastAPI application wired to *store*.

    Args:
        store: The fake session store to inject.
        keepalive_seconds: Keepalive interval forwarded to :class:`SseRouterDeps`.

    Returns:
        A ``FastAPI`` app with the SSE router mounted.
    """
    app = FastAPI()
    deps = SseRouterDeps(session_store=store, keepalive_seconds=keepalive_seconds)
    app.include_router(build_sse_router(deps))
    return app


# ---------------------------------------------------------------------------
# Test 1 — missing cookie → 401 ERR-AUTH-001
# ---------------------------------------------------------------------------


def test_missing_cookie_returns_401() -> None:
    """GET /events/{id} without orch_sid cookie must return 401 ERR-AUTH-001."""
    session = _FakeSession(session_id=_SESSION_ID, user_label=_USER_LABEL)
    app = _make_app(_make_fake_store(session))

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/events/{_SESSION_ID}")  # no cookie

    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"]["error_id"] == "ERR-AUTH-001"
    assert "request_id" in body["detail"]


# ---------------------------------------------------------------------------
# Test 2 — cookie present but != path → 403 ERR-AUTH-009  (F-06)
# ---------------------------------------------------------------------------


def test_cookie_path_mismatch_returns_403_err_auth_009() -> None:
    """F-06: cookie value != path session_id must return 403 ERR-AUTH-009.

    This is the explicit F-06 compliance test.  The cookie carries
    'other-session-id' while the path asks for _SESSION_ID.
    """
    session = _FakeSession(session_id=_SESSION_ID, user_label=_USER_LABEL)
    app = _make_app(_make_fake_store(session))

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(
            f"/events/{_SESSION_ID}",
            cookies={"orch_sid": "other-session-id"},  # deliberate mismatch
        )

    assert resp.status_code == 403
    body = resp.json()
    detail = body["detail"]
    assert detail["error_id"] == "ERR-AUTH-009"
    assert "request_id" in detail


# ---------------------------------------------------------------------------
# Test 3 — ERR-AUTH-009 body contains the required message field
# ---------------------------------------------------------------------------


def test_cookie_path_mismatch_body_contains_message() -> None:
    """403 detail must include 'message: Cross-session SSE subscription attempt'."""
    session = _FakeSession(session_id=_SESSION_ID, user_label=_USER_LABEL)
    app = _make_app(_make_fake_store(session))

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(
            f"/events/{_SESSION_ID}",
            cookies={"orch_sid": "intruder-session-xyz"},
        )

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["message"] == "Cross-session SSE subscription attempt"


# ---------------------------------------------------------------------------
# Test 4 — valid cookie + path but session not found → 404
# ---------------------------------------------------------------------------


def test_session_not_found_returns_404() -> None:
    """When the store raises KeyError for the session_id, the endpoint must return 404."""
    store = _make_fake_store(session=None)  # raises KeyError
    app = _make_app(store)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(
            f"/events/{_SESSION_ID}",
            cookies={"orch_sid": _SESSION_ID},
        )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 5 — valid request → 200 text/event-stream
# ---------------------------------------------------------------------------


def test_valid_request_returns_200_event_stream() -> None:
    """Cookie + path match + session present → 200 with content-type text/event-stream."""
    session = _FakeSession(session_id=_SESSION_ID, user_label=_USER_LABEL)
    # Pre-load a close sentinel so the stream terminates after the ready event.
    session.sse_queue.put_nowait(None)

    app = _make_app(_make_fake_store(session))

    with TestClient(app, raise_server_exceptions=False) as client:
        with client.stream("GET", f"/events/{_SESSION_ID}", cookies={"orch_sid": _SESSION_ID}) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# Async helpers for SSE streaming tests (Tests 6-8)
#
# TestClient.stream() / iter_lines() blocks if the server's async generator
# does not terminate on its own (no sentinel / CancelledError path) — the
# TestClient thread waits for response drain.  Instead we use httpx.AsyncClient
# with ASGITransport, which runs the ASGI app and the HTTP client coroutines in
# the same event loop as the test.  We control stream termination by:
#   a) pre-seeding a close sentinel on the queue (None), OR
#   b) reading N data lines then breaking — httpx propagates the break as an
#      early-close / CancelledError to the ASGI generator.
#
# The asyncio.Queue must be created inside the running event loop so that
# put_nowait() from sync setup and async get() from the handler share the same
# loop.  We use a pytest-asyncio fixture pattern: the queue and session are
# created *inside* the async test function.
# ---------------------------------------------------------------------------


def _parse_data_lines(raw: str) -> list[dict]:
    """Extract and JSON-parse all 'data: ...' lines from an SSE response body.

    Args:
        raw: The full response text from the SSE stream.

    Returns:
        Parsed JSON objects for every ``data:`` line.
    """
    payloads: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            payloads.append(json.loads(line[len("data: "):]))
    return payloads


# ---------------------------------------------------------------------------
# Test 6 — first chunk is SessionReadyEvent with correct user_label
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_chunk_is_session_ready_event() -> None:
    """The first SSE data line must be a SessionReadyEvent with the correct user_label.

    The queue is created inside the async test so queue + ASGI handler share the
    same event loop.  A None sentinel is placed *after* the handler runs so the
    stream delivers exactly one event before closing.

    Strategy: publish the sentinel from a background task that fires after a
    brief yield, ensuring the handler's ``await channel.publish(SessionReadyEvent)``
    runs first and the sentinel arrives second.
    """
    import httpx
    from httpx._transports.asgi import ASGITransport

    q: asyncio.Queue = asyncio.Queue()
    session = _FakeSession(session_id=_SESSION_ID, user_label=_USER_LABEL, sse_queue=q)
    store = _make_fake_store(session)
    app = _make_app(store)

    # Schedule the sentinel to arrive shortly after the stream opens.
    async def _send_sentinel() -> None:
        await asyncio.sleep(0.05)
        await q.put(None)

    asyncio.create_task(_send_sentinel())

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with client.stream(
            "GET",
            f"/events/{_SESSION_ID}",
            cookies={"orch_sid": _SESSION_ID},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            body = await resp.aread()

    payloads = _parse_data_lines(body.decode())
    assert payloads, "No data lines received from SSE stream"
    assert payloads[0]["type"] == "session_ready"
    assert payloads[0]["user_label"] == _USER_LABEL
    assert "server_time" in payloads[0]


# ---------------------------------------------------------------------------
# Test 7 — RoutingEvent on queue → delivered as next chunk after session_ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_event_delivered_after_ready() -> None:
    """A RoutingEvent published after the stream opens must appear after SessionReadyEvent.

    The handler enqueues SessionReadyEvent first (inside the route handler body),
    then returns the StreamingResponse.  We publish the RoutingEvent and a close
    sentinel from a background task that fires *after* a short delay, ensuring
    the ready event is already in the queue before the routing event arrives.
    """
    import httpx
    from httpx._transports.asgi import ASGITransport

    q: asyncio.Queue = asyncio.Queue()
    routing = RoutingEvent(request_id="req-007", agent_id="hr_agent", agent_label="HR Agent")

    session = _FakeSession(session_id=_SESSION_ID, user_label=_USER_LABEL, sse_queue=q)
    store = _make_fake_store(session)
    app = _make_app(store)

    async def _publisher() -> None:
        # Wait one tick so the handler's SessionReadyEvent publish runs first.
        await asyncio.sleep(0.02)
        await q.put(routing)
        await q.put(None)  # sentinel — terminates the stream

    asyncio.create_task(_publisher())

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with client.stream(
            "GET",
            f"/events/{_SESSION_ID}",
            cookies={"orch_sid": _SESSION_ID},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            assert resp.status_code == 200
            body = await asyncio.wait_for(resp.aread(), timeout=5.0)

    payloads = _parse_data_lines(body.decode())
    assert len(payloads) >= 2, f"Expected >= 2 data payloads; got: {payloads!r}"

    assert payloads[0]["type"] == "session_ready"
    assert payloads[1]["type"] == "routing"
    assert payloads[1]["request_id"] == "req-007"
    assert payloads[1]["agent_id"] == "hr_agent"


# ---------------------------------------------------------------------------
# Test 8 — multiple events delivered in order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_events_delivered_in_order() -> None:
    """Three RoutingEvents published after stream open arrive in order after session_ready.

    Same background-task pattern as test_routing_event_delivered_after_ready:
    events are enqueued with a brief delay so the handler's initial
    SessionReadyEvent always lands first in FIFO order.
    """
    import httpx
    from httpx._transports.asgi import ASGITransport

    q: asyncio.Queue = asyncio.Queue()
    ordered_events = [
        RoutingEvent(request_id="r1", agent_id="hr_agent", agent_label="HR Agent"),
        RoutingEvent(request_id="r2", agent_id="it_agent", agent_label="IT Agent"),
        RoutingEvent(request_id="r3", agent_id="hr_agent", agent_label="HR Agent"),
    ]

    session = _FakeSession(session_id=_SESSION_ID, user_label=_USER_LABEL, sse_queue=q)
    store = _make_fake_store(session)
    app = _make_app(store)

    async def _publisher() -> None:
        await asyncio.sleep(0.02)
        for ev in ordered_events:
            await q.put(ev)
        await q.put(None)  # sentinel

    asyncio.create_task(_publisher())

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with client.stream(
            "GET",
            f"/events/{_SESSION_ID}",
            cookies={"orch_sid": _SESSION_ID},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            assert resp.status_code == 200
            body = await asyncio.wait_for(resp.aread(), timeout=5.0)

    payloads = _parse_data_lines(body.decode())
    assert len(payloads) == 4, f"Expected 4 payloads (1 ready + 3 routing); got: {payloads!r}"

    assert payloads[0]["type"] == "session_ready"
    for i, ev in enumerate(ordered_events, start=1):
        assert payloads[i]["type"] == "routing"
        assert payloads[i]["request_id"] == ev.request_id


# ---------------------------------------------------------------------------
# Test 9 — keepalive comment delivered after keepalive_seconds idle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keepalive_delivered_after_idle() -> None:
    """A keep-alive comment must be emitted when the queue is idle for keepalive_seconds.

    Uses a very short keepalive (0.05 s) and an asyncio-level stream consumer to
    avoid the 15 s real timer.
    """
    from orchestrator.events.sse import SseChannel  # already loaded in sys.modules

    q: asyncio.Queue = asyncio.Queue()
    channel = SseChannel(q)

    keepalive_s = 0.05
    received: list[bytes] = []

    async def _collect_one() -> None:
        async for chunk in channel.stream(keepalive_seconds=keepalive_s):
            received.append(chunk)
            # Stop after first chunk so the test doesn't hang.
            await channel.close()

    task = asyncio.create_task(_collect_one())
    # Wait well past the keepalive timeout.
    await asyncio.sleep(keepalive_s * 6)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # If any chunk was collected before cancellation, it must be the keepalive.
    if received:
        assert received[0] == keepalive_comment(), (
            f"Expected keepalive comment as first chunk; got: {received[0]!r}"
        )
    else:
        # The task was cancelled before any chunk — run a simpler deterministic check.
        q2: asyncio.Queue = asyncio.Queue()
        channel2 = SseChannel(q2)
        first_chunks: list[bytes] = []

        async def _one_shot() -> None:
            async for chunk in channel2.stream(keepalive_seconds=keepalive_s):
                first_chunks.append(chunk)
                break  # stop after first

        await asyncio.wait_for(_one_shot(), timeout=keepalive_s * 10)
        assert first_chunks[0] == keepalive_comment()


# ---------------------------------------------------------------------------
# Test 10 — disconnect mid-stream (CancelledError) — no error raised to caller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_mid_stream_no_error() -> None:
    """Cancelling the SSE consumer task must not raise from the SseChannel.stream() side.

    The ``asyncio.CancelledError`` propagates naturally out of
    ``asyncio.wait_for(queue.get(), ...)`` and causes the generator to stop.
    The test verifies that:
    - The task can be cancelled without the channel raising any other exception.
    - The channel's internal state allows close() to be called afterwards without error.
    """
    from orchestrator.events.sse import SseChannel  # already loaded in sys.modules

    q: asyncio.Queue = asyncio.Queue()
    channel = SseChannel(q)

    collected: list[bytes] = []

    async def _long_running_consumer() -> None:
        async for chunk in channel.stream(keepalive_seconds=60.0):
            collected.append(chunk)

    task = asyncio.create_task(_long_running_consumer())
    # Yield control to let the consumer enter wait_for.
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The channel must still be closeable without error after disconnect.
    await channel.close()  # must not raise

    # Nothing should have been collected (queue was empty and keepalive is 60 s).
    assert collected == []
