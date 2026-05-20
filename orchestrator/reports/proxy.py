"""Reusable cookie-session → token-A → backend pass-through primitive.

Sprint 4 S4.3 introduces this primitive so subsequent slices (S4.4, S4.5)
can mount additional reporting endpoints without re-implementing the
session-lookup / pre-flight / Bearer-forward / pass-through plumbing.

Flow (per `docs/architecture/sprint-4.md` §8 — security view):

    1. Look up ``Session`` by ``orch_sid`` cookie. Missing or unknown → 401.
    2. Reject if ``Session.terminating`` (a logout cascade is in flight) → 401.
    3. Pre-flight scope check on ``Session.token_a.scope`` — if the required
       scope is absent, refuse before round-tripping to the backend (cheap
       UI-bug guard; the backend remains authoritative).
    4. ``GET <target_url>`` with ``Authorization: Bearer <token-A>`` and the
       inbound ``X-Request-ID`` propagated (best-effort).
    5. Pass the upstream 200 body back verbatim. Map upstream 5xx / network
       errors to a 503 with ``ERR-API-PROXY-001`` per Stage 5 §6.

Boundary rule (F-09): this module exposes a plain function — no Pydantic
models cross the HTTP boundary here, the upstream JSON is forwarded as-is.
The error envelope uses the same ``{error_id, message, request_id}`` shape
as ``orchestrator/auth/routes.py``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from common.logging.correlation import get_request_id
from orchestrator.auth.session_store import Session, SessionStore

__all__ = ["forward_with_token_a"]

_logger = logging.getLogger(__name__)


def _error(*, status_code: int, error_id: str, message: str) -> JSONResponse:
    """Build a JSON error envelope matching the orchestrator-wide shape."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error_id": error_id,
            "message": message,
            "request_id": get_request_id() or "",
        },
    )


async def forward_with_token_a(
    request: Request,
    *,
    session_store: SessionStore,
    session_cookie_name: str,
    target_url: str,
    required_scope: str,
    http_client: httpx.AsyncClient,
) -> JSONResponse:
    """Forward a cookie-authenticated GET to a backend with token-A.

    Args:
        request: Incoming FastAPI/Starlette request — used to read the cookie
            and propagate ``X-Request-ID``.
        session_store: The orchestrator's in-memory session store.
        session_cookie_name: Cookie name (typically ``cfg.session_cookie_name``).
        target_url: Fully-qualified upstream URL, e.g.
            ``"http://hr_server:8000/api/me/leaves"``.
        required_scope: Scope that must be present on token-A before the
            round-trip is attempted. Pre-flight only — the backend remains
            authoritative.
        http_client: Caller-owned ``httpx.AsyncClient``.

    Returns:
        ``JSONResponse``: 200 with the upstream body verbatim on success;
        401 / 403 / 503 with the ``ErrorEnvelope`` shape otherwise.
    """
    session_id = request.cookies.get(session_cookie_name)
    if not session_id:
        return _error(
            status_code=401,
            error_id="ERR-AUTH-001",
            message="Sign in required.",
        )

    try:
        session: Session = await session_store.get_or_404(session_id)
    except KeyError:
        return _error(
            status_code=401,
            error_id="ERR-AUTH-001",
            message="Sign in required.",
        )

    # Mirror the chat-route fence — once a logout cascade has set
    # Session.terminating the cookie may still authenticate but the session
    # is being torn down. Refuse new proxied calls cleanly.
    if session.terminating:
        return _error(
            status_code=401,
            error_id="ERR-AUTH-001",
            message="Sign in required.",
        )

    # Pre-flight scope check — refuse before the backend round-trip when the
    # required scope is obviously absent. The backend remains authoritative.
    token_a = session.token_a
    scope_string = (token_a.scope or "") if token_a is not None else ""
    if required_scope not in scope_string.split():
        _logger.warning(
            "proxy_preflight_scope_denied | required=%s present=%r session_id=%s",
            required_scope,
            scope_string,
            session.session_id,
        )
        return _error(
            status_code=403,
            error_id="ERR-AUTH-scope-missing",
            message=f"This view requires the {required_scope} scope.",
        )

    # Forward the call.
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token_a.access_token}",
        "Accept": "application/json",
    }
    request_id = get_request_id()
    if request_id:
        headers["X-Request-ID"] = request_id

    try:
        upstream = await http_client.get(target_url, headers=headers)
    except httpx.HTTPError as exc:
        _logger.warning(
            "proxy_upstream_unreachable | target=%s exc=%r request_id=%s",
            target_url,
            exc,
            request_id,
        )
        return _error(
            status_code=503,
            error_id="ERR-API-PROXY-001",
            message="The reporting backend is not responding right now.",
        )

    if upstream.status_code >= 500:
        _logger.warning(
            "proxy_upstream_5xx | target=%s status=%d request_id=%s",
            target_url,
            upstream.status_code,
            request_id,
        )
        return _error(
            status_code=503,
            error_id="ERR-API-PROXY-001",
            message="The reporting backend is not responding right now.",
        )

    # For 200 we forward the body verbatim. Non-200 (4xx) is also forwarded
    # as-is so the SPA can surface the upstream's specific error code (e.g.
    # 403 with ``ERR-MCP-003`` from the HR Server scope guard) without the
    # orchestrator paraphrasing.
    body: Any
    try:
        body = upstream.json()
    except ValueError:
        # Upstream returned a non-JSON payload — surface as a 503 to keep the
        # SPA's contract clean (it expects JSON envelopes).
        _logger.warning(
            "proxy_upstream_not_json | target=%s status=%d request_id=%s",
            target_url,
            upstream.status_code,
            request_id,
        )
        return _error(
            status_code=503,
            error_id="ERR-API-PROXY-001",
            message="The reporting backend returned an unexpected response.",
        )

    return JSONResponse(status_code=upstream.status_code, content=body)
