"""orchestrator/reports/routes.py — cookie-auth REST surfaces (Sprint 4 S4.3+).

This router currently mounts a single endpoint:

    GET /api/me/leaves     scope (pre-flight): hr_self_rest

S4.4 / S4.5 add ``/api/reports/...`` endpoints to the same router. The
endpoint bodies all delegate to :func:`forward_with_token_a` from
``orchestrator/reports/proxy.py`` so the cookie-session → token-A → backend
plumbing is shared.

Boundary rule (F-09): ``ReportsRouterDeps`` is a ``@dataclass`` because it
holds runtime objects (the session store, the httpx client). The router
factory returns a plain ``APIRouter`` — no Pydantic models cross the HTTP
boundary here, the upstream JSON is forwarded verbatim by the proxy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from orchestrator.auth.session_store import SessionStore
from orchestrator.reports.proxy import forward_with_token_a

__all__ = ["ReportsRouterDeps", "build_reports_router"]

_logger = logging.getLogger(__name__)


@dataclass
class ReportsRouterDeps:
    """Dependency bundle injected into the reports router factory.

    Attributes:
        session_store: In-memory session store (cookie → ``Session``).
        http_client: ``httpx.AsyncClient`` used for upstream fan-out. Owned
            by the lifespan in ``orchestrator/main.py``.
        session_cookie_name: Cookie name to look up (typically
            ``cfg.session_cookie_name``).
        hr_server_url: Base URL for HR Server (e.g.
            ``http://hr_server:8000``). Trailing slash optional.
        it_server_url: Base URL for IT Server. Currently unused — declared
            for the S4.5 device-assignments endpoint that lands on the
            same router.
    """

    session_store: SessionStore
    http_client: httpx.AsyncClient
    session_cookie_name: str
    hr_server_url: str
    it_server_url: str


def build_reports_router(deps: ReportsRouterDeps) -> APIRouter:
    """Build the reports router with the supplied deps closure.

    Args:
        deps: Dependency bundle (see :class:`ReportsRouterDeps`).

    Returns:
        An ``APIRouter`` ready to mount via ``app.include_router(...)``.
    """
    router = APIRouter(tags=["reports"])

    hr_base = deps.hr_server_url.rstrip("/")

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

    return router
