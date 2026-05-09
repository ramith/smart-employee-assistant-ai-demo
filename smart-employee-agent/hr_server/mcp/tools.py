"""HR-server MCP tool endpoints — Sprint 1 Wave 6.

Exposes three FastAPI POST endpoints under ``/mcp/tools/``:

    POST /mcp/tools/get_leave_balance   scope: hr_self_rest
    POST /mcp/tools/get_leave_history   scope: hr_self_rest
    POST /mcp/tools/approve_leave       scope: hr_approve_rest

Each handler:
  1. Extracts a Bearer token from the ``Authorization`` header.
  2. Reads the ``X-Request-ID`` correlation id (set by ``CorrelationIdMiddleware``
     or passed directly from the caller; falls back to ``get_request_id()``).
  3. Calls ``deps.validator.validate_token(jwt, required_scopes=...)`` which runs
     the full F-04 six-step check (sig, iss, exp, aud, act.sub, scope).
  4. On ``JWTValidationError``, ``PeerTrustError``, or ``ScopeError``: raises
     ``HTTPException(401)`` whose ``detail`` dict is ``{"error_id": ..., "request_id": ...}``.
  5. On success: looks up canned data via ``claims.sub`` and returns a typed
     Pydantic response.

Sprint 1 uses hardcoded canned data.  Sprint 2 may swap in a real ``HRDataStore``.
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

logger = logging.getLogger(__name__)

__all__ = [
    "HRMcpToolRouterDeps",
    "build_hr_mcp_router",
    # Pydantic models (re-exported so tests can import them directly)
    "GetLeaveBalanceArgs",
    "LeaveBalanceResult",
    "GetLeaveHistoryArgs",
    "LeaveHistoryEntry",
    "GetLeaveHistoryResult",
    "ApproveLeaveArgs",
    "ApproveLeaveResult",
]

# ---------------------------------------------------------------------------
# Canned data (Sprint 1 — no DB)
# ---------------------------------------------------------------------------

#: Annual leave balance per employee ``sub``.
_CANNED_LEAVE_BALANCES: dict[str, dict] = {
    "probe.user": {"leave_days": 12, "leave_type": "annual"},
    "user-uuid-abc123": {"leave_days": 10, "leave_type": "annual"},
    "default": {"leave_days": 14, "leave_type": "annual"},
}

#: Leave history per employee ``sub``.
_CANNED_LEAVE_HISTORY: dict[str, list[dict]] = {
    "probe.user": [
        {
            "leave_id": "LV-001",
            "start_date": "2026-03-10",
            "end_date": "2026-03-12",
            "days": 3,
            "status": "approved",
            "type": "annual",
        },
        {
            "leave_id": "LV-002",
            "start_date": "2026-04-01",
            "end_date": "2026-04-01",
            "days": 1,
            "status": "approved",
            "type": "sick",
        },
    ],
    "user-uuid-abc123": [
        {
            "leave_id": "LV-003",
            "start_date": "2026-02-14",
            "end_date": "2026-02-14",
            "days": 1,
            "status": "approved",
            "type": "annual",
        },
    ],
    "default": [],
}

#: Leave requests available for approval, keyed by ``leave_id``.
_CANNED_LEAVE_REQUESTS: dict[str, dict] = {
    "LV-001": {"employee_id": "probe.user", "pending": False},
    "LV-004": {"employee_id": "probe.user", "pending": True},
    "LV-005": {"employee_id": "user-uuid-abc123", "pending": True},
}

# Today's date string (canned; Sprint 2 will use real date queries).
_TODAY: str = str(date.today())


# ---------------------------------------------------------------------------
# Pydantic request / response models (from api-contracts.md §4)
# ---------------------------------------------------------------------------


class GetLeaveBalanceArgs(BaseModel):
    """Request body for ``get_leave_balance``.

    ``employee_id`` is optional; if absent the handler uses ``claims.sub``.
    """

    employee_id: str | None = Field(default=None, description="Defaults to token.sub")


class LeaveBalanceResult(BaseModel):
    """Response for ``get_leave_balance``."""

    employee_id: str
    leave_days: int
    leave_type: str = "annual"
    as_of_date: str = Field(default_factory=lambda: str(date.today()))


class GetLeaveHistoryArgs(BaseModel):
    """Request body for ``get_leave_history``."""

    employee_id: str | None = Field(default=None, description="Defaults to token.sub")
    limit: int = Field(default=10, ge=1, le=50)


class LeaveHistoryEntry(BaseModel):
    """A single leave record."""

    leave_id: str
    start_date: str
    end_date: str
    days: int
    status: Literal["approved", "pending", "rejected"]
    type: str


class GetLeaveHistoryResult(BaseModel):
    """Response for ``get_leave_history``."""

    employee_id: str
    entries: list[LeaveHistoryEntry]


class ApproveLeaveArgs(BaseModel):
    """Request body for ``approve_leave``."""

    leave_id: str


class ApproveLeaveResult(BaseModel):
    """Response for ``approve_leave``."""

    leave_id: str
    status: Literal["approved"] = "approved"
    approved_by: str  # act.sub of the token (agent acting for the manager)
    approved_at: str  # ISO-8601 datetime


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------


@dataclass
class HRMcpToolRouterDeps:
    """Injected dependencies for the HR MCP tool router.

    Attributes:
        validator: Wave 5 token validator that enforces the F-04 six-step check.

    Sprint 2 addition::

        data_store: HRDataStore  # non-canned persistence layer
    """

    validator: HRServerTokenValidator  # type: ignore[valid-type]


# ---------------------------------------------------------------------------
# Helper — extract Bearer token
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


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_hr_mcp_router(deps: HRMcpToolRouterDeps) -> APIRouter:
    """Return a FastAPI ``APIRouter`` with three HR tool endpoints.

    All endpoints are mounted under the prefix supplied by the caller (typically
    ``/mcp/tools``).  Each handler validates the inbound token via
    ``deps.validator.validate_token()`` before accessing canned data.

    Args:
        deps: Injected validator (and future data_store in Sprint 2).

    Returns:
        Configured ``APIRouter`` ready to be included in the hr_server FastAPI app.
    """
    router = APIRouter()

    # ── get_leave_balance ─────────────────────────────────────────────────────

    @router.post("/get_leave_balance", response_model=LeaveBalanceResult)
    async def get_leave_balance(
        body: GetLeaveBalanceArgs,
        request: Request,
    ) -> LeaveBalanceResult:
        """Return the employee's current leave balance.

        Required scope: ``hr_self_rest``.
        Uses ``token.sub`` as the employee identifier unless ``body.employee_id``
        is supplied (manager use-case).
        """
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning(
                "get_leave_balance missing_bearer rid=%s", rid
            )
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

        employee_id = body.employee_id or claims.sub
        row = _CANNED_LEAVE_BALANCES.get(employee_id) or _CANNED_LEAVE_BALANCES["default"]
        return LeaveBalanceResult(
            employee_id=employee_id,
            leave_days=row["leave_days"],
            leave_type=row.get("leave_type", "annual"),
            as_of_date=_TODAY,
        )

    # ── get_leave_history ─────────────────────────────────────────────────────

    @router.post("/get_leave_history", response_model=GetLeaveHistoryResult)
    async def get_leave_history(
        body: GetLeaveHistoryArgs,
        request: Request,
    ) -> GetLeaveHistoryResult:
        """Return the employee's leave history.

        Required scope: ``hr_self_rest``.
        Returns at most ``body.limit`` entries (default 10, max 50).
        """
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning(
                "get_leave_history missing_bearer rid=%s", rid
            )
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

        employee_id = body.employee_id or claims.sub
        raw_entries = (
            _CANNED_LEAVE_HISTORY.get(employee_id) or _CANNED_LEAVE_HISTORY["default"]
        )
        limited = raw_entries[: body.limit]
        return GetLeaveHistoryResult(
            employee_id=employee_id,
            entries=[LeaveHistoryEntry(**e) for e in limited],
        )

    # ── approve_leave ─────────────────────────────────────────────────────────

    @router.post("/approve_leave", response_model=ApproveLeaveResult)
    async def approve_leave(
        body: ApproveLeaveArgs,
        request: Request,
    ) -> ApproveLeaveResult:
        """Approve a pending leave request.

        Required scope: ``hr_approve_rest``.
        Manager-only.  Sprint 1: canned response; the scope guard is exercised
        even though this tool is not exposed in the demo query path.
        """
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning(
                "approve_leave missing_bearer rid=%s", rid
            )
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

        from datetime import datetime, timezone

        act_sub: str = (
            claims.act.get("sub") if isinstance(claims.act, dict) else None
        ) or claims.sub
        return ApproveLeaveResult(
            leave_id=body.leave_id,
            status="approved",
            approved_by=act_sub,
            approved_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    return router
