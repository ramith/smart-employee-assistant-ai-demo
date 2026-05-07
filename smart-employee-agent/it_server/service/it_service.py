"""IT Service — read-side query logic.

Mirrors hr_server/service/hr_service.py shape. Sprint 0 scaffold; Sprint 1
fills in pagination + cursor + the full it.get_employee_assets / it.get_asset_by_id
tool implementations.
"""
import base64
import json
import logging
from typing import Optional, Tuple

from . import store

logger = logging.getLogger(__name__)


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return int(data.get("o", 0))
    except Exception:
        return 0


def get_employee_assets(
    employee_id: str,
    asset_category: Optional[str] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> dict:
    """Return paginated assets for an employee.

    Pagination envelope per docs/milestone-plan.md §3.4 task 21.
    """
    limit = max(1, min(limit, 200))  # POC clamps
    offset = decode_cursor(cursor)

    all_matches = store.get_assets_for_employee(employee_id, asset_category)
    total = len(all_matches)
    page = all_matches[offset : offset + limit]
    next_cursor = encode_cursor(offset + limit) if offset + limit < total else None

    logger.info(
        "[IT QUERY] employee_id=%s category=%s limit=%d offset=%d returned=%d total=%d",
        employee_id, asset_category, limit, offset, len(page), total,
    )
    return {"assets": page, "total": total, "next_cursor": next_cursor}


def get_asset_by_id(asset_id: str) -> Optional[dict]:
    return store.get_asset_by_id(asset_id)
