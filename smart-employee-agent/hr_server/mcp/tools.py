"""HR-server MCP tool endpoints — Sprint 1 Wave 6 / Sprint 4 S4.0 (Track B).

Exposes three FastAPI POST endpoints under ``/mcp/tools/``:

    POST /mcp/tools/get_leave_balance   scope: hr_self_rest
    POST /mcp/tools/get_leave_history   scope: hr_self_rest
    POST /mcp/tools/approve_leave       scope: hr_approve_rest

Sprint 4 S4.0 reconciliation (D1): handlers now delegate to ``hr_service``
(the canonical in-memory implementation backed by ``service/store.py``)
instead of returning the Sprint-1 canned dicts. The ``_CANNED_*`` constants
have been removed; ``hr_service.ensure_user`` auto-registers users on first
call so the demo's "single-user" assumption keeps holding.

Each handler:
  1. Extracts a Bearer token from the ``Authorization`` header.
  2. Reads the ``X-Request-ID`` correlation id (set by ``CorrelationIdMiddleware``
     or passed directly from the caller; falls back to ``get_request_id()``).
  3. Calls ``deps.validator.validate_token(jwt, required_scopes=...)`` which runs
     the full F-04 six-step check (sig, iss, exp, aud, act.sub, scope).
  4. On ``JWTValidationError``, ``PeerTrustError``, or ``ScopeError``: raises
     ``HTTPException(401)`` whose ``detail`` dict is ``{"error_id": ..., "request_id": ...}``.
  5. On success: delegates to ``hr_service`` using the now-Sprint-4-plumbed
     ``claims.username`` (Track A) — falling back to ``claims.sub`` prefix when
     username is absent (e.g. system tokens).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from common.auth.errors import JWTValidationError, PeerTrustError, ScopeError
from common.logging.correlation import get_request_id

# Import validator using a guard so the module loads cleanly in both the
# production path (hr_server package) and the test path (importlib-loaded).
try:
    from hr_server.auth.validators import HRServerTokenValidator
    from hr_server.config import HRServerConfig
except ModuleNotFoundError:
    # Tests load this module directly via importlib; the package shim may not
    # match the hyphenated filesystem path.  The test file injects the
    # validator via HRMcpToolRouterDeps, so these imports are unused.
    HRServerTokenValidator = None  # type: ignore[assignment,misc]
    HRServerConfig = None  # type: ignore[assignment,misc]

# Lazy-import the service so the module loads even when service/__init__.py
# (or the in-memory store's `from hr_server.service import store` chain) is
# stubbed out in test fixtures. Tests that exercise full handler bodies will
# import the real service via the fixture path; lightweight tests that only
# care about scope-guard behaviour can stub `hr_service` in sys.modules.
try:
    from hr_server.service import hr_service
except ModuleNotFoundError:
    hr_service = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "HRMcpToolRouterDeps",
    "build_hr_mcp_router",
    # Pydantic models (re-exported so tests can import them directly)
    "GetLeaveBalanceArgs",
    "LeaveBalanceResult",
    "LeaveBalanceBuckets",
    "GetLeaveHistoryArgs",
    "LeaveHistoryEntry",
    "GetLeaveHistoryResult",
    "ApproveLeaveArgs",
    "ApproveLeaveResult",
]


# ---------------------------------------------------------------------------
# Pydantic request / response models — Sprint 4 reshape (D1, RR-1)
# ---------------------------------------------------------------------------


class GetLeaveBalanceArgs(BaseModel):
    """Request body for ``get_leave_balance``.

    ``employee_id`` is accepted for legacy compatibility but is no longer
    consulted — the service always keys on ``claims.sub``. Sprint 5 will drop
    the field entirely once chat-side callers stop sending it.
    """

    employee_id: str | None = Field(default=None, description="Legacy; ignored.")


class LeaveBalanceBuckets(BaseModel):
    """Per-leave-type day balances. Mirrors ``hr_service.get_my_leave_balance``."""

    annual: int
    sick: int
    personal: int


class LeaveBalanceResult(BaseModel):
    """Response for ``get_leave_balance`` (Sprint 4 shape).

    Returned by ``hr_service.get_my_leave_balance`` after store.ensure_user
    auto-registration. The legacy single-int ``leave_days`` shape was dropped
    in S4.0 — see docs/architecture/sprint-4-stage-6.5-reconciliation.md §1.6.
    """

    employee: str
    balance: LeaveBalanceBuckets
    as_of_date: str = Field(default_factory=lambda: str(date.today()))


class GetLeaveHistoryArgs(BaseModel):
    """Request body for ``get_leave_history``."""

    employee_id: str | None = Field(default=None, description="Legacy; ignored.")
    limit: int = Field(default=10, ge=1, le=50)


class LeaveHistoryEntry(BaseModel):
    """A single leave record (Sprint 4 shape — matches hr_service projection)."""

    request_id: str
    type: str
    start_date: str
    end_date: str
    days_requested: int
    status: str
    reason: str = ""


class GetLeaveHistoryResult(BaseModel):
    """Response for ``get_leave_history``."""

    employee_id: str
    entries: list[LeaveHistoryEntry]


class ApproveLeaveArgs(BaseModel):
    """Request body for ``approve_leave``."""

    leave_id: str = Field(description="Maps to hr_service request_id.")


class ApproveLeaveResult(BaseModel):
    """Response for ``approve_leave``.

    Mirrors ``hr_service.approve_leave_request`` happy-path response. ``error``/
    ``message`` are populated for service-level rejections (insufficient
    balance, not-found, already approved); HTTP status stays 200 in those
    cases since the auth/scope checks succeeded — the rejection is business
    rather than security.
    """

    success: bool = True
    request_id: str
    new_status: str | None = None
    employee: str | None = None
    notification: str | None = None
    error: str | None = None
    message: str | None = None
    approved_by: str | None = None


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------


@dataclass
class HRMcpToolRouterDeps:
    """Injected dependencies for the HR MCP tool router.

    Attributes:
        validator: Wave 5 token validator that enforces the F-04 six-step check.
    """

    validator: HRServerTokenValidator  # type: ignore[valid-type]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_bearer(request: Request) -> str | None:
    """Return the raw JWT string from ``Authorization: Bearer <token>``.

    Returns:
        The token string, or ``None`` if the header is absent or malformed.
    """
    auth_header: str | None = request.headers.get("Authorization")
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _get_rid(request: Request) -> str:
    """Return the request-id for error bodies.

    Prefers the ``X-Request-ID`` header value (set by ``CorrelationIdMiddleware``
    before we arrive here).  Falls back to the ContextVar value, then empty
    string.
    """
    return (
        request.headers.get("X-Request-ID")
        or get_request_id()
        or ""
    )


def _username_for(claims) -> str:  # type: ignore[no-untyped-def]
    """Resolve a display first-name from the verified token claims.

    Sprint 4 plumbs ``username`` (Track A) through the JWT model. When absent —
    e.g. internal/system tokens or legacy fixtures — fall back to the prefix
    of ``sub`` so ``hr_service.ensure_user`` still gets a non-empty name. The
    fallback is intentionally lossy; production tokens always carry username.
    """
    name = getattr(claims, "username", None)
    if name:
        return name
    raw_sub = getattr(claims, "sub", "") or ""
    return raw_sub.split("@", 1)[0] or "user"


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_hr_mcp_router(deps: HRMcpToolRouterDeps) -> APIRouter:
    """Return a FastAPI ``APIRouter`` with three HR tool endpoints.

    All endpoints are mounted under the prefix supplied by the caller (typically
    ``/mcp/tools``).  Each handler validates the inbound token via
    ``deps.validator.validate_token()`` before delegating to ``hr_service``.
    """
    router = APIRouter()

    # ── get_leave_balance ─────────────────────────────────────────────────────

    @router.post("/get_leave_balance", response_model=LeaveBalanceResult)
    async def get_leave_balance(
        body: GetLeaveBalanceArgs,  # noqa: ARG001 — kept for client compat
        request: Request,
    ) -> LeaveBalanceResult:
        """Return the caller's current leave balance (per token.sub)."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("get_leave_balance missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        logger.debug(
            "get_leave_balance tool_entry rid=%s required_scopes=%s",
            rid,
            ["hr_self_rest"],
        )

        try:
            claims = await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_self_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "get_leave_balance token validation failed error_id=%s rid=%s reason=%r details=%s",
                exc.error_id,
                rid,
                str(exc),
                getattr(exc, "details", None),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        logger.debug(
            "get_leave_balance validation_ok rid=%s sub=%s jti=%s",
            rid,
            claims.sub,
            claims.jti,
        )

        # S4.0 D1: delegate to hr_service. The "" last_name keeps the existing
        # service signature compat (full_name = "<first> ".strip()).
        result = await hr_service.get_my_leave_balance(
            claims.sub, _username_for(claims), ""
        )
        return LeaveBalanceResult(
            employee=result["employee"],
            balance=LeaveBalanceBuckets(**result["balance"]),
        )

    # ── get_leave_history ─────────────────────────────────────────────────────

    @router.post("/get_leave_history", response_model=GetLeaveHistoryResult)
    async def get_leave_history(
        body: GetLeaveHistoryArgs,
        request: Request,
    ) -> GetLeaveHistoryResult:
        """Return the caller's leave history (at most ``body.limit`` rows)."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("get_leave_history missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        logger.debug(
            "get_leave_history tool_entry rid=%s required_scopes=%s",
            rid,
            ["hr_self_rest"],
        )

        try:
            claims = await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_self_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "get_leave_history token validation failed error_id=%s rid=%s reason=%r details=%s",
                exc.error_id,
                rid,
                str(exc),
                getattr(exc, "details", None),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        logger.debug(
            "get_leave_history validation_ok rid=%s sub=%s jti=%s",
            rid,
            claims.sub,
            claims.jti,
        )

        rows = await hr_service.get_my_leave_requests(
            claims.sub, _username_for(claims), ""
        )
        # body.limit caps the response — service returns full history.
        limited = rows[: body.limit]
        return GetLeaveHistoryResult(
            employee_id=claims.sub,
            entries=[LeaveHistoryEntry(**r) for r in limited],
        )

    # ── approve_leave ─────────────────────────────────────────────────────────

    @router.post("/approve_leave", response_model=ApproveLeaveResult)
    async def approve_leave(
        body: ApproveLeaveArgs,
        request: Request,
    ) -> ApproveLeaveResult:
        """Approve a pending leave request (manager-only, scope hr_approve_rest)."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("approve_leave missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        logger.debug(
            "approve_leave tool_entry rid=%s leave_id=%s required_scopes=%s",
            rid,
            body.leave_id,
            ["hr_approve_rest"],
        )

        try:
            claims = await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_approve_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "approve_leave token validation failed error_id=%s rid=%s reason=%r details=%s",
                exc.error_id,
                rid,
                str(exc),
                getattr(exc, "details", None),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        logger.debug(
            "approve_leave validation_ok rid=%s sub=%s jti=%s",
            rid,
            claims.sub,
            claims.jti,
        )

        # act.sub identifies the agent acting for the manager; reviewer_sub is
        # the human manager (claims.sub). The audit trail records both via
        # the F-13 correlation log already; the service captures reviewer_sub +
        # reviewer_name on the request row.
        reviewer_name = _username_for(claims)
        result = await hr_service.approve_leave_request(
            request_id=body.leave_id,
            reviewer_sub=claims.sub,
            reviewer_name=reviewer_name,
        )

        act_sub: str | None = (
            claims.act.get("sub") if isinstance(claims.act, dict) else None
        )
        if result.get("success"):
            return ApproveLeaveResult(
                success=True,
                request_id=result["request_id"],
                new_status=result.get("new_status"),
                employee=result.get("employee"),
                notification=result.get("notification"),
                approved_by=act_sub or claims.sub,
            )
        # Service-level rejection (not_found / invalid_status / insufficient
        # balance). Auth + scope passed, so we return 200 with success=False.
        return ApproveLeaveResult(
            success=False,
            request_id=body.leave_id,
            error=result.get("error"),
            message=result.get("message"),
            approved_by=act_sub or claims.sub,
        )

    return router
