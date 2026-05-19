"""IT Agent specialist service — Sprint 1.

Entry point for the it_agent FastAPI process.  Builds every dependency from
``ITAgentConfig``, wires the A2A router, and exposes a ``/healthz`` probe.

Public surface
--------------
``create_app(config: ITAgentConfig | None = None) -> FastAPI``
    Application factory.  Accepts an optional pre-built config for testing;
    reads from ``os.environ`` when ``None``.

``main() -> None``
    Uvicorn entry point.  Reads ``cfg.host`` / ``cfg.port`` from the config.

Dependency wiring order (F-09 boundary rules apply throughout)
--------------------------------------------------------------
1.  ``WSO2ISClient``         — HTTP client for App-Native Auth
2.  ``ActorTokenProvider``   — caches the agent's own I4 actor-token
3.  ``CIBAClient``           — CIBA initiation + polling
4.  ``ITMcpClient``          — HTTP client for it_server tool endpoints
5.  ``ITDispatcher``         — implements DispatchProtocol; owns pending dict
6.  ``FastAPI`` app          — CorrelationIdMiddleware + A2A router + /healthz

All objects that contain ``asyncio`` types (``asyncio.Lock``, ``asyncio.Event``,
``asyncio.Task``) are ``@dataclass``; no Pydantic models appear in this module
(F-09).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from common.auth.actor_token_provider import ActorTokenProvider
from common.auth.ciba_client import CIBAClient, CIBAClientConfig
from common.auth.wso2_is_client import WSO2ISClient
from common.logging.correlation import CorrelationIdMiddleware, install_logging
from common.revocation import RevocationState, build_internal_events_router
from it_agent.a2a.handler import ITA2AHandlerDeps, build_it_a2a_router
from it_agent.ciba.orchestrator import ITDispatcher, ITDispatcherDeps
from it_agent.config import ITAgentConfig
from it_agent.mcp.client import ITMcpClient, ITMcpClientConfig

logger = logging.getLogger(__name__)

__all__ = ["create_app", "main"]


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(config: ITAgentConfig | None = None) -> FastAPI:
    """Build and return the IT Agent FastAPI application.

    Wires all dependencies from ``config`` and returns a fully-configured
    ``FastAPI`` instance ready to be handed to uvicorn.  No side-effects
    outside this function; safe to call multiple times (e.g., in tests).

    Args:
        config: Pre-built, validated configuration.  When ``None`` the config
            is loaded from ``os.environ`` via ``ITAgentConfig.from_env()``.

    Returns:
        A ``FastAPI`` application with:
        - ``CorrelationIdMiddleware`` (X-Request-ID propagation, F-13 / F-16)
        - A2A router mounted at the root (``/a2a/message/send``,
          ``/a2a/await``, ``/a2a/cancel``)
        - ``GET /healthz`` liveness probe
    """
    cfg: ITAgentConfig = config if config is not None else ITAgentConfig.from_env()

    # 3A.2 BLOCK-I: single-worker invariant.
    workers = int(os.getenv("UVICORN_WORKERS", "1"))
    assert workers == 1, (
        f"it_agent requires UVICORN_WORKERS=1 (got {workers}). "
        "Multi-worker support requires Redis-backed denylist (Sprint 4+)."
    )

    # ── Logging ───────────────────────────────────────────────────────────────
    install_logging(level="INFO")

    # Traceloop SDK — activates @atask decorators used in the IT dispatcher.
    # disable_batch=True avoids a second span processor since amp-instrument
    # already exports spans.
    try:
        from traceloop.sdk import Traceloop
        Traceloop.init(app_name="it_agent", disable_batch=True)
        logger.info("traceloop_sdk_initialized")
    except Exception as exc:  # noqa: BLE001
        logger.debug("traceloop_sdk_init_skipped reason=%r", exc)

    # ── 1. WSO2ISClient ────────────────────────────────────────────────────────
    is_client = WSO2ISClient(cfg.is_client_config())

    # ── 2. ActorTokenProvider (agent's own I4 actor-token, F-09 dataclass) ─────
    actor_token_provider = ActorTokenProvider(
        credentials=cfg.agent,
        is_client=is_client,
    )

    # ── 3. CIBAClient (CIBA initiation + polling) ──────────────────────────────
    ciba_client = CIBAClient(
        config=CIBAClientConfig(
            is_base_url=cfg.is_base_url,
            insecure_tls=cfg.is_insecure_tls,
        )
    )

    # ── 4. ITMcpClient (plain HTTP to it_server) ───────────────────────────────
    mcp_client = ITMcpClient(
        config=ITMcpClientConfig(base_url=cfg.it_server_url)
    )

    # ── 5. ITDispatcher (owns in-process pending dict, DispatchProtocol) ───────
    dispatcher_deps = ITDispatcherDeps(
        ciba_client=ciba_client,
        actor_token_provider=actor_token_provider,
        mcp_client=mcp_client,
        oauth_client_id=cfg.agent.oauth_client_id,
        oauth_client_secret=cfg.agent.oauth_client_secret,
        agent_id=cfg.agent.agent_id,
        agent_label="IT Agent",
        ciba_scope=cfg.ciba_scope,
        max_poll_seconds=float(cfg.max_poll_seconds),
    )
    dispatcher = ITDispatcher(deps=dispatcher_deps)

    # 3A.2: revocation state. Lifespan owns the sweeper task.
    revocation = RevocationState()
    if hasattr(dispatcher, "attach_revocation"):
        dispatcher.attach_revocation(revocation)

    # Shared pending state: keyed by auth_req_id, accessed by router + dispatcher
    pending: dict = {}

    # ── 6. FastAPI app + middleware + router ───────────────────────────────────
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Lifespan context: log startup/shutdown.

        Resources (is_client, ciba_client, mcp_client) are closed on shutdown
        so that async httpx clients are not leaked in tests.
        """
        logger.info(
            "it_agent_startup | host=%s port=%d is_base_url=%s it_server_url=%s",
            cfg.host,
            cfg.port,
            cfg.is_base_url,
            cfg.it_server_url,
        )
        # 3A.2 FIX-21: lifespan-wired sweeper.
        sweep_task = asyncio.create_task(revocation.revoked_jtis.sweep_loop())
        revocation.sweep_task = sweep_task

        # Mid-sprint fix #3 (2026-05-09): pre-warm the shared JWKS registry.
        try:
            from common.auth.jwt_validator import prewarm_shared_cache
            await prewarm_shared_cache(
                jwks_url=cfg.is_jwks_url,
                insecure_tls=cfg.is_insecure_tls,
            )
            logger.info("it_agent.jwks_prewarm_ok jwks_url=%s", cfg.is_jwks_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("it_agent.jwks_prewarm_failed err=%r", exc)

        yield
        logger.info("it_agent_shutdown")
        sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep_task
        await mcp_client.aclose()
        await ciba_client.aclose()
        await is_client.aclose()

    app = FastAPI(
        title="it_agent",
        version="1.0.0-sprint1",
        lifespan=lifespan,
    )

    # X-Request-ID propagation (F-13, F-16: generate with WARN when absent)
    app.add_middleware(CorrelationIdMiddleware)

    # A2A router: POST /a2a/message/send, POST /a2a/await, POST /a2a/cancel
    a2a_router = build_it_a2a_router(
        ITA2AHandlerDeps(
            config=cfg,
            dispatcher=dispatcher,
            pending=pending,
        )
    )
    app.include_router(a2a_router)

    # 3A.2 + FIX-2 (mid-sprint review): WARN on empty secret so misconfigs
    # are observable in operator logs.
    secret = getattr(cfg, "internal_revoke_shared_secret", "")
    if secret:
        app.include_router(
            build_internal_events_router(
                state=revocation,
                shared_secret=secret,
                on_revoke=dispatcher.revoke_jti,
                service_label="it-agent",
            )
        )
    else:
        logger.warning(
            "internal_events_receiver_disabled | service=it-agent reason=no_shared_secret"
        )

    # ── /healthz ───────────────────────────────────────────────────────────────
    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        """Liveness probe — returns service name for quick identification."""
        return {"ok": True, "service": "it_agent"}

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the IT Agent with uvicorn.

    Reads all configuration from ``os.environ``.  Exits non-zero if any
    required environment variable is missing (``ValueError`` from
    ``ITAgentConfig.from_env``).
    """
    import uvicorn

    install_logging()
    cfg = ITAgentConfig.from_env()

    logger.info("Starting it_agent on %s:%d", cfg.host, cfg.port)
    uvicorn.run(
        "it_agent.main:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
        log_config=None,  # we manage logging ourselves
    )


if __name__ == "__main__":
    main()
