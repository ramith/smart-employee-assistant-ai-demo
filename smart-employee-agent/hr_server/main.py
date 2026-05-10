"""HR-server FastAPI application entry point — Sprint 1 Wave 8 / Sprint 4 S4.0.

Wires together:
  - ``HRServerConfig``       (Wave 4) — env-var driven frozen config.
  - ``HRServerTokenValidator`` (Wave 5) — F-04 six-step token validation
    (MCP-tool path; strict single-aud).
  - ``JWTValidator``         (Sprint 4 S4.0 Track B) — REST-path validator
    that accepts a configurable audience list (capped at <=3 per security
    audit F-01); used by ``/api/...`` endpoints from the SPA + reporting
    proxies.
  - ``build_hr_mcp_router``  (Wave 6) — three MCP tool endpoints.
  - ``build_rest_router``    (Sprint 4 S4.0 Track B) — REST surfaces.
  - ``CorrelationIdMiddleware`` / ``install_logging`` (common) — F-13 / F-16.
  - ``RedactionFilter``      (common) — F-11 log redaction.

Route inventory (all under /mcp/tools/):
  POST /mcp/tools/get_leave_balance    scope: hr_self_rest
  POST /mcp/tools/get_leave_history    scope: hr_self_rest
  POST /mcp/tools/approve_leave        scope: hr_approve_rest
  GET  /healthz                        unauthenticated liveness probe

REST surfaces (delivered by ``rest_api.server.build_rest_router``):
  GET  /api/holidays                   scope: hr_basic_rest
  GET  /api/leave-policy               scope: hr_basic_rest
  GET  /api/leave-balance              scope: hr_self_rest
  GET  /api/leaves                     scope: hr_self_rest|hr_read_rest
  GET  /api/leaves/{id}                scope: hr_self_rest|hr_read_rest
  POST /api/leaves                     scope: hr_self_rest
  POST /api/leaves/{id}/approve        scope: hr_approve_rest
  POST /api/leaves/{id}/reject         scope: hr_approve_rest
  POST /reset                          scope: hr_approve_rest|hr_approve_mcp

F-15 / N28: ``validator.log_startup_assertion()`` fires during ``create_app()``
so the ``expected_aud`` value is visible in the startup log before any token
arrives. Sprint 4 S4.0 adds a parallel REST validator startup log enumerating
the configured audience list (security audit F-01 transparency).
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
from hr_server.auth.jwt_validator import JWTValidator
from hr_server.auth.validators import HRServerTokenValidator
from hr_server.config import HRServerConfig
from hr_server.mcp.tools import HRMcpToolRouterDeps, build_hr_mcp_router
from hr_server.rest_api.server import RestApiDeps, build_rest_router

__all__ = ["create_app", "main"]

# Sprint 4 S4.0 (Track B, security audit F-01): the REST validator's audience
# list is capped at this many entries. Three is enough for the demo's
# expected pattern: HR-server's own client ID + orchestrator client ID + SPA
# client ID. Anything beyond that is a misconfiguration and is failed-closed.
_MAX_REST_AUDIENCES = 3


def _resolve_rest_audiences(cfg: HRServerConfig, environ: dict[str, str] | None = None) -> list[str]:
    """Build the audience list for the REST validator with cap enforcement.

    Reads ``HR_SERVER_REST_VALID_AUDIENCES`` (comma-separated). Default is
    ``[cfg.expected_aud]``. Extra entries are appended in order; duplicates
    against ``cfg.expected_aud`` are de-duplicated. The list is capped at
    ``_MAX_REST_AUDIENCES`` — exceeding the cap raises at startup so a
    misconfigured env var can never silently widen token acceptance.

    Args:
        cfg: Validated HR Server config (gives ``expected_aud`` floor).
        environ: Optional override for tests; falls back to ``os.environ``.

    Returns:
        The deduplicated, capped audience list ready for ``JWTValidator``.

    Raises:
        ValueError: When the resolved list exceeds the cap (F-01).
    """
    env = environ if environ is not None else dict(os.environ)
    raw = env.get("HR_SERVER_REST_VALID_AUDIENCES", "").strip()
    extras = [piece.strip() for piece in raw.split(",") if piece.strip()] if raw else []

    audiences: list[str] = [cfg.expected_aud]
    for extra in extras:
        if extra not in audiences:
            audiences.append(extra)

    if len(audiences) > _MAX_REST_AUDIENCES:
        # F-01 fail-closed: log + raise. The startup log surface this as ERROR
        # so SIEM has a single grep target.
        logging.getLogger(__name__).error(
            "rest_validator.startup audience_cap_exceeded count=%d cap=%d audiences=%r",
            len(audiences),
            _MAX_REST_AUDIENCES,
            audiences,
        )
        raise ValueError(
            f"HR_SERVER_REST_VALID_AUDIENCES would yield {len(audiences)} audiences "
            f"(cap={_MAX_REST_AUDIENCES}). Refuse to start. "
            "Reduce the env-var entries; security audit F-01 caps the list."
        )

    return audiences


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

    # Build validator. Startup assertion is deferred until AFTER
    # ``attach_revocation()`` below so the F-15 line carries
    # ``denylist_enforcement=on`` and the absence-warning fires here only if
    # the wiring was somehow skipped.
    validator = HRServerTokenValidator.from_config(cfg)

    # 3A.2/3A.3: revocation state. The receiver (mounted below) populates
    # the denylist via /internal/events fan-out from the orchestrator's
    # logout cascade. The validator (Sprint 3 3A.3) consults the same
    # denylist on every MCP tool call as Step 7 of validate_token().
    revocation = RevocationState()
    validator.attach_revocation(revocation)

    # F-15 / N28 — emitted now so denylist_enforcement is captured. SIEM grep
    # target: "denylist_enforcement=off" → wiring regression.
    validator.log_startup_assertion()

    # ── Sprint 4 S4.0 Track B: REST validator with audience-list cap. ────
    rest_audiences = _resolve_rest_audiences(cfg)
    rest_validator = JWTValidator(
        jwks_url=cfg.is_jwks_url,
        issuer=cfg.is_issuer,
        audience=rest_audiences,
        ssl_verify=not cfg.is_insecure_tls,
    )
    # F-01 transparency: emit one INFO line enumerating every accepted
    # audience. SIEM grep target: "rest_validator.startup expected_audiences=".
    logging.getLogger(__name__).info(
        "rest_validator.startup expected_audiences=%r count=%d cap=%d",
        rest_audiences,
        len(rest_audiences),
        _MAX_REST_AUDIENCES,
    )

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

    # Sprint 4 S4.0 Track B: mount the REST surfaces. The router uses the
    # audience-cap-aware validator so the SPA/orchestrator can call us with
    # token-A or OBO tokens. Strict-aud MCP path stays on /mcp/tools/.
    app.include_router(build_rest_router(RestApiDeps(validator=rest_validator)))

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
