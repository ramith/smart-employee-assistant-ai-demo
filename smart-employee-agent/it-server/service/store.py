"""In-Memory IT Asset Store.

Mirrors hr-server/service/store.py — same shape, same auto-registration
pattern, same logging style. Asset data keyed by employee_id (server-derived
from JWT sub).
"""
import copy
import logging
from datetime import date as dt_date
from typing import Dict, List

logger = logging.getLogger(__name__)

# ─── Global Seed Data (sample assets — for demo) ─────────────────────────────

_SEED_ASSETS: List[Dict] = [
    {"asset_id": "AST-12345", "employee_id": "1042", "type": "laptop", "model": "MBP 14 M3", "status": "outstanding"},
    {"asset_id": "AST-12346", "employee_id": "1042", "type": "phone", "model": "iPhone 15", "status": "returned"},
    {"asset_id": "AST-22001", "employee_id": "2017", "type": "laptop", "model": "Dell XPS 13", "status": "outstanding"},
    {"asset_id": "AST-22002", "employee_id": "2017", "type": "monitor", "model": "Dell U2723QE", "status": "outstanding"},
    {"asset_id": "AST-30115", "employee_id": "3110", "type": "laptop", "model": "MBP 16 M3", "status": "outstanding"},
    {"asset_id": "AST-30116", "employee_id": "3110", "type": "headset", "model": "AirPods Pro 2", "status": "returned"},
]

# ─── Mutable In-Memory Stores ────────────────────────────────────────────────

assets: List[Dict] = []
users: Dict[str, Dict] = {}  # sub -> {first_seen, name}


def reset_data() -> None:
    """Reset all stores. Asset seed re-applied; user records cleared."""
    global assets, users
    prior_users = len(users) if users else 0
    assets = copy.deepcopy(_SEED_ASSETS)
    users = {}
    if prior_users:
        logger.warning(
            "[STORE RESET] IT data reset — cleared %d user(s); seed assets re-applied",
            prior_users,
        )


def ensure_user(sub: str, first_name: str = "", last_name: str = "") -> Dict:
    """Ensure a user record exists. Mirrors hr-server's pattern.

    Returns the user record. (IT data is keyed by employee_id, derived
    server-side from sub at the tool layer; this is just a lightweight
    audit log of who has hit the service.)
    """
    full_name = f"{first_name} {last_name}".strip()
    if sub not in users:
        users[sub] = {
            "sub": sub,
            "name": full_name,
            "first_seen": str(dt_date.today()),
        }
        logger.info(
            "[USER SEEN] sub=%s name=%s",
            sub, full_name or "(no name)",
        )
    return users[sub]


def get_assets_for_employee(employee_id: str, asset_category: str | None = None) -> List[Dict]:
    """Return all assets currently assigned to an employee, optionally filtered by type."""
    matches = [a for a in assets if a["employee_id"] == employee_id]
    if asset_category:
        matches = [a for a in matches if a["type"] == asset_category]
    return matches


def get_asset_by_id(asset_id: str) -> Dict | None:
    for a in assets:
        if a["asset_id"] == asset_id:
            return a
    return None


# Initialize on import.
reset_data()
