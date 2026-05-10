"""orchestrator/reports/routes.py — cookie-auth REST surfaces (Sprint 4).

This router mounts the cookie-authenticated reporting endpoints used by the
SPA. Two flavours co-exist:

    * Read-only proxies — ``GET`` endpoints that delegate to
      :func:`forward_with_token_a` from ``orchestrator/reports/proxy.py``.
      Cookie session → pre-flight scope → backend Bearer token-A.
    * CIBA-driven write actions — ``POST`` endpoints that trigger the same
      A2A fan-out path used by ``/api/chat`` but without going through the
      keyword router. Cookie session → ``X-Request-ID`` guard → synthetic
      :class:`ToolCall` → ``_run_serial_fan_out``.

Endpoints today:

    GET  /api/me/leaves                                  scope: hr_self_rest    [S4.3]
    GET  /api/reports/leave-requests                     scope: hr_read_rest    [S4.4 A3]
    POST /api/reports/leave-requests/{request_id}/approve scope: hr_approve_rest [S4.4 A6]
    POST /api/reports/leave-requests/{request_id}/reject  scope: hr_approve_rest [S4.4 A7]
    GET  /api/reports/cubicle-assignments                scope: hr_read_rest    [S4.5 A4]
    GET  /api/reports/device-assignments                 scope: it_assets_read_rest [S4.5 A5]

Design — A6 / A7 (Stage 5 Decision A):
    Approve / Reject are *not* chat plumbing. They are dedicated REST
    handlers that internally drive HR Agent CIBA via a one-element fan-out
    (synthetic ``ToolCall``). The handler returns 200 immediately with
    ``{ok, request_id, agent_id}``; the consent widget renders from the
    ``ciba_url`` SSE event the fan-out pushes. This mirrors the
    ``/auth/logout`` pattern: cookie auth + required ``X-Request-ID`` (F-02).

Boundary rule (F-09): ``ReportsRouterDeps`` is a ``@dataclass`` because it
holds runtime objects (the session store, the httpx client, A2A clients,
the agent registry). The router factory returns a plain ``APIRouter``;
upstream JSON is forwarded verbatim by the proxy.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common.a2a.client import A2AClient
from common.logging.correlation import get_request_id
from orchestrator.agent_registry.cards import AgentRegistry
from orchestrator.auth.session_store import Session, SessionStore
from orchestrator.chat.keyword_fallback import ToolCall
from orchestrator.chat.routes import ChatRouterDeps, _run_serial_fan_out
from orchestrator.reports.proxy import forward_with_token_a

__all__ = ["ReportsRouterDeps", "build_reports_router"]

_logger = logging.getLogger(__name__)


@dataclass
class ReportsRouterDeps:
    """Dependency bundle injected into the reports router factory.

    Attributes:
        session_store: In-memory session store (cookie → ``Session``).
        http_client: ``httpx.AsyncClient`` used for upstream proxies. Owned
            by the lifespan in ``orchestrator/main.py``.
        session_cookie_name: Cookie name to look up (typically
            ``cfg.session_cookie_name``).
        hr_server_url: Base URL for HR Server (e.g.
            ``http://hr_server:8000``). Trailing slash optional.
        it_server_url: Base URL for IT Server. Used by A5
            ``/api/reports/device-assignments`` (S4.5 UC-16).
        a2a_clients: Mapping of agent_id → ``A2AClient``. Required by the
            A6 / A7 CIBA-driven REST handlers (they reuse the chat fan-out).
        agent_registry: Registry of loaded ``AgentCard`` records. Same
            justification as ``a2a_clients`` — required by the synthetic
            fan-out path.
        chat_deps: Pre-built ``ChatRouterDeps``. The CIBA-driven handlers
            invoke ``_run_serial_fan_out`` which expects this dataclass; we
            re-use the chat router's deps rather than re-derive them.
    """

    session_store: SessionStore
    http_client: httpx.AsyncClient
    session_cookie_name: str
    hr_server_url: str
    it_server_url: str
    a2a_clients: dict[str, A2AClient] | None = None
    agent_registry: AgentRegistry | None = None
    chat_deps: ChatRouterDeps | None = None


# ---------------------------------------------------------------------------
# Pydantic request/response models (HTTP boundary — F-09)
# ---------------------------------------------------------------------------


class RejectLeaveBody(BaseModel):
    """Request body for ``POST /api/reports/leave-requests/{id}/reject``.

    Attributes:
        reason: Free-text rejection reason. Stored on the leave request row
            for audit; surfaced verbatim in the action_text via the F-08
            charset whitelist applied dispatcher-side.
    """

    reason: str


class CibaActionAck(BaseModel):
    """Acknowledgment returned by A6 / A7 (similar to ``ChatAck``).

    The actual consent flow is delivered via the SSE channel; the ack just
    confirms that the fan-out task is in flight so the SPA can begin
    rendering the consent widget on the next ``ciba_url`` event.

    Attributes:
        ok: Always ``True``.
        request_id: Correlation ID echoing the inbound ``X-Request-ID``.
        agent_id: Specialist that will drive CIBA (always ``hr_agent`` here).
    """

    ok: bool = True
    request_id: str
    agent_id: str


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_reports_router(deps: ReportsRouterDeps) -> APIRouter:
    """Build the reports router with the supplied deps closure.

    Args:
        deps: Dependency bundle (see :class:`ReportsRouterDeps`).

    Returns:
        An ``APIRouter`` ready to mount via ``app.include_router(...)``.
    """
    router = APIRouter(tags=["reports"])

    hr_base = deps.hr_server_url.rstrip("/")
    it_base = deps.it_server_url.rstrip("/")

    # ── Session-and-XRID guard (shared by A6 / A7) ────────────────────────────

    async def _resolve_session(request: Request) -> Session | JSONResponse:
        """Authenticate the cookie session; return ``Session`` or 401 envelope.

        Mirrors the pattern from ``proxy.py`` — but inlined here because the
        CIBA-driven handlers need the resolved ``Session`` object directly
        (they pass ``session.token_a.access_token`` to A2A).
        """
        session_id = request.cookies.get(deps.session_cookie_name)
        if not session_id:
            return JSONResponse(
                status_code=401,
                content={
                    "error_id": "ERR-AUTH-001",
                    "message": "Sign in required.",
                    "request_id": get_request_id() or "",
                },
            )
        try:
            session: Session = await deps.session_store.get_or_404(session_id)
        except KeyError:
            return JSONResponse(
                status_code=401,
                content={
                    "error_id": "ERR-AUTH-001",
                    "message": "Sign in required.",
                    "request_id": get_request_id() or "",
                },
            )
        if session.terminating:
            return JSONResponse(
                status_code=401,
                content={
                    "error_id": "ERR-AUTH-001",
                    "message": "Sign in required.",
                    "request_id": get_request_id() or "",
                },
            )
        return session

    # ── A1 / S4.3 — My Leaves panel ─────────────────────────────────────────

    @router.get("/api/me/leaves")
    async def get_my_leaves(request: Request) -> JSONResponse:
        """Proxy the My Leaves panel fetch to HR Server.

        Behaviour: cookie auth → pre-flight ``hr_self_rest`` → forward as
        Bearer ``token-A`` → pass through 200 body. Errors per the proxy
        primitive (401 / 403 / 503).
        """
        return await forward_with_token_a(
            request,
            session_store=deps.session_store,
            session_cookie_name=deps.session_cookie_name,
            target_url=f"{hr_base}/api/me/leaves",
            required_scope="hr_self_rest",
            http_client=deps.http_client,
        )

    # ── A3 / S4.4 — Pending Leaves report ───────────────────────────────────

    @router.get("/api/reports/leave-requests")
    async def get_pending_leave_requests(request: Request) -> JSONResponse:
        """Proxy the Pending Leaves report fetch to HR Server.

        Cookie auth → pre-flight ``hr_read_rest`` → Bearer token-A →
        upstream returns ``{data: [...], count: N}`` with one row per
        pending leave request (including ``request_id`` for the action
        buttons). Query string ``?status=pending`` is forwarded verbatim.
        """
        # Forward the status query string (HR Server defaults to "Pending"
        # when absent). The proxy primitive only forwards path; we stitch
        # the query string here so the upstream filter applies.
        query_string = request.url.query
        target_url = f"{hr_base}/api/reports/leave-requests"
        if query_string:
            target_url = f"{target_url}?{query_string}"
        return await forward_with_token_a(
            request,
            session_store=deps.session_store,
            session_cookie_name=deps.session_cookie_name,
            target_url=target_url,
            required_scope="hr_read_rest",
            http_client=deps.http_client,
        )

    # ── A4 / S4.5 — Cubicle assignments report ──────────────────────────────

    @router.get("/api/reports/cubicle-assignments")
    async def get_cubicle_assignments(request: Request) -> JSONResponse:
        """Proxy the Cubicle assignments report fetch to HR Server (UC-16).

        Cookie auth → pre-flight ``hr_read_rest`` → Bearer token-A →
        upstream returns ``{data: [...], count: N}`` with one row per
        currently-assigned cubicle (username + email + cubicle_id + floor +
        assigned_at). Identity surface is ``username`` + ``email``; ``sub``
        and ``employee_id`` are never returned (sprint-4.md §7).
        """
        return await forward_with_token_a(
            request,
            session_store=deps.session_store,
            session_cookie_name=deps.session_cookie_name,
            target_url=f"{hr_base}/api/reports/cubicle-assignments",
            required_scope="hr_read_rest",
            http_client=deps.http_client,
        )

    # ── A5 / S4.5 — Device assignments report ───────────────────────────────

    @router.get("/api/reports/device-assignments")
    async def get_device_assignments(request: Request) -> JSONResponse:
        """Proxy the Device assignments report fetch to IT Server (UC-16).

        Cookie auth → pre-flight ``it_assets_read_rest`` → Bearer token-A →
        upstream returns ``{data: [...], count: N}`` with one row per
        seeded asset (username + email + asset_id + type + model + status).
        Identity surface is ``username`` + ``email``; ``sub`` and
        ``employee_id`` are never returned (sprint-4.md §7).
        """
        return await forward_with_token_a(
            request,
            session_store=deps.session_store,
            session_cookie_name=deps.session_cookie_name,
            target_url=f"{it_base}/api/reports/device-assignments",
            required_scope="it_assets_read_rest",
            http_client=deps.http_client,
        )

    # ── A6 / S4.4 — Approve leave (CIBA-driven) ─────────────────────────────

    @router.post(
        "/api/reports/leave-requests/{request_id}/approve",
        response_model=CibaActionAck,
    )
    async def approve_leave(
        request_id: str, request: Request
    ) -> CibaActionAck | JSONResponse:
        """Trigger HR Agent CIBA for an Approve action.

        Flow:
            1. Cookie auth → 401 on missing / terminating session.
            2. Require ``X-Request-ID`` header (F-02 CSRF guard, mirrors
               ``/auth/logout``).
            3. Pre-flight scope check on ``Session.token_a`` for
               ``hr_approve_rest`` — refuse before consuming the agent if
               the SPA somehow surfaced the button without the scope.
            4. Build a synthetic ``ToolCall`` for ``hr.approve_leave`` and
               spawn ``_run_serial_fan_out`` so the existing consent
               widget plumbing (CibaUrlEvent → CibaStateChange) takes over.
            5. Return 200 ``{ok, request_id, agent_id}`` immediately.
        """
        rid = request.headers.get("X-Request-ID")
        if not rid:
            _logger.warning("reports_approve_missing_rid | rejecting per F-02")
            raise HTTPException(status_code=400, detail="X-Request-ID required")

        session_or_err = await _resolve_session(request)
        if isinstance(session_or_err, JSONResponse):
            return session_or_err
        session: Session = session_or_err

        token_a = session.token_a
        scope_string = (token_a.scope or "") if token_a is not None else ""
        if "hr_approve_rest" not in scope_string.split():
            _logger.warning(
                "reports_approve_preflight_scope_denied | session_id=%s",
                session.session_id,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error_id": "ERR-AUTH-scope-missing",
                    "message": "This action requires the hr_approve_rest scope.",
                    "request_id": rid,
                },
            )

        chat_deps = deps.chat_deps
        if chat_deps is None:
            _logger.error("reports_approve_chat_deps_unset | misconfiguration")
            raise HTTPException(status_code=500, detail="reports router not fully wired")

        tool_call = ToolCall(
            agent_id="hr_agent",
            tool_id="hr.approve_leave",
            args={"leave_id": request_id},
        )

        _logger.info(
            "reports_approve_dispatched | request_id=%s leave_id=%s session_id=%s",
            rid,
            request_id,
            session.session_id,
        )

        asyncio.create_task(
            _run_serial_fan_out(session, [tool_call], rid, chat_deps),
            name=f"reports_approve:{rid}",
        )

        return CibaActionAck(request_id=rid, agent_id="hr_agent")

    # ── A7 / S4.4 — Reject leave (CIBA-driven) ──────────────────────────────

    @router.post(
        "/api/reports/leave-requests/{request_id}/reject",
        response_model=CibaActionAck,
    )
    async def reject_leave(
        request_id: str, body: RejectLeaveBody, request: Request
    ) -> CibaActionAck | JSONResponse:
        """Trigger HR Agent CIBA for a Reject action.

        Same shape as A6 with an additional body carrying the rejection
        reason. The reason is forwarded into the synthetic ``ToolCall``
        args; the dispatcher passes it to the MCP tool which records it on
        the leave request row for audit.
        """
        rid = request.headers.get("X-Request-ID")
        if not rid:
            _logger.warning("reports_reject_missing_rid | rejecting per F-02")
            raise HTTPException(status_code=400, detail="X-Request-ID required")

        if not (body.reason or "").strip():
            return JSONResponse(
                status_code=400,
                content={
                    "error_id": "ERR-VALIDATION-reason-empty",
                    "message": "A non-empty reason is required to reject a leave request.",
                    "request_id": rid,
                },
            )

        session_or_err = await _resolve_session(request)
        if isinstance(session_or_err, JSONResponse):
            return session_or_err
        session: Session = session_or_err

        token_a = session.token_a
        scope_string = (token_a.scope or "") if token_a is not None else ""
        if "hr_approve_rest" not in scope_string.split():
            _logger.warning(
                "reports_reject_preflight_scope_denied | session_id=%s",
                session.session_id,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error_id": "ERR-AUTH-scope-missing",
                    "message": "This action requires the hr_approve_rest scope.",
                    "request_id": rid,
                },
            )

        chat_deps = deps.chat_deps
        if chat_deps is None:
            _logger.error("reports_reject_chat_deps_unset | misconfiguration")
            raise HTTPException(status_code=500, detail="reports router not fully wired")

        tool_call = ToolCall(
            agent_id="hr_agent",
            tool_id="hr.reject_leave",
            args={"leave_id": request_id, "reason": body.reason.strip()},
        )

        _logger.info(
            "reports_reject_dispatched | request_id=%s leave_id=%s session_id=%s",
            rid,
            request_id,
            session.session_id,
        )

        asyncio.create_task(
            _run_serial_fan_out(session, [tool_call], rid, chat_deps),
            name=f"reports_reject:{rid}",
        )

        return CibaActionAck(request_id=rid, agent_id="hr_agent")

    return router
