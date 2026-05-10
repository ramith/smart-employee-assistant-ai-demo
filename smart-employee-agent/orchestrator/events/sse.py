"""orchestrator/events/sse.py — SSE event-stream emitter.

Owns the publish-side of the Server-Sent Events push path.
The route handler at ``GET /events/{session_id}`` (Wave 7) consumes this module
to serialize events from a session's ``asyncio.Queue`` over HTTP.

Wire format (per SSE spec, RFC 8895):
    data: <json>\\n\\n

F-13 (sprint-1-fixes.md) locks the vocabulary to six event types:
    session_ready, routing, ciba_url, ciba_state_change, chat_message, error

F-06 (sprint-1-fixes.md) mandates that the /events/{session_id} route handler
assert ``path_session_id == cookie.orch_sid`` before calling :class:`SseChannel`.
That auth check lives in the Wave-7 router, NOT here.

Boundary rule (F-09): all six event classes are Pydantic ``BaseModel`` subclasses
because they cross the HTTP/SSE boundary. ``SseChannel`` itself holds an
``asyncio.Queue`` (a non-serialisable runtime object) so it is a plain class, not
a Pydantic model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Event models (F-13 — locked 6-event vocabulary)
# ---------------------------------------------------------------------------


class SessionReadyEvent(BaseModel):
    """Emitted when the SSE stream is first connected.

    Lets the SPA confirm the server side is alive and display the user's name.
    """

    type: Literal["session_ready"] = "session_ready"
    user_label: str
    server_time: datetime


class RoutingEvent(BaseModel):
    """Emitted when the orchestrator has chosen a specialist and is sending the A2A request.

    Allows the SPA to show a "Routing to HR Agent…" indicator before the consent
    widget appears.  ``request_id`` ties back to the originating ``POST /api/chat``.

    ``tool_index`` is 0-based; ``total_tools`` is the count of routed agents
    in this fan-out. Together they let the SPA render natural copy:
        - 1 of 1 → "Routing to HR Agent…"
        - 1 of 2 → "Routing to HR Agent first…"
        - 2 of 2 → "Now routing to IT Agent…"
    """

    type: Literal["routing"] = "routing"
    request_id: str
    agent_id: str
    agent_label: str
    tool_index: int = 0
    total_tools: int = 1


class CibaUrlEvent(BaseModel):
    """Emitted when a specialist has initiated CIBA and returned ``auth_url``.

    The SPA renders the Consent Widget from this payload.

    ``is_refresh`` is ``True`` when the CIBA is a token-expiry re-auth (UC-06);
    the SPA should label the widget "Re-authorizing…" instead of "Authorizing…".

    ``prior_consent_at`` is set on re-auth so the widget can show
    "You approved this 47 minutes ago."
    """

    type: Literal["ciba_url"] = "ciba_url"
    request_id: str
    agent_id: str
    agent_label: str
    action: str
    auth_url: str
    binding_code: str
    expires_in: int
    scope: str
    is_refresh: bool = False
    prior_consent_at: datetime | None = None


class CibaStateChangeEvent(BaseModel):
    """Emitted by the orchestrator as CIBA polling progresses.

    Maps to the Consent Widget visual states documented in ``error-catalog.md``:

    * ``VERIFYING`` — user clicked Approve; IS confirmed; polling in progress.
    * ``WORKING``   — token received; MCP call in progress.
    * ``DONE``      — MCP returned; result is in the subsequent ``chat_message``.
    * ``DENIED``    — user clicked Deny at IS (ERR-CIBA-005..008).
    * ``EXPIRED``   — ``auth_req_id`` timed out (ERR-CIBA-009).
    * ``ERROR``     — unrecoverable (ERR-CIBA-001..004, ERR-AGENT-*, ERR-MCP-*).
    """

    type: Literal["ciba_state_change"] = "ciba_state_change"
    request_id: str
    state: Literal["VERIFYING", "WORKING", "DONE", "DENIED", "EXPIRED", "ERROR"]
    message: str | None = None


class ChatMessageEvent(BaseModel):
    """The orchestrator's LLM-composed reply.

    Always ``role="assistant"``.  ``request_id`` links it back to the originating
    ``POST /api/chat`` call.
    """

    type: Literal["chat_message"] = "chat_message"
    role: Literal["assistant"] = "assistant"
    content: str
    request_id: str


class SseErrorEvent(BaseModel):
    """Unrecoverable or session-level error.

    ``request_id`` is ``None`` for session-level errors (e.g. ERR-AUTH-009,
    ERR-INFRA-004) that occur outside a request context.
    """

    type: Literal["error"] = "error"
    error_id: str
    message: str
    request_id: str | None = None


class SessionTerminatedEvent(BaseModel):
    """3B.1: emitted when the orchestrator drops a session out-of-band.

    Pushed in two cases:

    * ``reason="admin_terminated"`` — IS Console terminate fired BCL,
      orchestrator ran the cascade. The SPA should land on
      ``/?reason=admin_terminated`` so the user sees a banner explaining
      why their tab is no longer signed in.
    * ``reason="user_signed_out"`` — multi-browser case. Tab A initiates
      sign-out; tab B (same user_sub) gets this push so it doesn't keep
      stale state.

    BLOCK-H: this event must be emitted BEFORE ``Session`` removal so the
    SPA's still-open SSE stream picks it up. The cascade enforces
    emit-then-delete ordering in ``logout_handler._execute_locked``.
    """

    type: Literal["session_terminated"] = "session_terminated"
    reason: Literal["admin_terminated", "user_signed_out"]
    request_id: str


# Discriminated union over all event types (F-13 + 3B.1 session_terminated).
SseEvent = Annotated[
    Union[
        SessionReadyEvent,
        RoutingEvent,
        CibaUrlEvent,
        CibaStateChangeEvent,
        ChatMessageEvent,
        SseErrorEvent,
        SessionTerminatedEvent,
    ],
    Field(discriminator="type"),
]

# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------

_KEEPALIVE_BYTES: bytes = b": keep-alive\n\n"


def format_sse(event: SseEvent) -> bytes:
    """Serialize a Pydantic event to the SSE wire format.

    Produces ``data: <json>\\n\\n`` encoded as UTF-8 bytes, ready to be written
    directly to the HTTP response stream.

    The JSON payload uses Pydantic v2's ``model_dump_json`` so that field
    aliases, validators, and ``datetime`` serialization are all respected.

    Args:
        event: Any of the six :data:`SseEvent` variants.

    Returns:
        UTF-8 encoded bytes in the form ``b'data: {...}\\n\\n'``.
    """
    json_str: str = event.model_dump_json()
    return f"data: {json_str}\n\n".encode()


def keepalive_comment() -> bytes:
    """Return the SSE keep-alive comment bytes.

    The comment line ```: keep-alive\\n\\n``` is emitted every 15 s by
    :meth:`SseChannel.stream` to keep the TCP connection open and allow the SPA's
    ``EventSource`` to detect drops via its built-in timeout mechanism.

    Returns:
        ``b': keep-alive\\n\\n'``
    """
    return _KEEPALIVE_BYTES


# ---------------------------------------------------------------------------
# SseChannel
# ---------------------------------------------------------------------------


class SseChannel:
    """Wraps a session's ``asyncio.Queue`` for fan-in event publishing.

    The orchestrator's application code calls :meth:`publish` when it has news
    for the SPA.  The ``GET /events/{session_id}`` route handler (Wave 7) iterates
    :meth:`stream` to push formatted bytes to the HTTP response.

    Lifecycle
    ---------
    1. The route handler creates (or retrieves) an ``asyncio.Queue`` for the
       session and constructs an ``SseChannel`` from it.
    2. The route handler ``async for chunk in channel.stream():`` and writes each
       chunk to the ``StreamingResponse``.
    3. When the orchestrator is done (or the client disconnects), ``close()`` is
       called to signal the stream generator to stop.

    Sentinel protocol
    -----------------
    :meth:`close` publishes ``None`` to the queue.  :meth:`stream` exits when it
    dequeues ``None``.  Calling :meth:`close` multiple times is safe (idempotent).

    Thread / task safety
    --------------------
    ``asyncio.Queue`` is not thread-safe.  All callers must run in the same event
    loop.  The queue's ``maxsize=0`` (unbounded) means :meth:`publish` never
    blocks under normal load.
    """

    def __init__(self, queue: asyncio.Queue[SseEvent | None]) -> None:
        """Wrap *queue* for publish/stream access.

        Args:
            queue: An ``asyncio.Queue`` shared between the publish side
                (orchestrator logic) and the consume side (SSE route handler).
                The type parameter is ``SseEvent | None``; ``None`` is the
                close sentinel.
        """
        self._queue: asyncio.Queue[SseEvent | None] = queue
        self._closed: bool = False

    async def publish(self, event: SseEvent) -> None:
        """Put *event* on the queue.

        Non-blocking under normal load (the queue has no size cap).  The
        ``StreamingResponse`` consumer will dequeue it on the next iteration of
        :meth:`stream`.

        Args:
            event: Any of the six :data:`SseEvent` variants.
        """
        await self._queue.put(event)

    async def stream(
        self, *, keepalive_seconds: float = 15.0
    ) -> AsyncIterator[bytes]:
        """Yield SSE-formatted bytes from the queue.

        Yields keepalive comments (``b': keep-alive\\n\\n'``) every
        *keepalive_seconds* seconds when no events are available, so that TCP
        middleboxes and the browser's ``EventSource`` do not consider the
        connection stale.

        The generator stops when:

        * :meth:`close` has been called (``None`` sentinel dequeued), or
        * the consuming coroutine is cancelled (``asyncio.CancelledError``
          propagates naturally — do **not** catch it here).

        Args:
            keepalive_seconds: Interval between keepalive comment emissions when
                the queue is idle.  Defaults to 15 s.

        Yields:
            Raw UTF-8 bytes — either a ``b'data: {...}\\n\\n'`` event frame or
            the ``b': keep-alive\\n\\n'`` comment.
        """
        while True:
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=keepalive_seconds
                )
            except asyncio.TimeoutError:
                yield keepalive_comment()
                continue

            if item is None:
                # Close sentinel — stop the generator.
                return

            yield format_sse(item)

    async def close(self) -> None:
        """Signal :meth:`stream` to terminate.

        Publishes a ``None`` sentinel to the queue.  Calling this method more
        than once is safe; subsequent calls are no-ops.
        """
        if not self._closed:
            self._closed = True
            await self._queue.put(None)
