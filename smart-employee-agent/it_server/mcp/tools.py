"""IT-server MCP tool endpoints — Sprint 1 Wave 6 (Sprint 4 S4.0 reconciliation).

Exposes FastAPI POST endpoints under ``/mcp/tools/``:

    POST /mcp/tools/list_available_assets   scope: it_assets_read_rest
    POST /mcp/tools/get_my_assets           scope: it_assets_read_rest
    POST /mcp/tools/issue_asset             scope: it_assets_write_rest

Each handler:
  1. Extracts a Bearer token from the ``Authorization`` header.
  2. Reads the ``X-Request-ID`` correlation id (set by ``CorrelationIdMiddleware``
     or passed directly from the caller; falls back to ``get_request_id()``).
  3. Calls ``deps.validator.validate_token(jwt, required_scopes=...)`` which runs
     the full F-04 six-step check (sig, iss, exp, aud, act.sub, scope).
  4. On ``JWTValidationError``, ``PeerTrustError``, or ``ScopeError``: raises
     ``HTTPException(401)`` whose ``detail`` dict is ``{"error_id": ..., "request_id": ...}``.
  5. On success: delegates into ``it_server.service.it_service`` and returns a
     typed Pydantic response.

Sprint 4 S4.0 (Stage 6.5 D1): the previous ``_CANNED_*`` dicts have been replaced
by ``it_service.list_available_assets()`` / ``get_assigned_assets()`` so this
module is no longer the source of truth for any data. The ``employee_id``-keyed
shape is preserved per Stage 6.5 D8 — rename to ``username`` is deferred to S4.2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from common.auth.errors import JWTValidationError, PeerTrustError, ScopeError
from common.logging.correlation import get_request_id

try:
    from it_server.auth.validators import ITServerTokenValidator
    from it_server.config import ITServerConfig
except ModuleNotFoundError:
    ITServerTokenValidator = None  # type: ignore[assignment,misc]
    ITServerConfig = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

__all__ = [
    "ITMcpToolRouterDeps",
    "build_it_mcp_router",
    # Pydantic models
    "ListAvailableAssetsArgs",
    "AssetEntry",
    "ListAvailableAssetsResult",
    "GetMyAssetsArgs",
    "AssignedAsset",
    "GetMyAssetsResult",
    "IssueAssetArgs",
    "IssueAssetResult",
]

# ---------------------------------------------------------------------------
# Service delegation (Sprint 4 S4.0 — Stage 6.5 D1)
# ---------------------------------------------------------------------------
# The previous _CANNED_* dicts were inlined Sprint 1 stop-gaps. Sprint 4 routes
# every read through it_server.service.it_service, which reads from
# it_server.service.store. The store still keys by ``employee_id`` (rename to
# ``username`` is deferred to S4.2 — Stage 6.5 D8), so the MCP tool args + this
# module's request models keep ``employee_id`` for now.
from it_server.service import it_service  # noqa: E402


# ---------------------------------------------------------------------------
# Pydantic request / response models (from api-contracts.md §4)
# ---------------------------------------------------------------------------


class ListAvailableAssetsArgs(BaseModel):
    """Request body for ``list_available_assets``.

    ``asset_type`` filters the catalogue; ``None`` returns all asset types.
    """

    asset_type: str | None = Field(
        default=None,
        description="Filter by type: 'laptop' | 'monitor' | 'phone' | None",
    )


class AssetEntry(BaseModel):
    """A single entry in the asset catalogue."""

    asset_id: str
    model: str
    type: str
    available_count: int


class ListAvailableAssetsResult(BaseModel):
    """Response for ``list_available_assets``."""

    assets: list[AssetEntry]


class GetMyAssetsArgs(BaseModel):
    """Request body for ``get_my_assets``.

    ``employee_id`` defaults to ``token.sub`` (self-service).  Managers may
    supply an explicit id when they have ``it.read`` and are acting for a
    subordinate.
    """

    employee_id: str | None = Field(default=None, description="Defaults to token.sub")


class AssignedAsset(BaseModel):
    """A single asset assigned to an employee.

    Sprint 4 S4.0: shape now matches ``it_server.service.store._SEED_ASSETS``:
    ``{asset_id, type, model, status}``. The earlier Sprint 1 ``assigned_since``
    field was a canned-data artefact; the seed store does not carry it.
    """

    asset_id: str
    model: str
    type: str
    status: str  # outstanding | returned


class GetMyAssetsResult(BaseModel):
    """Response for ``get_my_assets``."""

    employee_id: str
    assets: list[AssignedAsset]


class IssueAssetArgs(BaseModel):
    """Request body for ``issue_asset`` (HR Admin write path; D2.8).

    Assigns a catalogued asset to a target employee.  Requires
    ``it_assets_write_rest`` (HR Admin role only) — N33 acceptance.
    """

    asset_id: str = Field(description="Catalogue asset_id, e.g. 'MBP-14-001'")
    employee_id: str = Field(description="Target employee sub")


class IssueAssetResult(BaseModel):
    """Response for ``issue_asset``."""

    asset_id: str
    employee_id: str
    issued_by: str  # act.sub of the token (the agent acting for HR Admin)
    issued_at: str  # ISO-8601 datetime


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------


@dataclass
class ITMcpToolRouterDeps:
    """Injected dependencies for the IT MCP tool router.

    Attributes:
        validator: Wave 5 token validator that enforces the F-04 six-step check.

    Sprint 2 addition::

        data_store: ITDataStore  # non-canned persistence layer
    """

    validator: ITServerTokenValidator  # type: ignore[valid-type]


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

    Prefers the ``X-Request-ID`` header value, falls back to the ContextVar,
    then empty string.
    """
    return (
        request.headers.get("X-Request-ID")
        or get_request_id()
        or ""
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_it_mcp_router(deps: ITMcpToolRouterDeps) -> APIRouter:
    """Return a FastAPI ``APIRouter`` with two IT tool endpoints.

    All endpoints are mounted under the prefix supplied by the caller (typically
    ``/mcp/tools``).  Each handler validates the inbound token via
    ``deps.validator.validate_token()`` before accessing canned data.

    Args:
        deps: Injected validator (and future data_store in Sprint 2).

    Returns:
        Configured ``APIRouter`` ready to be included in the it_server FastAPI app.
    """
    router = APIRouter()

    # ── list_available_assets ─────────────────────────────────────────────────

    @router.post("/list_available_assets", response_model=ListAvailableAssetsResult)
    async def list_available_assets(
        body: ListAvailableAssetsArgs,
        request: Request,
    ) -> ListAvailableAssetsResult:
        """Return asset catalogue, optionally filtered by ``asset_type``.

        Required scope: ``it_assets_read_rest``.
        Not user-specific; ``employee_id`` from the token is not used for
        the catalogue lookup, but the token is still fully validated.
        """
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning(
                "list_available_assets missing_bearer rid=%s", rid
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        logger.debug(
            "list_available_assets tool_entry rid=%s asset_type=%r required_scopes=%s",
            rid,
            body.asset_type,
            ["it_assets_read_rest"],
        )

        try:
            await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"it_assets_read_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "list_available_assets token validation failed error_id=%s rid=%s reason=%r details=%s",
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
            "list_available_assets validation_ok rid=%s asset_type=%r",
            rid,
            body.asset_type,
        )

        catalogue = it_service.list_available_assets(body.asset_type)
        return ListAvailableAssetsResult(
            assets=[AssetEntry(**a) for a in catalogue],
        )

    # ── get_my_assets ─────────────────────────────────────────────────────────

    @router.post("/get_my_assets", response_model=GetMyAssetsResult)
    async def get_my_assets(
        body: GetMyAssetsArgs,
        request: Request,
    ) -> GetMyAssetsResult:
        """Return assets assigned to the requesting user (or to ``body.employee_id``).

        Required scope: ``it_assets_read_rest``.
        Defaults to ``token.sub`` for self-service; managers may pass an explicit
        ``employee_id``.
        """
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning(
                "get_my_assets missing_bearer rid=%s", rid
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        logger.debug(
            "get_my_assets tool_entry rid=%s required_scopes=%s",
            rid,
            ["it_assets_read_rest"],
        )

        try:
            claims = await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"it_assets_read_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "get_my_assets token validation failed error_id=%s rid=%s reason=%r details=%s",
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
            "get_my_assets validation_ok rid=%s sub=%s jti=%s",
            rid,
            claims.sub,
            claims.jti,
        )

        employee_id = body.employee_id or claims.sub
        raw_assets = it_service.get_assigned_assets(employee_id)
        return GetMyAssetsResult(
            employee_id=employee_id,
            assets=[
                AssignedAsset(
                    asset_id=a["asset_id"],
                    model=a["model"],
                    type=a["type"],
                    status=a["status"],
                )
                for a in raw_assets
            ],
        )

    # ── issue_asset (HR Admin write path; D2.8) ────────────────────────────────

    @router.post("/issue_asset", response_model=IssueAssetResult)
    async def issue_asset(
        body: IssueAssetArgs,
        request: Request,
    ) -> IssueAssetResult:
        """Issue an asset to an employee.

        Required scope: ``it_assets_write_rest``.  Sprint 1 used canned data;
        this endpoint records the issuance in-memory only and returns success.
        """
        rid = _get_rid(request)
        token_str = _extract_bearer(request)
        if not token_str:
            logger.warning(
                "issue_asset missing_bearer rid=%s", rid
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        logger.debug(
            "issue_asset tool_entry rid=%s asset_id=%s employee_id=%s required_scopes=%s",
            rid,
            body.asset_id,
            body.employee_id,
            ["it_assets_write_rest"],
        )

        try:
            claims = await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"it_assets_write_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "issue_asset token validation failed error_id=%s rid=%s reason=%r details=%s",
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
            "issue_asset validation_ok rid=%s sub=%s jti=%s",
            rid,
            claims.sub,
            claims.jti,
        )

        from datetime import datetime, timezone

        act_sub: str = (
            claims.act.get("sub") if isinstance(claims.act, dict) else None
        ) or claims.sub
        return IssueAssetResult(
            asset_id=body.asset_id,
            employee_id=body.employee_id,
            issued_by=act_sub,
            issued_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    return router
