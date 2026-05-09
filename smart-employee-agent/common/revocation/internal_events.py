"""common.revocation.internal_events — shared /internal/events receiver.

Sprint 3 3A.2 deliverable. One implementation, used by all 4 receivers
(HR-AGENT, IT-AGENT, hr_server, it_server) so the wire shape and auth
check is single-source.

Per sprint-3-tech-arch.md §3.2 (Stage 4-locked):

    POST /internal/events
      X-Internal-Auth: <INTERNAL_REVOKE_SHARED_SECRET>   (NIT-1: dedicated header)
      X-Request-ID: <rid>
      Content-Type: application/json
      Body: {
        "type": "session-revoked",
        "subject": {"sub": "<user uuid>", "jti": "<jti>"},
        "exp": 1746825600,
        "reason": "user_signed_out" | "admin_terminated"
      }

    200 OK { "acked": true }
    401 Unauthorized { "error": "invalid_secret" }

Auth model is BLOCK-B (simple) — static shared secret in env, set at
``docker compose up`` time. Production roadmap = OAuth client_credentials
with scope ``revoke:jti`` or mTLS.

The receiver is service-specific in only ONE way: what it does with the
jti after adding it to the denylist. Some services drop a cached token
(agents); others just record (servers). The router accepts an
``on_revoke`` callback that the wiring layer provides.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from common.revocation.jti_denylist import RevocationState

logger = logging.getLogger(__name__)

__all__ = ["build_internal_events_router", "InternalEventBody"]


class _Subject(BaseModel):
    sub: str
    jti: str


class InternalEventBody(BaseModel):
    """Body for ``POST /internal/events``."""

    type: str = Field(..., description="Event type — currently only 'session-revoked'.")
    subject: _Subject
    exp: float = Field(..., description="JWT exp claim as Unix epoch seconds.")
    reason: str = Field(default="user_signed_out")


class _AckResponse(BaseModel):
    acked: bool = True
    note: str | None = None


def build_internal_events_router(
    *,
    state: RevocationState,
    shared_secret: str,
    on_revoke: Callable[[str, str, float, str], Awaitable[None]] | None = None,
    service_label: str = "unknown",
) -> APIRouter:
    """Build a FastAPI router that mounts ``POST /internal/events``.

    Args:
        state: The receiver's RevocationState (denylist).
        shared_secret: The expected ``X-Internal-Auth`` header value. Must
            be non-empty; receivers should fail-fast at startup if env is
            missing rather than passing an empty string here.
        on_revoke: Optional async callback invoked with ``(jti, user_sub,
            exp, reason)`` AFTER the denylist add. Used by agents to drop
            their cached OBO token. Servers can pass ``None``.
        service_label: Short label used in log lines (``"hr-agent"``,
            ``"it-server"``, …) to disambiguate audit chains.

    Returns:
        FastAPI ``APIRouter`` with the ``POST /internal/events`` route.
    """
    if not shared_secret:
        raise ValueError(
            "shared_secret must be non-empty; set INTERNAL_REVOKE_SHARED_SECRET in env."
        )

    router = APIRouter()

    @router.post("/internal/events", response_model=_AckResponse)
    async def receive_event(
        body: InternalEventBody,
        request: Request,
        x_internal_auth: str | None = Header(default=None, alias="X-Internal-Auth"),
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> _AckResponse:
        if x_internal_auth != shared_secret:
            logger.warning(
                "internal_event_auth_failed | service=%s rid=%s remote=%s",
                service_label,
                x_request_id,
                request.client.host if request.client else "?",
            )
            raise HTTPException(status_code=401, detail="invalid_secret")

        if body.type != "session-revoked":
            # Forward-compatible: future event types could be CAEP-shaped.
            logger.warning(
                "internal_event_unknown_type | service=%s rid=%s type=%r",
                service_label,
                x_request_id,
                body.type,
            )
            raise HTTPException(status_code=400, detail="unknown_event_type")

        jti = body.subject.jti
        user_sub = body.subject.sub
        exp = float(body.exp)
        reason = body.reason

        # Denylist add is idempotent — repeated events for the same jti are
        # safe (cf. R-LOGOUT-2 / FIX-19 idempotency requirement).
        already_present = jti in state.revoked_jtis
        state.revoked_jtis.add(jti, exp)

        # Service-specific side effect (e.g. drop cached _CachedToken for an agent).
        if on_revoke is not None:
            try:
                await on_revoke(jti, user_sub, exp, reason)
            except Exception:
                logger.exception(
                    "internal_event_on_revoke_failed | service=%s rid=%s jti=%s",
                    service_label,
                    x_request_id,
                    jti[:8],
                )
                # Still return 200 — the denylist is the security boundary;
                # the cache drop is best-effort.

        logger.info(
            "internal_event_received | service=%s rid=%s jti=%s user_sub=%s reason=%s already_present=%s",
            service_label,
            x_request_id,
            jti[:8],
            user_sub,
            reason,
            already_present,
        )
        if already_present:
            return _AckResponse(acked=True, note="jti already in denylist")
        return _AckResponse(acked=True)

    return router
