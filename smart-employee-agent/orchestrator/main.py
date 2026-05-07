"""Orchestrator service entry-point — Sprint 1 Wave 8.

Wires all sub-systems (auth, chat, SSE, agent registry, A2A clients) into a
single runnable FastAPI application.

Usage
-----
*As a factory (uvicorn ``--factory`` flag or ``create_app()``)::*

    uvicorn orchestrator.main:create_app --factory --port 8080

*As a module (``python -m``)::*

    python -m orchestrator.main

Agent-card directory
--------------------
The AgentRegistry is populated from local JSON files at startup.  The
directory is resolved in the following priority order:

1. ``AGENT_CARDS_DIR`` environment variable (absolute or relative path).
2. ``<repo-root>/tests/fixtures/agent_cards/`` — Sprint 1 demo default.
3. ``<repo-root>/orchestrator/agent_cards/`` — future production directory.

A missing directory is logged at WARNING level; the registry starts empty and
the service still boots (graceful degradation).

Design notes
------------
- Middleware order (F-13): ``CorrelationIdMiddleware`` is mounted FIRST so that
  ``X-Request-ID`` is set in the ContextVar before any route handler runs.
  ``add_middleware()`` prepends; therefore CorrelationIdMiddleware must be the
  LAST call to ``add_middleware`` so that it ends up at the front of the chain.
- F-15 collision check is fully performed inside ``OrchestratorConfig.from_env()``
  (Wave 4).  No duplicate check is needed here.
- F-14 (``LLM_FALLBACK_MODE=keyword`` default): ``KeywordRouter()`` is
  constructed without arguments, which picks up ``DEFAULT_RULES``.  The
  ``llm_fallback_mode`` field of the config is intentionally not wired to
  runtime routing in Sprint 1 — the keyword router is always used.  Sprint 2
  will add a branch for ``"llm"`` mode.
- ``app.state`` is NOT used for shared resources.  Router factories capture
  dependencies via closure.  This avoids attribute look-ups at request time
  and makes the dependency graph explicit at wiring time.
- Lifespan owns all resource lifetimes (httpx clients).  It closes IS client
  and all A2A clients on shutdown regardless of whether they were used.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.a2a.client import A2AClient, A2AClientConfig
from common.auth.actor_token_provider import ActorTokenProvider, AgentCredentials
from common.auth.jwt_validator import JWKSCache, ValidatorConfig
from common.auth.wso2_is_client import WSO2ISClient
from common.logging.correlation import CorrelationIdMiddleware, install_logging
from common.logging.redaction import RedactionFilter
from orchestrator.agent_registry.cards import AgentRegistry
from orchestrator.auth.pattern_c import PatternCExchanger
from orchestrator.auth.routes import AuthRouterDeps, build_auth_router
from orchestrator.auth.session_store import SessionStore
from orchestrator.chat.keyword_fallback import KeywordRouter
from orchestrator.chat.routes import ChatRouterDeps, build_chat_router
from orchestrator.config import OrchestratorConfig
from orchestrator.events.sse_router import SseRouterDeps, build_sse_router

__all__ = ["create_app", "main"]

logger = logging.getLogger(__name__)

# ── Agent-card directory resolution ───────────────────────────────────────────

_HERE = Path(__file__).parent  # …/orchestrator/
_REPO_ROOT = _HERE.parent       # …/smart-employee-agent/

_AGENT_CARDS_CANDIDATES: list[Path] = [
    # Priority 2: test fixtures (Sprint 1 demo default)
    _REPO_ROOT / "tests" / "fixtures" / "agent_cards",
    # Priority 3: future production directory
    _HERE / "agent_cards",
]


def _resolve_agent_cards_dir() -> Path | None:
    """Return the first valid agent-cards directory, or ``None``.

    Priority:
    1. ``AGENT_CARDS_DIR`` env var (if set and the path exists).
    2. ``tests/fixtures/agent_cards/`` relative to the repo root.
    3. ``orchestrator/agent_cards/`` relative to the repo root.

    Returns:
        An existing :class:`~pathlib.Path` pointing to a directory of
        agent-card JSON files, or ``None`` if no candidate exists.
    """
    env_override = os.environ.get("AGENT_CARDS_DIR", "").strip()
    if env_override:
        candidate = Path(env_override)
        if candidate.is_dir():
            return candidate
        logger.warning(
            "AGENT_CARDS_DIR=%r does not point to an existing directory — ignoring",
            env_override,
        )

    for candidate in _AGENT_CARDS_CANDIDATES:
        if candidate.is_dir():
            return candidate

    return None


def _load_agent_registry() -> AgentRegistry:
    """Build an AgentRegistry from local JSON files.

    Scans ``_resolve_agent_cards_dir()`` for ``*.json`` files and delegates to
    :meth:`AgentRegistry.from_files`.  An empty registry is returned when no
    directory is found or no files parse successfully.

    Returns:
        A populated (possibly empty) :class:`AgentRegistry`.
    """
    cards_dir = _resolve_agent_cards_dir()
    if cards_dir is None:
        logger.warning(
            "No agent-cards directory found.  Set AGENT_CARDS_DIR or add files to "
            "tests/fixtures/agent_cards/*.json.  Registry will be empty."
        )
        return AgentRegistry()

    json_paths = sorted(cards_dir.glob("*.json"))
    if not json_paths:
        logger.warning(
            "Agent-cards directory %s contains no *.json files.  Registry will be empty.",
            cards_dir,
        )
        return AgentRegistry()

    registry = AgentRegistry.from_files(json_paths)
    logger.info(
        "agent_registry_loaded | dir=%s files=%d cards=%d",
        cards_dir,
        len(json_paths),
        len(registry.all()),
    )
    return registry


# ── App factory ───────────────────────────────────────────────────────────────


def create_app(config: OrchestratorConfig | None = None) -> FastAPI:
    """Build and return a fully-wired FastAPI app for the orchestrator.

    All sub-systems are constructed inside the lifespan context manager so
    that their shutdown hooks run cleanly when the process exits.

    Middleware order is significant — see module-level docstring for the
    ``CorrelationIdMiddleware`` ordering rationale.

    Args:
        config: Optional pre-built config (useful in tests to avoid hitting
            ``os.environ``).  When ``None`` the config is read from
            ``os.environ`` via :meth:`OrchestratorConfig.from_env`.

    Returns:
        A fully-wired :class:`fastapi.FastAPI` instance ready for
        ``uvicorn`` or the test client.
    """
    cfg = config or OrchestratorConfig.from_env()

    # ── Logging ───────────────────────────────────────────────────────────────
    # install_logging is idempotent; safe to call multiple times in tests.
    install_logging(level="INFO")
    root_logger = logging.getLogger()
    root_logger.addFilter(RedactionFilter())

    # ── Shared resources (constructed outside lifespan for CORS wiring) ───────
    # A2A clients and AgentRegistry are stateless — safe to build before
    # lifespan starts.  IS client and PatternCExchanger need an event loop,
    # so they live inside the lifespan.
    a2a_clients: dict[str, A2AClient] = {
        "hr_agent": A2AClient(A2AClientConfig(base_url=cfg.hr_agent_url)),
        "it_agent": A2AClient(A2AClientConfig(base_url=cfg.it_agent_url)),
    }
    agent_registry = _load_agent_registry()
    keyword_router = KeywordRouter()  # DEFAULT_RULES per F-14
    session_store = SessionStore()

    # ── Lifespan ──────────────────────────────────────────────────────────────

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Set up and tear down shared async resources.

        Resources created here:
        - ``WSO2ISClient``         — owns its httpx.AsyncClient
        - ``JWKSCache``            — lazy-fetched; no startup I/O
        - ``ActorTokenProvider``   — mint agent I4 token on first use
        - ``PatternCExchanger``    — injected into auth router

        All are injected into the router factories via their ``Deps``
        dataclasses.  No global state is mutated.

        On shutdown ``is_client.aclose()`` and all ``A2AClient.aclose()``
        calls are awaited so that httpx connection pools drain gracefully.
        """
        is_client_cfg = cfg.is_client_config()
        is_client = WSO2ISClient(config=is_client_cfg)

        jwks_cache = JWKSCache(
            jwks_url=cfg.is_jwks_url,
            insecure_tls=cfg.is_insecure_tls,
        )

        validator_cfg = ValidatorConfig(
            expected_iss=cfg.is_issuer,
            jwks_url=cfg.is_jwks_url,
            insecure_tls=cfg.is_insecure_tls,
        )

        actor_provider = ActorTokenProvider(
            credentials=AgentCredentials(
                agent_id=cfg.orchestrator_agent.agent_id,
                agent_secret=cfg.orchestrator_agent.agent_secret,
                oauth_client_id=cfg.orchestrator_agent.oauth_client_id,
                oauth_client_secret=cfg.orchestrator_agent.oauth_client_secret,
                redirect_uri=cfg.mcp_redirect_uri,
            ),
            is_client=is_client,
        )

        pattern_c = PatternCExchanger(
            is_client=is_client,
            actor_token_provider=actor_provider,
            mcp_client_id=cfg.mcp_client_id,
            mcp_client_secret=cfg.mcp_client_secret,
            validator=validator_cfg,
            jwks_cache=jwks_cache,
        )

        # Re-include routers inside lifespan so pattern_c is available.
        # The routers have already been mounted; this call wires the deps
        # by reassigning the router factory closures.  Because FastAPI
        # routes capture state through the closure at include time, we
        # mount the routers BEFORE the lifespan yields.  This lifespan
        # therefore just manages the client lifecycles — the router dep
        # injection happens at app construction time below.
        #
        # NOTE: router factories close over their ``Deps`` dataclass which
        # we pass in during `create_app`.  The PatternCExchanger is the
        # only resource built inside lifespan, so we store a reference on
        # the deps object via a mutable container on app.state.
        _app.state.pattern_c = pattern_c
        _app.state.is_client = is_client

        logger.info(
            "orchestrator_startup | host=%s port=%d llm_fallback_mode=%s",
            cfg.host,
            cfg.port,
            cfg.llm_fallback_mode,
        )

        yield  # ── service is running ──

        # ── Shutdown ──────────────────────────────────────────────────────────
        logger.info("orchestrator_shutdown | closing httpx clients")
        await is_client.aclose()
        for agent_id, client in a2a_clients.items():
            await client.aclose()
            logger.debug("a2a_client_closed | agent_id=%s", agent_id)

    # ── FastAPI app ───────────────────────────────────────────────────────────

    app = FastAPI(title="Orchestrator", lifespan=lifespan)

    # ── CORS (must be added before CorrelationIdMiddleware so that preflight
    #    OPTIONS requests are served before the correlation middleware generates
    #    a spurious X-Request-ID warning).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ── CorrelationIdMiddleware LAST (prepended → executes FIRST per F-13) ────
    app.add_middleware(CorrelationIdMiddleware)

    # ── PatternCExchanger placeholder ─────────────────────────────────────────
    # The exchanger needs the event loop (it holds an asyncio.Lock via
    # ActorTokenProvider).  We build a sentinel here and replace it in the
    # lifespan above via app.state.  However, because router factories close
    # over their Deps at build time (not at call time) we need a late-binding
    # approach.
    #
    # Solution: use a one-element list as a mutable cell so the closure in
    # the auth router captures the cell rather than the value.  The lifespan
    # replaces the cell's first element before the first request arrives.
    _pattern_c_cell: list[PatternCExchanger | None] = [None]

    class _LateBindingPatternC:
        """Thin proxy that forwards all calls to the real PatternCExchanger.

        Constructed before the event loop starts; wired to the real instance
        in the lifespan after all async resources are available.

        This avoids an architecture where lifespan re-includes routers after
        startup, which is not supported by FastAPI.
        """

        async def exchange(
            self,
            *,
            code: str,
            code_verifier: str,
            redirect_uri: str,
        ):  # type: ignore[return]
            real = _pattern_c_cell[0]
            if real is None:
                raise RuntimeError(
                    "PatternCExchanger not yet initialised (lifespan has not started)"
                )
            return await real.exchange(
                code=code,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
            )

    proxy_pattern_c = _LateBindingPatternC()

    # Patch lifespan to wire the cell when the real exchanger is ready.
    original_lifespan = lifespan

    @asynccontextmanager
    async def _patching_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(_app):
            _pattern_c_cell[0] = _app.state.pattern_c
            yield

    # Re-assign the app lifespan with the patching wrapper.
    app.router.lifespan_context = _patching_lifespan

    # ── Auth router ───────────────────────────────────────────────────────────
    app.include_router(
        build_auth_router(
            AuthRouterDeps(
                config=cfg,
                pattern_c=proxy_pattern_c,  # type: ignore[arg-type]
                session_store=session_store,
            )
        )
    )

    # ── Chat router ───────────────────────────────────────────────────────────
    app.include_router(
        build_chat_router(
            ChatRouterDeps(
                config=cfg,
                session_store=session_store,
                keyword_router=keyword_router,
                agent_registry=agent_registry,
                a2a_clients=a2a_clients,
            )
        )
    )

    # ── SSE router ────────────────────────────────────────────────────────────
    app.include_router(
        build_sse_router(
            SseRouterDeps(session_store=session_store)
        )
    )

    # ── Health check ──────────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict:
        """Liveness probe — no auth, no secrets in the response body.

        Returns:
            ``{"ok": true, "service": "orchestrator"}``
        """
        return {"ok": True, "service": "orchestrator"}

    # ── SPA static mount (last so API routes take priority) ───────────────────
    _spa_dir = Path("/app/client_static")
    if _spa_dir.is_dir():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        @app.get("/", include_in_schema=False)
        async def spa_root() -> FileResponse:
            return FileResponse(_spa_dir / "index.html")

        app.mount(
            "/static",
            StaticFiles(directory=str(_spa_dir)),
            name="spa-static",
        )

        @app.get("/app.js", include_in_schema=False)
        async def spa_appjs() -> FileResponse:
            return FileResponse(_spa_dir / "app.js", media_type="application/javascript")

        @app.get("/styles.css", include_in_schema=False)
        async def spa_styles() -> FileResponse:
            return FileResponse(_spa_dir / "styles.css", media_type="text/css")

    return app


# ── Module-level ASGI app (used by plain ``uvicorn orchestrator.main:app``) ───
# Loaded lazily from env so imports in tests don't trigger env-var reads.


def main() -> None:
    """uvicorn entry-point: ``python -m orchestrator.main``.

    Reads the config from the environment, then starts uvicorn with
    ``factory=True`` so that each worker calls ``create_app()`` independently.
    """
    import uvicorn

    cfg = OrchestratorConfig.from_env()
    uvicorn.run(
        "orchestrator.main:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
