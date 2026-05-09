"""HR-server FastAPI application entry point — Sprint 1 Wave 8.

Wires together:
  - ``HRServerConfig``       (Wave 4) — env-var driven frozen config.
  - ``HRServerTokenValidator`` (Wave 5) — F-04 six-step token validation.
  - ``build_hr_mcp_router``  (Wave 6) — three MCP tool endpoints.
  - ``CorrelationIdMiddleware`` / ``install_logging`` (common) — F-13 / F-16.
  - ``RedactionFilter``      (common) — F-11 log redaction.

Route inventory (all under /mcp/tools/):
  POST /mcp/tools/get_leave_balance    scope: hr.read
  POST /mcp/tools/get_leave_history    scope: hr.read
  POST /mcp/tools/approve_leave        scope: hr.write
  GET  /healthz                        unauthenticated liveness probe

F-15 / N28: ``validator.log_startup_assertion()`` fires during ``create_app()``
so the ``expected_aud`` value is visible in the startup log before any token
arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from common.logging.correlation import CorrelationIdMiddleware, install_logging
from common.logging.redaction import RedactionFilter
from common.revocation import RevocationState, build_internal_events_router
from hr_server.auth.validators import HRServerTokenValidator
from hr_server.config import HRServerConfig
from hr_server.mcp.tools import HRMcpToolRouterDeps, build_hr_mcp_router

__all__ = ["create_app", "main"]


def create_app(config: HRServerConfig | None = None) -> FastAPI:
    """Build and return the hr_server FastAPI application.

    Idempotent — safe to call multiple times (e.g. in tests with different
    ``config`` objects).  Each call returns a fresh ``FastAPI`` instance.

    Args:
        config: Optional pre-built config.  When ``None`` (production default),
            ``HRServerConfig.from_env()`` is called and the N28 startup log is
            emitted by the config constructor.

    Returns:
        A fully wired ``FastAPI`` application ready to serve via uvicorn.
    """
    cfg: HRServerConfig = config if config is not None else HRServerConfig.from_env()

    # 3A.2 BLOCK-I: single-worker invariant.
    workers = int(os.getenv("UVICORN_WORKERS", "1"))
    assert workers == 1, (
        f"hr_server requires UVICORN_WORKERS=1 (got {workers}). "
        "Multi-worker support requires Redis-backed denylist (Sprint 4+)."
    )

    # Configure the root logger exactly once per process.  Idempotent.
    install_logging(level="INFO")
    logging.getLogger().addFilter(RedactionFilter())

    # Build validator; emit F-15 startup log (expected_aud + trusted_act_subs).
    validator = HRServerTokenValidator.from_config(cfg)
    validator.log_startup_assertion()  # F-15 / N28

    # 3A.2: revocation state. Validator wires this in 3A.3 to enforce
    # denylist on /mcp/tools/* requests; for now the receiver populates it
    # so by 3A.3 the cache is non-empty when validators get the new check.
    revocation = RevocationState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        # Mid-sprint fix #3 (2026-05-09): pre-warm the JWKS cache so the first
        # token validation doesn't pay the ~800 ms IS round-trip. Best-effort
        # — non-fatal if IS is briefly unreachable.
        await validator.prewarm_jwks()

        sweep_task = asyncio.create_task(revocation.revoked_jtis.sweep_loop())
        revocation.sweep_task = sweep_task
        yield
        sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep_task

    app = FastAPI(
        title="HR Server",
        description="MCP tool server exposing HR leave management operations.",
        lifespan=lifespan,
    )

    # F-13 correlation middleware — must be added BEFORE routes so every
    # response (including 4xx from tool handlers) carries X-Request-ID.
    app.add_middleware(CorrelationIdMiddleware)

    # Mount the three HR MCP tool routes under /mcp/tools/.
    app.include_router(
        build_hr_mcp_router(HRMcpToolRouterDeps(validator=validator)),
        prefix="/mcp/tools",
    )

    # 3A.2: /internal/events receiver. Servers don't need a per-jti cache
    # eviction callback (no _CachedToken on the server), so on_revoke=None.
    # FIX-2 (mid-sprint review): WARN on empty secret.
    secret = getattr(cfg, "internal_revoke_shared_secret", "")
    if secret:
        app.include_router(
            build_internal_events_router(
                state=revocation,
                shared_secret=secret,
                on_revoke=None,
                service_label="hr-server",
            )
        )
    else:
        logging.getLogger(__name__).warning(
            "internal_events_receiver_disabled | service=hr-server reason=no_shared_secret"
        )

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, object]:
        """Unauthenticated liveness probe.

        Returns:
            ``{"ok": True, "service": "hr_server"}``
        """
        return {"ok": True, "service": "hr_server"}

    return app


def main() -> None:
    """Entry-point when the module is invoked directly or via ``python -m``.

    Reads config from the environment and starts a uvicorn server using the
    factory pattern so uvicorn manages the ``FastAPI`` application lifecycle.
    """
    import uvicorn

    cfg = HRServerConfig.from_env()
    uvicorn.run(
        "hr_server.main:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
    )


if __name__ == "__main__":
    main()
