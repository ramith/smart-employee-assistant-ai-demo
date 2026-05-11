"""IT REST API — Sprint 4 S4.0 reconciliation.

Mounted alongside the IT MCP tool router in ``it_server/main.py``. Browser
SPA tokens (PKCE-issued, ``aud == SPA_CLIENT_ID``) and orchestrator-mediated
REST tokens (``aud == orchestrator MCP client_id``) land here; the strict
single-aud MCP-tool validator stays on ``it_server/auth/validators.py``.

S4.0 lands the validator + auth machinery + an empty router (only ``/health``).
Sprint 4 S4.5 lands C1 ``GET /api/reports/device-assignments`` (UC-16 IT leg).

Mirrors ``hr_server/rest_api/server.py`` for ``_AuthContext`` / ``_authenticate``
/ ``_require_scope`` shape; uses FastAPI ``APIRouter`` so the router slots
into ``main.py`` via ``app.include_router(...)``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from it_server.auth.jwt_validator import JWTValidator, TokenError
from it_server.service import it_service

logger = logging.getLogger(__name__)

__all__ = [
    "ITRestRouterDeps",
    "build_rest_router",
]


# ─── Dependency container ──────────────────────────────────────────────────


@dataclass
class ITRestRouterDeps:
    """Injected dependencies for the IT REST router.

    Attributes:
        validator: REST-path JWT validator (audience-list capable; built by
            ``it_server/auth/jwt_validator.build_validator_from_config``).
    """

    validator: JWTValidator


# ─── Authentication helpers ────────────────────────────────────────────────


class _AuthContext:
    """Resolved identity + scopes from a validated bearer token.

    Mirrors ``hr_server/rest_api/server.py:_AuthContext`` shape. ``username``
    and ``email`` (Sprint 4 identity claims) are surfaced for downstream
    handlers; control-char + length sanitisation happens upstream in
    ``common/auth/jwt_validator._sanitise_user_string`` (security audit F-03).
    """

    def __init__(self, payload: dict):
        self.payload = payload
        self.sub: str = payload.get("sub") or ""
        self.scopes: list[str] = (
            payload.get("scope", "").split() if payload.get("scope") else []
        )

        first = payload.get("given_name") or ""
        last = payload.get("last_name") or ""
        if not first and not last:
            full = payload.get("name") or payload.get("preferred_username") or ""
            if full:
                parts = full.split(" ", 1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ""
        self.first_name = first
        self.last_name = last
        self.full_name = f"{first} {last}".strip() or "User"

        # Sprint 4: identity surfaces for username/email-keyed business logic.
        # Control-char / length sanitisation happens earlier in the common
        # JWT validator (security audit F-03); REST validator path uses
        # raw payload, so apply minimal defensive defaults here.
        self.username: str | None = payload.get("username") or None
        self.email: str | None = payload.get("email") or None


async def _authenticate(
    request: Request, validator: JWTValidator
) -> _AuthContext | JSONResponse:
    """Validate the Authorization header. Returns ``_AuthContext`` or an error response.

    Mirrors ``hr_server/rest_api/server.py:_authenticate`` shape with the
    validator passed in (FastAPI dependency-injection-friendly) rather than
    module-level.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        logger.warning(
            "[REST AUTH FAIL] path=%s reason=missing_token", request.url.path
        )
        return JSONResponse(
            {
                "error": "missing_token",
                "message": "Missing or invalid Authorization header",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    token = auth_header[7:]
    try:
        payload = await validator.validate_token(token)
    except TokenError as e:
        logger.warning(
            "[REST AUTH FAIL] path=%s reason=%s message=%s",
            request.url.path,
            e.error_type,
            e.message,
        )
        return JSONResponse(
            {"error": e.error_type, "message": e.message},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    ctx = _AuthContext(payload)

    act = payload.get("act")
    endpoint = request.url.path
    scope_str = ", ".join(ctx.scopes) if ctx.scopes else "(none)"
    if act:
        actor_sub = act.get("sub") if isinstance(act, dict) else str(act)
        logger.info(
            "[REST %s >> OBO Token] user(sub)=%s | name=%s | agent(act.sub)=%s | scopes=%s",
            endpoint,
            ctx.sub,
            ctx.full_name,
            actor_sub,
            scope_str,
        )
    else:
        logger.info(
            "[REST %s >> User Token] sub=%s | name=%s | scopes=%s",
            endpoint,
            ctx.sub,
            ctx.full_name,
            scope_str,
        )

    return ctx


def _require_scope(ctx: _AuthContext, *any_of: str) -> JSONResponse | None:
    """Return a 403 response if the caller has none of the listed scopes.

    Mirrors ``hr_server/rest_api/server.py:_require_scope``.
    """
    if not any(s in ctx.scopes for s in any_of):
        logger.warning(
            "[REST SCOPE DENIED] sub=%s name=%s required=%s present=%s",
            ctx.sub,
            ctx.full_name,
            list(any_of),
            ctx.scopes,
        )
        return JSONResponse(
            {
                "error": "insufficient_scope",
                "message": f"This action requires one of: {', '.join(any_of)}",
                "required_scope": list(any_of),
                "available_scopes": ctx.scopes,
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return None


# ─── Router factory ─────────────────────────────────────────────────────────


def build_rest_router(deps: ITRestRouterDeps) -> APIRouter:
    """Return a FastAPI ``APIRouter`` carrying the IT REST surface.

    S4.0 ships the auth machinery + a ``/health`` ping. Business endpoints
    (E1 / C1) land in subsequent slices and reuse ``_authenticate(request,
    deps.validator)`` + ``_require_scope(ctx, ...)`` from this module.
    """
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, object]:
        """Unauthenticated REST liveness probe.

        Distinct from the FastAPI app-level ``/healthz`` — kept under the REST
        router so a future operator who probes the REST surface gets a
        per-router signal.
        """
        return {"status": "ok"}

    @router.get("/api/reports/device-assignments")
    async def get_device_assignments(request: Request):
        """Sprint 4 S4.5 (UC-16 C1) — Device (IT asset) assignments report.

        Bearer token-A; scope ``it_assets_read_rest`` (existing; HR Admin
        role already holds it per ``docs/scope-policy.md``). Calls
        ``it_service.get_all_asset_assignments()`` which projects each row to
        ``{username, email, asset_id, type, model, status}``. ``sub`` is
        *never* surfaced (sprint-4.md §7 identity model).

        Response envelope (Stage 5 §5): ``{data: [...], count: N}``.
        """
        ctx = await _authenticate(request, deps.validator)
        if isinstance(ctx, JSONResponse):
            return ctx
        err = _require_scope(ctx, "it_assets_read_rest")
        if err:
            return err
        rows = it_service.get_all_asset_assignments()
        return JSONResponse({"data": rows, "count": len(rows)})

    @router.get("/api/me/assets")
    async def get_my_assets(request: Request):
        """Return the authenticated user's own IT assets.

        Sprint 4 (UC-12 / sidebar). Bearer token-A; scope
        ``it_assets_self_rest``. Identity is resolved from ``sub`` (the only
        claim OBO tokens reliably carry) with the ``username`` profile claim
        as a fallback. Returns ``{assets: [...], total: N}`` (it_service shape).
        """
        ctx = await _authenticate(request, deps.validator)
        if isinstance(ctx, JSONResponse):
            return ctx
        err = _require_scope(ctx, "it_assets_self_rest")
        if err:
            return err
        if not ctx.sub:
            return JSONResponse(
                {"error_id": "ERR-AUTH-claim-missing", "detail": "sub claim absent"},
                status_code=401,
            )
        return JSONResponse(
            it_service.get_my_assets(ctx.username or "", sub=ctx.sub)
        )

    return router
