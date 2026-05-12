"""HR-server MCP tool endpoints — Sprint 1 Wave 6 / Sprint 4 S4.0 + S4.1 / Sprint 5.

Exposes FastAPI POST endpoints under ``/mcp/tools/``:

    POST /mcp/tools/get_leave_balance              scope: hr_self_rest
    POST /mcp/tools/get_leave_history              scope: hr_self_rest
    POST /mcp/tools/approve_leave                  scope: hr_approve_rest
    POST /mcp/tools/reject_leave                   scope: hr_approve_rest    [S4.4 UC-15]
    POST /mcp/tools/get_cubicle_summary            scope: hr_read_rest        [S4.1 D1]
    POST /mcp/tools/get_vacant_cubicles_on_floor   scope: hr_read_rest        [S4.1 D2]
    POST /mcp/tools/assign_cubicle                 scope: hr_assets_write_rest [S4.1 D3]
    POST /mcp/tools/get_my_cubicle                 scope: hr_self_rest        [S4.1 D4]
    POST /mcp/tools/lookup_employee                scope: hr_read_rest        [S4.1 D5]
    POST /mcp/tools/get_all_leave_requests         scope: hr_approve_rest     [S5 hr.read_all_leaves]

Sprint 4 S4.0 reconciliation (D1): handlers delegate to ``hr_service``
(the canonical in-memory implementation backed by ``service/store.py``)
instead of returning the Sprint-1 canned dicts.

Each handler:
  1. Extracts a Bearer token from the ``Authorization`` header.
  2. Reads the ``X-Request-ID`` correlation id.
  3. Calls ``deps.validator.validate_token(jwt, required_scopes=...)`` (F-04).
  4. On JWTValidationError / PeerTrustError / ScopeError → 401.
  5. On success: delegates to ``hr_service``.
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
    "RejectLeaveArgs",
    "RejectLeaveResult",
    # Sprint 5 S5.1 apply_leave (UC-13 chat path)
    "ApplyLeaveArgs",
    "ApplyLeaveResult",
    # Sprint 5 hr.read_all_leaves (HR Admin chat path)
    "GetAllLeaveRequestsArgs",
    "AllLeaveRequestEntry",
    "GetAllLeaveRequestsResult",
    # Sprint 4 S4.1 cubicle models
    "GetCubicleSummaryArgs",
    "CubicleFloorCounts",
    "GetCubicleSummaryResult",
    "GetVacantCubiclesOnFloorArgs",
    "GetVacantCubiclesOnFloorResult",
    "GetMyCubicleArgs",
    "GetMyCubicleResult",
    "AssignCubicleArgs",
    "AssignCubicleResult",
    "LookupEmployeeArgs",
    "LookupEmployeeResult",
    # hr_basic leave policy
    "GetLeavePolicyArgs",
    "LeavePolicyEntry",
    "GetLeavePolicyResult",
]


# ---------------------------------------------------------------------------
# Pydantic request / response models — Sprint 4 reshape (D1, RR-1)
# ---------------------------------------------------------------------------


class GetLeavePolicyArgs(BaseModel):
    """Request body for ``get_leave_policy`` — no fields (parameter-less read)."""


class LeavePolicyEntry(BaseModel):
    """One leave-type entry. Mirrors a ``hr_service.get_leave_policy`` row."""

    leave_type: str
    max_days_per_year: int
    requires_approval: bool
    min_notice_days: int
    description: str


class GetLeavePolicyResult(BaseModel):
    """Response for ``get_leave_policy`` — the full company leave policy plus,
    in ``to_apply``, the information the employee must provide to submit a leave
    request (so a "what do I need to apply for leave?" question can be answered
    directly from this tool's output)."""

    leave_types: list[LeavePolicyEntry]
    to_apply: list[str] = Field(
        default_factory=lambda: [
            "leave_type (one of: Annual Leave, Sick Leave, Personal Leave)",
            "start_date (YYYY-MM-DD)",
            "end_date (YYYY-MM-DD, on or after start_date)",
            "reason (optional)",
        ],
        description="The fields the employee provides to apply for leave.",
    )


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


class RejectLeaveArgs(BaseModel):
    """Request body for ``reject_leave`` (Sprint 4 S4.4 UC-15)."""

    leave_id: str = Field(description="Maps to hr_service request_id.")
    reason: str = Field(min_length=1, description="Non-empty rejection reason.")


class RejectLeaveResult(BaseModel):
    """Response for ``reject_leave``.

    Mirrors ``hr_service.reject_leave_request`` shape. Same envelope rules
    as :class:`ApproveLeaveResult` — auth/scope failure → 401; business-
    layer rejection (not_found / invalid_status) → 200 with ``success=False``.
    """

    success: bool = True
    request_id: str
    new_status: str | None = None
    employee: str | None = None
    notification: str | None = None
    error: str | None = None
    message: str | None = None
    rejected_by: str | None = None


# ---------------------------------------------------------------------------
# Sprint 5 S5.1 — apply_leave (UC-13 chat path; the dispatcher requires
# leave_type/start_date/end_date so a partial LLM call fails pre-CIBA).
# ---------------------------------------------------------------------------


class ApplyLeaveArgs(BaseModel):
    """Request body for ``apply_leave``.

    ``leave_type`` must be a key of ``store.leave_policy`` (Annual Leave /
    Sick Leave / Personal Leave); ``start_date`` / ``end_date`` are
    ``YYYY-MM-DD`` with ``end_date >= start_date``. Validation of those
    business rules happens in ``hr_service.apply_leave`` (returns a 200 with
    ``success=False`` + ``error`` on rejection); a structurally-malformed
    body (missing a required field, wrong type) → 422 from FastAPI.
    """

    leave_type: str = Field(description="One of: Annual Leave, Sick Leave, Personal Leave.")
    start_date: str = Field(description="YYYY-MM-DD.")
    end_date: str = Field(description="YYYY-MM-DD; on or after start_date.")
    reason: str = Field(default="", description="Optional free-text reason.")


class ApplyLeaveResult(BaseModel):
    """Response for ``apply_leave``.

    Mirrors ``hr_service.apply_leave``: happy path → ``success=True`` +
    ``request_id``; business-layer rejection (invalid_leave_type /
    invalid_dates / insufficient_notice / insufficient_balance) → 200 with
    ``success=False`` + ``error`` + ``message`` (same envelope rule as
    :class:`AssignCubicleResult`).
    """

    success: bool = True
    request_id: str | None = None
    error: str | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# Sprint 5 — get_all_leave_requests (hr.read_all_leaves, HR Admin chat path)
# ---------------------------------------------------------------------------


class GetAllLeaveRequestsArgs(BaseModel):
    """Request body for ``get_all_leave_requests``.

    Both fields are optional — omitting them returns all leave requests
    regardless of status or employee name.
    """

    status: str | None = Field(
        default=None,
        description="Optional status filter, e.g. 'Pending', 'Approved', 'Rejected'.",
    )
    employee_name: str | None = Field(
        default=None,
        description="Optional employee name substring filter.",
    )


class AllLeaveRequestEntry(BaseModel):
    """A single leave request row returned by ``get_all_leave_requests``.

    Never contains ``sub`` or ``user_sub`` — only display-safe fields.
    """

    request_id: str
    employee: str
    type: str
    start_date: str
    end_date: str
    days_requested: int
    status: str


class GetAllLeaveRequestsResult(BaseModel):
    """Response for ``get_all_leave_requests``."""

    leave_requests: list[AllLeaveRequestEntry]


# ---------------------------------------------------------------------------
# Sprint 4 S4.1 cubicle models (UC-11)
# ---------------------------------------------------------------------------


class GetCubicleSummaryArgs(BaseModel):
    """Request body for ``get_cubicle_summary``. No fields — included for
    payload symmetry with the other tools."""


class CubicleFloorCounts(BaseModel):
    """Per-floor totals returned by the summary endpoint."""

    total: int
    vacant: int


class GetCubicleSummaryResult(BaseModel):
    """Response for ``get_cubicle_summary``.

    Shape mirrors the dict returned by ``hr_service.get_cubicle_summary``:
    one entry per floor (1..4) keyed as ``floor_N``.
    """

    floor_1: CubicleFloorCounts
    floor_2: CubicleFloorCounts
    floor_3: CubicleFloorCounts
    floor_4: CubicleFloorCounts


class GetVacantCubiclesOnFloorArgs(BaseModel):
    """Request body for ``get_vacant_cubicles_on_floor``."""

    floor: int = Field(ge=1, le=4, description="Floor number 1..4.")


class GetVacantCubiclesOnFloorResult(BaseModel):
    """Response for ``get_vacant_cubicles_on_floor``.

    On valid floor: returns the list of vacant cubicle IDs on that floor.
    On invalid floor: ``error="invalid_floor"`` with ``vacant=[]`` (Pydantic
    cannot represent the heterogeneous union cleanly without a discriminator;
    we keep both fields and let the caller branch on ``error``).
    """

    floor: int | None = None
    vacant: list[str] = Field(default_factory=list)
    error: str | None = None
    message: str | None = None


class GetMyCubicleArgs(BaseModel):
    """Request body for ``get_my_cubicle``. Self-service: no fields needed —
    the handler reads ``claims.username``."""


class GetMyCubicleResult(BaseModel):
    """Response for ``get_my_cubicle``.

    ``assigned=False`` indicates the caller has no cubicle. Otherwise
    ``cubicle_id``, ``floor``, ``assigned_at`` are populated.
    """

    assigned: bool
    cubicle_id: str | None = None
    floor: int | None = None
    assigned_at: str | None = None


class AssignCubicleArgs(BaseModel):
    """Request body for ``assign_cubicle`` (write-tier, hr_assets_write_rest)."""

    cubicle_id: str = Field(min_length=1, description="Cubicle identifier (e.g. C-027).")
    employee_username: str = Field(min_length=1, description="Target employee username.")
    employee_email: str = Field(default="", description="Optional email; for audit.")


class _CurrentHolder(BaseModel):
    """Sub-model for ``cubicle_already_occupied`` responses."""

    username: str | None = None
    email: str | None = None


class _AssignedTo(BaseModel):
    """Sub-model for the happy-path ``assigned_to`` field."""

    username: str | None = None
    email: str | None = None


class AssignCubicleResult(BaseModel):
    """Response for ``assign_cubicle``.

    Happy path: ``success=True`` plus assignment fields. Business-layer
    rejection: ``success=False`` plus ``error`` and either ``current_holder``
    or ``message``. Status stays 200 — auth + scope already passed.
    """

    success: bool = True
    cubicle_id: str | None = None
    floor: int | None = None
    assigned_to: _AssignedTo | None = None
    assigned_at: str | None = None
    error: str | None = None
    message: str | None = None
    current_holder: _CurrentHolder | None = None


class LookupEmployeeArgs(BaseModel):
    """Request body for ``lookup_employee``."""

    username_or_email: str = Field(min_length=1, description="Username or email.")


class LookupEmployeeResult(BaseModel):
    """Response for ``lookup_employee``.

    F-12: ``sub`` IS UUID is returned to the HR Agent (which uses it as
    ``login_hint`` on CIBA) but MUST NOT be logged or surfaced to chat/UI.
    """

    found: bool
    username: str | None = None
    email: str | None = None
    sub: str | None = None


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
    """Return a FastAPI ``APIRouter`` with the HR tool endpoints.

    All endpoints are mounted under the prefix supplied by the caller (typically
    ``/mcp/tools``).  Each handler validates the inbound token via
    ``deps.validator.validate_token()`` before delegating to ``hr_service``.
    """
    router = APIRouter()

    # ── get_leave_policy (hr_basic_rest — company leave types + rules) ────────

    @router.post("/get_leave_policy", response_model=GetLeavePolicyResult)
    async def get_leave_policy(
        body: GetLeavePolicyArgs,  # noqa: ARG001 — parameter-less
        request: Request,
    ) -> GetLeavePolicyResult:
        """Return the company leave policy (leave types + rules). Scope: hr_basic_rest."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("get_leave_policy missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )
        try:
            await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_basic_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "get_leave_policy validation_failed error_id=%s rid=%s",
                exc.error_id, rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        policy = await hr_service.get_leave_policy()
        return GetLeavePolicyResult(
            leave_types=[LeavePolicyEntry(**row) for row in policy]
        )

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

    # ── reject_leave (Sprint 4 S4.4, UC-15) ───────────────────────────────────
    #
    # Mirrors approve_leave: same auth boilerplate, same scope (hr_approve_rest),
    # delegates to hr_service.reject_leave_request. Body carries `reason` which
    # is recorded on the leave request row for audit; the dispatcher constructs
    # action_text per F-08 sanitisation upstream.

    @router.post("/reject_leave", response_model=RejectLeaveResult)
    async def reject_leave(
        body: RejectLeaveArgs,
        request: Request,
    ) -> RejectLeaveResult:
        """Reject a pending leave request (manager-only, scope hr_approve_rest)."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("reject_leave missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        logger.debug(
            "reject_leave tool_entry rid=%s leave_id=%s required_scopes=%s",
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
                "reject_leave token validation failed error_id=%s rid=%s reason=%r details=%s",
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
            "reject_leave validation_ok rid=%s sub=%s jti=%s",
            rid,
            claims.sub,
            claims.jti,
        )

        reviewer_name = _username_for(claims)
        result = await hr_service.reject_leave_request(
            request_id=body.leave_id,
            reason=body.reason,
            reviewer_sub=claims.sub,
            reviewer_name=reviewer_name,
        )

        act_sub: str | None = (
            claims.act.get("sub") if isinstance(claims.act, dict) else None
        )
        if result.get("success"):
            return RejectLeaveResult(
                success=True,
                request_id=result["request_id"],
                new_status=result.get("new_status"),
                employee=result.get("employee"),
                notification=result.get("notification"),
                rejected_by=act_sub or claims.sub,
            )
        return RejectLeaveResult(
            success=False,
            request_id=body.leave_id,
            error=result.get("error"),
            message=result.get("message"),
            rejected_by=act_sub or claims.sub,
        )

    # ── apply_leave (S5.1, UC-13 chat path, hr_self_rest) ─────────────────────

    @router.post("/apply_leave", response_model=ApplyLeaveResult)
    async def apply_leave(
        body: ApplyLeaveArgs,
        request: Request,
    ) -> ApplyLeaveResult:
        """Submit a leave request for the authenticated user. Scope: hr_self_rest."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("apply_leave missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )
        try:
            claims = await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_self_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "apply_leave token validation failed error_id=%s rid=%s",
                exc.error_id, rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        logger.debug(
            "apply_leave validation_ok rid=%s sub=%s leave_type=%s start=%s end=%s",
            rid, claims.sub, body.leave_type, body.start_date, body.end_date,
        )
        result = await hr_service.apply_leave(
            claims.sub,
            _username_for(claims),
            "",
            body.leave_type,
            body.start_date,
            body.end_date,
            body.reason,
        )
        if result.get("success"):
            return ApplyLeaveResult(success=True, request_id=result["request_id"])
        return ApplyLeaveResult(
            success=False,
            error=result.get("error"),
            message=result.get("message"),
        )

    # ── Sprint 4 S4.1 cubicle handlers (UC-11) ────────────────────────────────
    #
    # All five share the same auth boilerplate as the leave handlers: extract
    # bearer, validate token under the required scope, delegate to hr_service.
    # Auth-layer rejection → 401. Business-layer rejection (e.g. cubicle
    # already occupied) → 200 with ``success=False`` per the existing pattern.

    def _validate(token_str: str | None, rid: str, required: frozenset[str]):
        """Shared validate-or-401 helper (used by the cubicle handlers)."""
        if not token_str:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )
        return token_str, rid, required

    # ── get_cubicle_summary (D1, hr_read_rest) ────────────────────────────────

    @router.post("/get_cubicle_summary", response_model=GetCubicleSummaryResult)
    async def get_cubicle_summary(
        body: GetCubicleSummaryArgs,  # noqa: ARG001 — empty body kept for symmetry
        request: Request,
    ) -> GetCubicleSummaryResult:
        """Return per-floor cubicle counts (HR Admin read path)."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("get_cubicle_summary missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )
        try:
            await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_read_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "get_cubicle_summary validation_failed error_id=%s rid=%s",
                exc.error_id, rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        result = await hr_service.get_cubicle_summary()
        return GetCubicleSummaryResult(
            floor_1=CubicleFloorCounts(**result["floor_1"]),
            floor_2=CubicleFloorCounts(**result["floor_2"]),
            floor_3=CubicleFloorCounts(**result["floor_3"]),
            floor_4=CubicleFloorCounts(**result["floor_4"]),
        )

    # ── get_vacant_cubicles_on_floor (D2, hr_read_rest) ───────────────────────

    @router.post(
        "/get_vacant_cubicles_on_floor",
        response_model=GetVacantCubiclesOnFloorResult,
    )
    async def get_vacant_cubicles_on_floor(
        body: GetVacantCubiclesOnFloorArgs,
        request: Request,
    ) -> GetVacantCubiclesOnFloorResult:
        """Return the vacant-cubicle list for a given floor."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("get_vacant_cubicles_on_floor missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )
        try:
            await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_read_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "get_vacant_cubicles_on_floor validation_failed error_id=%s rid=%s",
                exc.error_id, rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        result = await hr_service.get_vacant_cubicles_on_floor(body.floor)
        return GetVacantCubiclesOnFloorResult(**result)

    # ── get_my_cubicle (D4, hr_self_rest) ─────────────────────────────────────

    @router.post("/get_my_cubicle", response_model=GetMyCubicleResult)
    async def get_my_cubicle(
        body: GetMyCubicleArgs,  # noqa: ARG001 — empty body
        request: Request,
    ) -> GetMyCubicleResult:
        """Return the caller's cubicle assignment (self-service)."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("get_my_cubicle missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )
        try:
            claims = await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_self_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "get_my_cubicle validation_failed error_id=%s rid=%s",
                exc.error_id, rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        result = await hr_service.get_my_cubicle(
            sub=claims.sub, username=getattr(claims, "username", None)
        )
        return GetMyCubicleResult(**result)

    # ── assign_cubicle (D3, hr_assets_write_rest) ─────────────────────────────

    @router.post("/assign_cubicle", response_model=AssignCubicleResult)
    async def assign_cubicle(
        body: AssignCubicleArgs,
        request: Request,
    ) -> AssignCubicleResult:
        """Assign a cubicle to an employee (HR Admin, write tier).

        Required scope ``hr_assets_write_rest`` (NEW in Sprint 4). On
        business-layer rejection (already occupied / not found) returns 200
        with ``success=False`` and an ``error`` field.
        """
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("assign_cubicle missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )
        try:
            claims = await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_assets_write_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "assign_cubicle validation_failed error_id=%s rid=%s",
                exc.error_id, rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        # The agent passes employee_username + employee_email (from its
        # lookup_employee call). The target employee's IS sub is populated
        # only when the agent has resolved one — pass through if available
        # (Sprint 4 S4.1 keeps it None on this surface; sub is recorded
        # internally if the caller threads it).
        target_sub = getattr(claims, "sub", None)  # caller's sub (admin)
        # Per sprint-4.md §7 the assigned_to_sub records the *target employee*
        # sub. The HR Agent will look this up on the lookup_employee MCP tool
        # before initiating CIBA, but the wire body here doesn't carry it
        # (LLM- and admin-typed inputs control username/email only). For
        # Sprint 4 we therefore record None and rely on the username as the
        # canonical join key. The internal sub field is preserved in the
        # service signature for Sprint 5 when the agent threads it through.
        _ = target_sub  # not stored — caller's sub is for audit, not the join.

        logger.info(
            "assign_cubicle validation_ok rid=%s admin_sub=%s cubicle_id=%s "
            "employee_username=%s",
            rid,
            getattr(claims, "sub", "?"),
            body.cubicle_id,
            body.employee_username,
        )

        result = await hr_service.assign_cubicle(
            cubicle_id=body.cubicle_id,
            employee_username=body.employee_username,
            employee_email=body.employee_email or "",
            sub=None,
        )

        # Branch the response shape on success.
        if result.get("success"):
            return AssignCubicleResult(
                success=True,
                cubicle_id=result["cubicle_id"],
                floor=result["floor"],
                assigned_to=_AssignedTo(**result["assigned_to"]),
                assigned_at=result["assigned_at"],
            )
        # Business-layer rejection.
        if result.get("error") == "cubicle_already_occupied":
            return AssignCubicleResult(
                success=False,
                error="cubicle_already_occupied",
                current_holder=_CurrentHolder(**result["current_holder"]),
            )
        return AssignCubicleResult(
            success=False,
            error=result.get("error"),
            message=result.get("message"),
        )

    # ── lookup_employee (D5, hr_read_rest) ────────────────────────────────────

    @router.post("/lookup_employee", response_model=LookupEmployeeResult)
    async def lookup_employee(
        body: LookupEmployeeArgs,
        request: Request,
    ) -> LookupEmployeeResult:
        """Resolve a username-or-email to ``{found, username, email, sub}``.

        F-12: ``sub`` is returned to the agent (used as ``login_hint`` on
        CIBA in some flows) but agent log lines MUST NOT echo it. The SPA /
        chat surface NEVER sees this response — the agent transforms it
        into the human-readable ``action_text`` before propagating.
        """
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("lookup_employee missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )
        try:
            await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_read_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "lookup_employee validation_failed error_id=%s rid=%s",
                exc.error_id, rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        result = await hr_service.lookup_employee(body.username_or_email)
        return LookupEmployeeResult(**result)

    # ── get_all_leave_requests (Sprint 5, hr.read_all_leaves, hr_approve_rest) ──
    #
    # HR Admin read: list all leave requests, optionally filtered by status and/or
    # employee_name. Scope: hr_approve_rest (same as approve/reject — whoever can
    # approve is who'd ask). Employees lack this scope → IS denies CIBA consent →
    # the existing ERR-MCP-003 / ERR-CIBA-005 path surfaces "you don't have
    # permission" copy in the SPA. No ``sub`` is returned in any row.

    @router.post("/get_all_leave_requests", response_model=GetAllLeaveRequestsResult)
    async def get_all_leave_requests(
        body: GetAllLeaveRequestsArgs,
        request: Request,
    ) -> GetAllLeaveRequestsResult:
        """List all employees' leave requests (HR Admin only, scope hr_approve_rest)."""
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning("get_all_leave_requests missing_bearer rid=%s", rid)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        logger.debug(
            "get_all_leave_requests tool_entry rid=%s required_scopes=%s",
            rid,
            ["hr_approve_rest"],
        )

        try:
            await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"hr_approve_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "get_all_leave_requests validation_failed error_id=%s rid=%s",
                exc.error_id, rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        rows = await hr_service.get_all_leave_requests(
            status=body.status,
            employee_name=body.employee_name,
        )
        return GetAllLeaveRequestsResult(
            leave_requests=[AllLeaveRequestEntry(**row) for row in rows]
        )

    return router
