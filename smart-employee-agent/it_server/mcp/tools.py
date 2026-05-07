"""IT-server MCP tool endpoints — Sprint 1 Wave 6.

Exposes two FastAPI POST endpoints under ``/mcp/tools/``:

    POST /mcp/tools/list_available_assets   scope: it_assets_read_rest
    POST /mcp/tools/get_my_assets           scope: it_assets_read_rest

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

Sprint 1 uses hardcoded canned data.  Sprint 2 may swap in a real ``ITDataStore``.
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
]

# ---------------------------------------------------------------------------
# Canned data (Sprint 1 — no DB)
# ---------------------------------------------------------------------------

#: Asset catalogue available for request.
_CANNED_ASSET_CATALOGUE: list[dict] = [
    {"asset_id": "MBP-14-001", "model": "MacBook Pro 14", "type": "laptop", "available_count": 3},
    {"asset_id": "MBP-16-001", "model": "MacBook Pro 16", "type": "laptop", "available_count": 1},
    {"asset_id": "MON-LG-001", "model": "LG 27UK850", "type": "monitor", "available_count": 5},
    {"asset_id": "MON-DEL-001", "model": "Dell UltraSharp 27", "type": "monitor", "available_count": 2},
    {"asset_id": "PHN-IP15-001", "model": "iPhone 15 Pro", "type": "phone", "available_count": 4},
]

#: Assets assigned to each employee, keyed by ``sub``.
_CANNED_ASSIGNED_ASSETS: dict[str, list[dict]] = {
    "probe.user": [
        {
            "asset_id": "MBP-14-002",
            "model": "MacBook Pro 14",
            "type": "laptop",
            "assigned_since": "2025-09-01",
        },
        {
            "asset_id": "MON-LG-002",
            "model": "LG 27UK850",
            "type": "monitor",
            "assigned_since": "2025-09-01",
        },
    ],
    "user-uuid-abc123": [
        {
            "asset_id": "MBP-16-002",
            "model": "MacBook Pro 16",
            "type": "laptop",
            "assigned_since": "2025-11-15",
        },
    ],
    "default": [],
}


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
    """A single asset assigned to an employee."""

    asset_id: str
    model: str
    type: str
    assigned_since: str  # ISO-8601 date


class GetMyAssetsResult(BaseModel):
    """Response for ``get_my_assets``."""

    employee_id: str
    assets: list[AssignedAsset]


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
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        try:
            await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"it_assets_read_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "list_available_assets token validation failed error_id=%s rid=%s",
                exc.error_id,
                rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        catalogue = _CANNED_ASSET_CATALOGUE
        if body.asset_type is not None:
            catalogue = [a for a in catalogue if a["type"] == body.asset_type]

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
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": "ERR-AUTH-006", "request_id": rid},
            )

        try:
            claims = await deps.validator.validate_token(
                token_str,
                required_scopes=frozenset({"it_assets_read_rest"}),
            )
        except (JWTValidationError, PeerTrustError, ScopeError) as exc:
            logger.warning(
                "get_my_assets token validation failed error_id=%s rid=%s",
                exc.error_id,
                rid,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_id": exc.error_id, "request_id": rid},
            ) from exc

        employee_id = body.employee_id or claims.sub
        raw_assets = (
            _CANNED_ASSIGNED_ASSETS.get(employee_id)
            or _CANNED_ASSIGNED_ASSETS["default"]
        )
        return GetMyAssetsResult(
            employee_id=employee_id,
            assets=[AssignedAsset(**a) for a in raw_assets],
        )

    return router
