"""IT Service — read-side query logic.

Mirrors hr_server/service/hr_service.py shape. Sprint 4 S4.2 (UC-12):
the legacy ``get_employee_assets(employee_id)`` is gone; the seed is
rekeyed by ``username``. New functions:

  - ``get_my_assets(username)``           — UC-12 self-service IT leg
  - ``get_all_asset_assignments()``       — UC-16 Devices report (lands here
                                            for completeness; consumed in S4.5)
  - ``list_available_assets(asset_type)`` — kept; reads asset catalogue
"""
import logging
from typing import Optional

from . import store

logger = logging.getLogger(__name__)


def list_available_assets(asset_type: Optional[str] = None) -> list:
    """Return the asset catalogue, optionally filtered by type.

    Sprint 4 S4.0: catalogue lives in ``store._ASSET_CATALOGUE``; this is a
    thin pass-through. The catalogue is not user-keyed.
    """
    return store.get_asset_catalogue(asset_type)


def get_my_assets(username: str) -> dict:
    """Return assets assigned to *username* (UC-12 IT leg).

    Sprint 4 S4.2: replaces ``get_employee_assets(employee_id)`` /
    ``get_assigned_assets(employee_id)`` from Sprint 1. Identity is now
    keyed by ``username`` per sprint-4.md §7.

    Returns ``{assets, total}`` with an empty list when no rows match.
    """
    rows = store.get_assets_for_username(username)
    logger.info(
        "[IT QUERY] get_my_assets username=%s returned=%d",
        username, len(rows),
    )
    return {"assets": rows, "total": len(rows)}


def get_all_asset_assignments() -> list[dict]:
    """Return all asset rows joined with the user's email — UC-16 Devices report.

    Output rows: ``{username, email, asset_id, type, model, status}``.
    The ``sub`` field is *never* surfaced (sprint-4.md §7 identity model).
    Lands here in S4.2 for completeness; the REST surface that consumes it
    is built in S4.5 (UC-16).
    """
    rows: list[dict] = []
    for asset in store.assets:
        username = asset.get("username", "")
        record = store.lookup_user_by_username(username) if username else None
        email = (record or {}).get("email", "")
        rows.append({
            "username": username,
            "email": email,
            "asset_id": asset["asset_id"],
            "type": asset["type"],
            "model": asset["model"],
            "status": asset["status"],
        })
    rows.sort(key=lambda r: (r["username"], r["asset_id"]))
    return rows


def get_asset_by_id(asset_id: str) -> Optional[dict]:
    return store.get_asset_by_id(asset_id)
