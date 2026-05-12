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


def get_my_assets(username: str = "", *, sub: str = "") -> dict:
    """Return assets assigned to the caller (UC-12 IT leg).

    Identity resolution: prefer the ``username`` profile claim when present;
    otherwise resolve ``sub`` → ``username`` via the user seed (OBO/CIBA
    tokens carry only ``sub``). Returns ``{assets, total}`` with an empty
    list when nothing resolves or no rows match.
    """
    if not username and sub:
        record = store.lookup_user_by_sub(sub)
        username = (record or {}).get("username", "") if record else ""
    rows = store.get_assets_for_username(username) if username else []
    logger.info(
        "[IT QUERY] get_my_assets username=%s sub=%s returned=%d",
        username or "(unresolved)", sub or "(none)", len(rows),
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


def issue_asset(asset_id: str, employee: str) -> dict:
    """Record an asset issuance (HR-Admin write path; UC-07 / UC-18).

    Persists an assignment row so the employee's "My IT Assets" panel
    (``get_my_assets``) and the HR-admin Devices report
    (``get_all_asset_assignments``) reflect it — the Sprint 1 endpoint
    returned success without recording anything.

    ``employee`` is the recipient's username; an email-form value is
    normalised to its local part (the ``username == email-local-part``
    convention — see ``common/auth/ciba_client.py`` / ``docs/wso2-is-setup.md``
    §5.5). ``model`` / ``type`` come from the catalogue when ``asset_id`` is
    catalogued; an uncatalogued id is still recorded. Returns the stored row.
    """
    asset_id = (asset_id or "").strip()
    employee = (employee or "").strip()
    username = employee.split("@", 1)[0] if "@" in employee else employee
    cat = store.catalogue_entry_by_id(asset_id)
    model = (cat or {}).get("model", "") if cat else ""
    asset_type = (cat or {}).get("type", "") if cat else ""
    row = store.record_issuance(asset_id, username, model=model, asset_type=asset_type)
    logger.info(
        "[IT WRITE] issue_asset asset_id=%s -> username=%s (type=%s)",
        asset_id, username or "(empty)", asset_type or "(uncatalogued)",
    )
    return row
