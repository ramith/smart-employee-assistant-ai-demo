"""orchestrator/events/sse_router.py — FastAPI router for the SSE event stream.

Exposes ``GET /events/{session_id}`` as a ``text/event-stream`` endpoint.

Security contract (F-06, sprint-1-fixes.md)
-------------------------------------------
The path parameter ``session_id`` MUST exactly match the ``orch_sid`` cookie.
Any mismatch — including a missing cookie — is rejected *before* the session
store is consulted:

* Cookie absent  → 401  ``{"error_id": "ERR-AUTH-001", "request_id": ...}``
* Cookie != path → 403  ``{"error_id": "ERR-AUTH-009", "request_id": ...,
                           "message": "Cross-session SSE subscription attempt"}``

Flow after auth passes
-----------------------
1. ``SessionStore.get_or_404(session_id)`` — raises ``KeyError`` → 404.
2. ``session.touch()`` is called by ``get_or_404`` already; no extra call needed.
3. A ``SseChannel`` is constructed around ``session.sse_queue``.
4. A ``SessionReadyEvent`` is published to the channel so the SPA receives an
   immediate confirmation that the stream is alive.
5. ``StreamingResponse(channel.stream(...))`` is returned with the mandatory
   ``Cache-Control: no-cache`` and ``X-Accel-Buffering: no`` headers.

The generator runs until:
* The consumer (SPA / browser) closes the TCP connection — ``asyncio.CancelledError``
  propagates out of ``channel.stream()`` and the generator terminates naturally.
* ``channel.close()`` is called from elsewhere (delivers ``None`` sentinel).

Boundary rule (F-09)
--------------------
``SseRouterDeps`` holds a ``SessionStore`` (non-serialisable runtime object) so
it is a ``@dataclass``, not a Pydantic ``BaseModel``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from common.logging.correlation import get_request_id
from orchestrator.auth.session_store import SessionStore
from orchestrator.events.sse import SessionReadyEvent, SseChannel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency container (F-09: dataclass, not Pydantic)
# ---------------------------------------------------------------------------


@dataclass
class SseRouterDeps:
    """Dependencies injected into the SSE router at construction time.

    Attributes:
        session_store: The orchestrator's in-memory session store.
        keepalive_seconds: Interval between keep-alive comment emissions on
            idle streams.  Defaults to 15 s.
    """

    session_store: SessionStore
    keepalive_seconds: float = 15.0


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_sse_router(deps: SseRouterDeps) -> APIRouter:
    """Return a FastAPI ``APIRouter`` with the SSE endpoint registered.

    The returned router mounts ``GET /events/{session_id}`` and enforces all
    auth checks documented in F-06 of ``sprint-1-fixes.md``.

    Args:
        deps: Injected ``SseRouterDeps`` carrying the session store and
            keepalive interval.

    Returns:
        A ``fastapi.APIRouter`` ready to be included in the application with::

            app.include_router(build_sse_router(deps))
    """
    router = APIRouter()

    @router.get("/events/{session_id}")
    async def stream_events(
        session_id: str,
        request: Request,
    ) -> StreamingResponse:
        """SSE endpoint — pushes events to the SPA for *session_id*.

        Auth: ``orch_sid`` cookie must be present and equal *session_id*.

        Args:
            session_id: The path parameter identifying the session.
            request: The incoming FastAPI/Starlette request (for cookie access).

        Returns:
            A ``StreamingResponse`` of ``text/event-stream`` content.

        Raises:
            HTTPException 401: When the ``orch_sid`` cookie is absent.
            HTTPException 403: When the cookie is present but does not match
                *session_id* (F-06 cross-session subscription guard).
            HTTPException 404: When *session_id* is not found in the store.
        """
        from fastapi import HTTPException  # local import keeps module-level clean

        request_id = get_request_id()

        # ── F-06 Step 1: cookie presence check ──────────────────────────────
        cookie_sid: str | None = request.cookies.get("orch_sid")
        if not cookie_sid:
            logger.warning(
                "[SSE] missing orch_sid cookie for session_id=%s request_id=%s",
                session_id,
                request_id,
            )
            raise HTTPException(
                status_code=401,
                detail={"error_id": "ERR-AUTH-001", "request_id": request_id},
            )

        # ── F-06 Step 2: cross-session mismatch check ────────────────────────
        if cookie_sid != session_id:
            logger.warning(
                "[SSE] ERR-AUTH-009 cross-session attempt cookie=%s path=%s request_id=%s",
                cookie_sid,
                session_id,
                request_id,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error_id": "ERR-AUTH-009",
                    "request_id": request_id,
                    "message": "Cross-session SSE subscription attempt",
                },
            )

        # ── Step 3: session lookup (404 on miss) ─────────────────────────────
        try:
            session = await deps.session_store.get_or_404(session_id)
        except KeyError:
            logger.warning(
                "[SSE] session not found session_id=%s request_id=%s",
                session_id,
                request_id,
            )
            raise HTTPException(status_code=404, detail="Session not found")

        # ── Steps 4-7: channel setup, initial event, streaming response ───────
        # get_or_404 already calls touch() internally.
        channel = SseChannel(session.sse_queue)

        # Publish the SessionReadyEvent so the SPA receives it as the first chunk.
        now_utc: datetime = datetime.now(tz=timezone.utc)
        await channel.publish(
            SessionReadyEvent(user_label=session.user_label, server_time=now_utc)
        )

        logger.info(
            "[SSE] stream opened session_id=%s user_label=%s request_id=%s",
            session_id,
            session.user_label,
            request_id,
        )

        return StreamingResponse(
            channel.stream(keepalive_seconds=deps.keepalive_seconds),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
