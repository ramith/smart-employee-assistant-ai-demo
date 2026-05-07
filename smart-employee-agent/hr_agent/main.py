"""HR-agent FastAPI application factory — Wave 8, Sprint 1.

Wires all Sprint-1 components into a runnable FastAPI app:

- ``CorrelationIdMiddleware``  — X-Request-ID propagation on every request (F-16).
- ``RedactionFilter``          — strips JWTs / secrets from log records (F-11, T1).
- ``WSO2ISClient``             — shared IS HTTP client (App-Native Auth, JWKS).
- ``ActorTokenProvider``       — single-flight actor-token cache.
- ``CIBAClient``               — CIBA initiate + poll.
- ``HRMcpClient``              — hr_server MCP tool caller.
- ``HRDispatcher``             — CIBA→OBO→MCP orchestrator, implements DispatchProtocol.
- ``build_hr_a2a_router``      — mounts POST /a2a/message/send, /a2a/await, /a2a/cancel.
- GET /healthz                 — liveness probe.

Design decisions
----------------
- ``pending: dict[str, A2APendingState]``  is an in-process map (Q5 single-process).
  Sprint 2 may replace it with a Redis-backed map for horizontal scaling.
- All heavy objects (IS client, MCP client …) are created inside the ``lifespan``
  async context manager and closed on shutdown — no leaked connections.
- ``create_app`` accepts an optional ``HRAgentConfig`` for test injection; when
  ``None`` the config is read from environment variables.
- F-09 boundary: nothing in this file is Pydantic; all runtime-object holders are
  plain dataclasses or regular classes.

Usage (production)::

    uvicorn hr_agent.main:create_app --factory --host 0.0.0.0 --port 8001

Usage (test injection)::

    app = create_app(config=fake_cfg)
    with TestClient(app) as client:
        resp = client.get("/healthz")
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from common.auth.actor_token_provider import ActorTokenProvider, AgentCredentials
from common.auth.ciba_client import CIBAClient, CIBAClientConfig
from common.auth.wso2_is_client import WSO2ISClient
from common.logging.correlation import CorrelationIdMiddleware, install_logging
from common.logging.redaction import RedactionFilter
from hr_agent.a2a.handler import HRA2AHandlerDeps, build_hr_a2a_router
from hr_agent.ciba.orchestrator import HRDispatcher, HRDispatcherDeps
from hr_agent.config import HRAgentConfig
from hr_agent.mcp.client import HRMcpClient, HRMcpClientConfig

# Import A2APendingState for the shared pending map type annotation.
from common.a2a.server import A2APendingState

__all__ = ["create_app", "main"]

logger = logging.getLogger(__name__)


def create_app(config: HRAgentConfig | None = None) -> FastAPI:
    """Build and return the HR-agent FastAPI application.

    Creates all heavyweight objects inside the ``lifespan`` async context
    manager, ensuring correct startup / shutdown ordering and no leaked
    connections.

    Args:
        config: Pre-built configuration instance.  When ``None`` (production),
            :meth:`HRAgentConfig.from_env` is called to read from environment
            variables.  Pass a pre-built instance in tests to avoid env-var
            dependency.

    Returns:
        A fully configured :class:`fastapi.FastAPI` instance ready to serve
        requests.  Lifespan has NOT been entered yet — the caller (or ASGI
        server) is responsible for that.
    """
    cfg: HRAgentConfig = config or HRAgentConfig.from_env()

    # ── Logging: install once at app-factory time ──────────────────────────────
    install_logging(level="INFO")
    logging.getLogger().addFilter(RedactionFilter())

    # ── In-process CIBA pending state map (Q5 single-process) ─────────────────
    # Shared between the lifespan (which owns HRDispatcher) and the A2A router
    # (which exposes /a2a/await and /a2a/cancel). Both are wired in the same
    # OS process so a plain dict suffices for Sprint 1.
    pending: dict[str, A2APendingState] = {}

    # ── Lifespan: create / close expensive objects ─────────────────────────────

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Create shared resources on startup, close them on shutdown.

        Objects are stored on ``app.state`` so integration tests can inspect
        them (e.g. ``app.state.mcp_client``). They are NOT used from ``app.state``
        by the route handlers — handlers receive their dependencies through the
        A2ARouterConfig / HRA2AHandlerDeps that were assembled BEFORE the
        lifespan was entered.
        """
        logger.info(
            "hr_agent_startup | host=%s port=%d is_base_url=%s",
            cfg.host,
            cfg.port,
            cfg.is_base_url,
        )

        # ── IS HTTP client ─────────────────────────────────────────────────────
        is_client = WSO2ISClient(config=cfg.is_client_config())

        # ── Actor-token provider (single-flight cache) ─────────────────────────
        actor_provider = ActorTokenProvider(
            credentials=AgentCredentials(
                agent_id=cfg.agent.agent_id,
                agent_secret=cfg.agent.agent_secret,
                oauth_client_id=cfg.agent.oauth_client_id,
                oauth_client_secret=cfg.agent.oauth_client_secret,
            ),
            is_client=is_client,
        )

        # ── CIBA client ────────────────────────────────────────────────────────
        ciba_client = CIBAClient(
            config=CIBAClientConfig(
                is_base_url=cfg.is_base_url,
                insecure_tls=cfg.is_insecure_tls,
            )
        )

        # ── HR-server MCP client ───────────────────────────────────────────────
        mcp_client = HRMcpClient(
            config=HRMcpClientConfig(base_url=cfg.hr_server_url)
        )

        # ── CIBA dispatcher (ties CIBA + actor-token + MCP together) ──────────
        dispatcher = HRDispatcher(
            deps=HRDispatcherDeps(
                ciba_client=ciba_client,
                actor_token_provider=actor_provider,
                mcp_client=mcp_client,
                oauth_client_id=cfg.agent.oauth_client_id,
                oauth_client_secret=cfg.agent.oauth_client_secret,
                agent_id=cfg.agent.agent_id,
                ciba_scope=cfg.ciba_scope,
                max_poll_seconds=float(cfg.max_poll_seconds),
            )
        )

        # ── Expose objects on app.state for observability / test assertions ────
        app.state.is_client = is_client
        app.state.ciba_client = ciba_client
        app.state.mcp_client = mcp_client
        app.state.dispatcher = dispatcher

        logger.info("hr_agent_startup_complete")

        yield  # Application is live.

        # ── Shutdown: close HTTP connections in reverse dependency order ───────
        logger.info("hr_agent_shutdown_start")
        await mcp_client.aclose()
        await ciba_client.aclose()
        await is_client.aclose()
        logger.info("hr_agent_shutdown_complete")

    # ── App construction ───────────────────────────────────────────────────────
    app = FastAPI(title="HR Agent", lifespan=lifespan)

    # Middleware: correlation ID must be outermost so all route handlers see it.
    app.add_middleware(CorrelationIdMiddleware)

    # ── A2A router — three specialist endpoints ────────────────────────────────
    # The dispatcher is created inside lifespan (per-startup) but the router
    # must be registered at app-factory time so FastAPI can discover routes
    # during startup. We build the dispatcher here too (stateless construction;
    # actual IS / MCP connections are deferred to lifespan).
    #
    # For the router we need a dispatcher instance at factory time. We construct
    # a *second* dispatcher that shares the same pending dict; the lifespan also
    # stores one on app.state for observability. Both share the same pending map
    # which is the only mutable state that needs to be consistent.
    _is_client_for_router = WSO2ISClient(config=cfg.is_client_config())
    _actor_provider_for_router = ActorTokenProvider(
        credentials=AgentCredentials(
            agent_id=cfg.agent.agent_id,
            agent_secret=cfg.agent.agent_secret,
            oauth_client_id=cfg.agent.oauth_client_id,
            oauth_client_secret=cfg.agent.oauth_client_secret,
        ),
        is_client=_is_client_for_router,
    )
    _ciba_client_for_router = CIBAClient(
        config=CIBAClientConfig(
            is_base_url=cfg.is_base_url,
            insecure_tls=cfg.is_insecure_tls,
        )
    )
    _mcp_client_for_router = HRMcpClient(
        config=HRMcpClientConfig(base_url=cfg.hr_server_url)
    )
    _dispatcher_for_router = HRDispatcher(
        deps=HRDispatcherDeps(
            ciba_client=_ciba_client_for_router,
            actor_token_provider=_actor_provider_for_router,
            mcp_client=_mcp_client_for_router,
            oauth_client_id=cfg.agent.oauth_client_id,
            oauth_client_secret=cfg.agent.oauth_client_secret,
            agent_id=cfg.agent.agent_id,
            ciba_scope=cfg.ciba_scope,
            max_poll_seconds=float(cfg.max_poll_seconds),
        )
    )

    app.include_router(
        build_hr_a2a_router(
            HRA2AHandlerDeps(
                config=cfg,
                dispatcher=_dispatcher_for_router,
                pending=pending,
            )
        )
    )

    # ── Liveness probe ─────────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict:
        """Return a simple liveness indicator.

        Returns:
            JSON ``{"ok": True, "service": "hr_agent"}``.
        """
        return {"ok": True, "service": "hr_agent"}

    return app


def main() -> None:
    """Entry point for production execution via ``python -m hr_agent.main``.

    Reads configuration from environment variables and starts a Uvicorn server
    using the ``factory`` mode so that Uvicorn calls ``create_app()`` itself
    (enabling proper lifespan management).
    """
    import uvicorn

    cfg = HRAgentConfig.from_env()
    uvicorn.run(
        "hr_agent.main:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
    )


if __name__ == "__main__":
    main()
