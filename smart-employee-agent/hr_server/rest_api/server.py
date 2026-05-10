"""
 Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.

  HR REST API

  Sprint 4 S4.0 Track B reconciliation: this module was orphaned at sprint
  start (`main.py` only mounted `mcp/tools.py`). It is now wired via
  `build_rest_router(deps)` so the SPA-facing REST surfaces — `/api/me/leaves`
  (S4.3), `/api/reports/...` (S4.4 / S4.5), approve/reject (S4.4) — land on a
  router that's already authenticated and store-backed.

  Endpoints (existing handlers preserved from the orphan; B1/B2 new endpoints
  arrive in S4.3+):
    GET  /api/holidays               (hr_basic_rest)
    GET  /api/leave-policy           (hr_basic_rest)
    GET  /api/leave-balance          (hr_self_rest)
    GET  /api/leaves                 (hr_self_rest | hr_read_rest)
    GET  /api/leaves/{id}            (hr_self_rest for own | hr_read_rest)
    POST /api/leaves                 (hr_self_rest)
    POST /api/leaves/{id}/approve    (hr_approve_rest)
    POST /api/leaves/{id}/reject     (hr_approve_rest)
    POST /reset                      (hr_approve_rest | hr_approve_mcp)

  S4.0 wiring contract: `main.py` builds a single ``JWTValidator`` instance
  (with audience cap enforced) and passes it via ``RestApiDeps``. Module-level
  validator construction is gone — all auth runs through ``deps.validator``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from hr_server.auth.jwt_validator import JWTValidator, TokenError
from hr_server.service import hr_service, store

logger = logging.getLogger(__name__)


# ─── Dependency container ──────────────────────────────────────────────────


@dataclass
class RestApiDeps:
    """Injected dependencies for the HR REST router.

    Attributes:
        validator: REST-path JWTValidator. Distinct from the MCP-tool
            validator; accepts an audience LIST (capped at 3 entries by
            ``main.py``) so token-A from the SPA AND OBO tokens from the
            orchestrator both validate. The MCP-tool validator stays strict.
    """

    validator: JWTValidator


# ─── Authentication helpers (closure-bound to deps) ────────────────────────


class _AuthContext:
    """Resolved identity + scopes from a validated bearer token."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.sub: str = payload.get("sub") or ""
        self.scopes: list[str] = (
            payload.get("scope", "").split() if payload.get("scope") else []
        )

        first = payload.get("given_name") or ""
        last = payload.get("last_name") or ""
        if not first and not last:
            full = (
                payload.get("username")
                or payload.get("name")
                or payload.get("preferred_username")
                or ""
            )
            if full:
                parts = full.split(" ", 1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ""
        self.first_name = first
        self.last_name = last
        self.full_name = f"{first} {last}".strip() or "User"


def _make_authenticate(deps: RestApiDeps):
    """Return an `_authenticate(request)` closure bound to the deps validator."""

    async def _authenticate(request: Request):  # type: ignore[no-untyped-def]
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(
                "[REST AUTH FAIL] path=%s reason=missing_token", request.url.path
            )
            return JSONResponse(
                {"error": "missing_token", "message": "Missing or invalid Authorization header"},
                status_code=401,
            )
        token = auth_header[7:]
        try:
            payload = await deps.validator.validate_token(token)
        except TokenError as e:
            logger.warning(
                "[REST AUTH FAIL] path=%s reason=%s message=%s",
                request.url.path,
                e.error_type,
                e.message,
            )
            return JSONResponse(
                {"error": e.error_type, "message": e.message}, status_code=401
            )

        ctx = _AuthContext(payload)
        if ctx.sub and ctx.first_name:
            store.ensure_user(ctx.sub, ctx.first_name, ctx.last_name)

        act = payload.get("act")
        endpoint = request.url.path
        scope_str = ", ".join(ctx.scopes) if ctx.scopes else "(none)"
        if act:
            actor_sub = act.get("sub") if isinstance(act, dict) else str(act)
            logger.info(
                "[REST %s >> OBO Token] user(sub)=%s | name=%s | agent(act.sub)=%s | scopes=%s",
                endpoint, ctx.sub, ctx.full_name, actor_sub, scope_str,
            )
        else:
            logger.info(
                "[REST %s >> User Token] sub=%s | name=%s | scopes=%s",
                endpoint, ctx.sub, ctx.full_name, scope_str,
            )

        return ctx

    return _authenticate


def _require_scope(ctx: _AuthContext, *any_of: str):
    """Return a 403 response if the caller has none of the listed scopes."""
    if not any(s in ctx.scopes for s in any_of):
        logger.warning(
            "[REST SCOPE DENIED] sub=%s name=%s required=%s present=%s",
            ctx.sub, ctx.full_name, list(any_of), ctx.scopes,
        )
        return JSONResponse(
            {
                "error": "insufficient_scope",
                "message": f"This action requires one of: {', '.join(any_of)}",
                "required_scope": list(any_of),
                "available_scopes": ctx.scopes,
            },
            status_code=403,
        )
    return None


# ─── Router factory ─────────────────────────────────────────────────────────


def build_rest_router(deps: RestApiDeps) -> APIRouter:
    """Build the FastAPI router carrying the REST surfaces.

    S4.0 contract: returns a router that's safe to mount under ``""`` even when
    no business endpoints are registered yet (S4.3+ add them). Today we mount
    the existing handlers preserved from the orphan module so the manual gate
    can exercise the auth wiring against live IS.

    Args:
        deps: Dependency container holding the audience-list-aware validator.

    Returns:
        An ``APIRouter`` ready for ``app.include_router(router)``.
    """
    router = APIRouter()
    authenticate = _make_authenticate(deps)

    # ── Read-only handlers ────────────────────────────────────────────────

    @router.get("/api/holidays")
    async def get_holidays(request: Request):
        ctx = await authenticate(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        err = _require_scope(ctx, "hr_basic_rest")
        if err:
            return err
        return JSONResponse({"holidays": await hr_service.get_holidays()})

    @router.get("/api/leave-policy")
    async def get_leave_policy(request: Request):
        ctx = await authenticate(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        err = _require_scope(ctx, "hr_basic_rest")
        if err:
            return err
        return JSONResponse({"leave_types": await hr_service.get_leave_policy()})

    @router.get("/api/leave-balance")
    async def get_leave_balance(request: Request):
        ctx = await authenticate(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        err = _require_scope(ctx, "hr_self_rest")
        if err:
            return err
        return JSONResponse(
            await hr_service.get_my_leave_balance(ctx.sub, ctx.first_name, ctx.last_name)
        )

    @router.get("/api/leaves")
    async def get_leaves(request: Request):
        ctx = await authenticate(request)
        if isinstance(ctx, JSONResponse):
            return ctx

        if "hr_read_rest" in ctx.scopes:
            leaves = await hr_service.get_leaves_for_dashboard(
                status=request.query_params.get("status"),
                employee_name=request.query_params.get("employee_name"),
            )
            return JSONResponse({"leaves": leaves})

        if "hr_self_rest" in ctx.scopes and ctx.sub:
            leaves = await hr_service.get_leaves_for_dashboard(user_sub=ctx.sub)
            return JSONResponse({"leaves": leaves})

        logger.warning(
            "[REST SCOPE DENIED] sub=%s name=%s required=hr_self_rest|hr_read_rest present=%s",
            ctx.sub, ctx.full_name, ctx.scopes,
        )
        return JSONResponse(
            {"error": "insufficient_scope", "message": "Requires hr_self_rest or hr_read_rest scope."},
            status_code=403,
        )

    @router.get("/api/leaves/{request_id}")
    async def get_leave_details(request: Request):
        ctx = await authenticate(request)
        if isinstance(ctx, JSONResponse):
            return ctx

        request_id = request.path_params["request_id"]
        details = await hr_service.get_leave_request_details(request_id)
        if not details:
            return JSONResponse(
                {"error": "not_found", "message": f"Leave request '{request_id}' not found."},
                status_code=404,
            )

        if "hr_read_rest" in ctx.scopes:
            return JSONResponse(details)
        if "hr_self_rest" in ctx.scopes and ctx.sub:
            owner_sub = store.leave_requests.get(request_id, {}).get("user_sub")
            if owner_sub == ctx.sub:
                return JSONResponse(details)
            logger.warning(
                "[REST FORBIDDEN] sub=%s tried to access leave %s owned by %s",
                ctx.sub, request_id, owner_sub,
            )
            return JSONResponse(
                {"error": "forbidden", "message": "You can only view your own leave requests."},
                status_code=403,
            )
        logger.warning(
            "[REST SCOPE DENIED] sub=%s name=%s required=hr_self_rest|hr_read_rest present=%s",
            ctx.sub, ctx.full_name, ctx.scopes,
        )
        return JSONResponse(
            {"error": "insufficient_scope", "message": "Requires hr_self_rest or hr_read_rest scope."},
            status_code=403,
        )

    # ── Write handlers ────────────────────────────────────────────────────

    @router.post("/api/leaves")
    async def create_leave(request: Request):
        ctx = await authenticate(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        err = _require_scope(ctx, "hr_self_rest")
        if err:
            return err

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "invalid_body", "message": "Body must be valid JSON."},
                status_code=400,
            )

        leave_type = (body.get("leave_type") or body.get("type") or "").strip()
        start_date = (body.get("start_date") or "").strip()
        end_date = (body.get("end_date") or "").strip()
        reason = (body.get("reason") or "").strip()

        missing = [k for k, v in {
            "leave_type": leave_type, "start_date": start_date,
            "end_date": end_date, "reason": reason,
        }.items() if not v]
        if missing:
            return JSONResponse(
                {"error": "missing_fields", "message": f"Required fields missing: {', '.join(missing)}"},
                status_code=400,
            )

        result = await hr_service.apply_leave(
            ctx.sub, ctx.first_name, ctx.last_name,
            leave_type, start_date, end_date, reason,
        )
        status_code = 201 if result.get("success") else 400
        return JSONResponse(result, status_code=status_code)

    @router.post("/api/leaves/{request_id}/approve")
    async def approve_leave(request: Request):
        ctx = await authenticate(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        err = _require_scope(ctx, "hr_approve_rest")
        if err:
            return err

        request_id = request.path_params["request_id"]
        result = await hr_service.approve_leave_request(request_id, ctx.sub, ctx.full_name)
        if result.get("success"):
            logger.info("[AUDIT] Leave %s approved (reviewer_sub=%s)", request_id, ctx.sub)
            return JSONResponse(result)
        status_code = 404 if result.get("error") == "not_found" else 400
        return JSONResponse(result, status_code=status_code)

    @router.post("/api/leaves/{request_id}/reject")
    async def reject_leave(request: Request):
        ctx = await authenticate(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        err = _require_scope(ctx, "hr_approve_rest")
        if err:
            return err

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "invalid_body", "message": "Body must be valid JSON."},
                status_code=400,
            )
        reason = (body.get("reason") or "").strip()
        if not reason:
            return JSONResponse(
                {"error": "missing_fields", "message": "A non-empty 'reason' is required."},
                status_code=400,
            )

        request_id = request.path_params["request_id"]
        result = await hr_service.reject_leave_request(
            request_id, reason, ctx.sub, ctx.full_name
        )
        if result.get("success"):
            logger.info("[AUDIT] Leave %s rejected (reviewer_sub=%s)", request_id, ctx.sub)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[AUDIT-DETAIL] Leave %s rejection reason: %s", request_id, reason
                )
            return JSONResponse(result)
        status_code = 404 if result.get("error") == "not_found" else 400
        return JSONResponse(result, status_code=status_code)

    @router.post("/reset")
    async def reset(request: Request):
        ctx = await authenticate(request)
        if isinstance(ctx, JSONResponse):
            return ctx
        err = _require_scope(ctx, "hr_approve_rest", "hr_approve_mcp")
        if err:
            return err
        store.reset_data()
        return JSONResponse(
            {"success": True, "message": "HR data reset to default state."}
        )

    return router
