"""In-Memory IT Asset Store.

Mirrors hr_server/service/store.py — same shape, same auto-registration
pattern, same logging style.

Sprint 4 S4.2 (UC-12): one-shot data migration. The legacy ``employee_id``
field has been dropped and the seed is rekeyed by ``username`` (per
sprint-4.md §7 identity model). Asset rows now look like
``{asset_id, username, type, model, status}``. The ``users`` dict is also
pre-seeded with the four named demo users (``employee_user``,
``hr_admin_user``, ``jane.doe``, ``bob.smith``) — same shape as
``hr_server/service/store.py`` so ``it_service.get_my_assets`` and the
joined reporting helper can resolve username → email without an extra
lookup.
"""
import copy
import logging
from datetime import date as dt_date
from typing import Dict, List

logger = logging.getLogger(__name__)

# ─── Global Seed Data (sample assets — for demo) ─────────────────────────────
#
# Sprint 4 S4.2: rekeyed by ``username`` (Sprint 1's numeric ``employee_id``
# is gone). The four demo users mirror the HR seed (``hr_server/service/store.py``):
#   - ``employee_user``  : 1 laptop (outstanding) + 1 phone (returned)
#   - ``hr_admin_user``  : 1 laptop (outstanding)
#   - ``jane.doe``       : 1 laptop (outstanding)
#   - ``bob.smith``      : 1 laptop (outstanding)

_SEED_ASSETS: List[Dict] = [
    {"asset_id": "AST-12345", "username": "employee_user", "type": "laptop", "model": "MBP 14 M3", "status": "outstanding"},
    {"asset_id": "AST-12346", "username": "employee_user", "type": "phone", "model": "iPhone 15", "status": "returned"},
    {"asset_id": "AST-22001", "username": "hr_admin_user", "type": "laptop", "model": "Dell XPS 13", "status": "outstanding"},
    {"asset_id": "AST-30115", "username": "jane.doe", "type": "laptop", "model": "MBP 16 M3", "status": "outstanding"},
    {"asset_id": "AST-40220", "username": "bob.smith", "type": "laptop", "model": "ThinkPad X1 Carbon", "status": "outstanding"},
]

# Sprint 4 S4.2 — pre-seeded named demo users; mirror hr_server/service/store.py.
# Keys are the JWT ``sub`` so the self-service path can resolve sub → username
# (OBO/CIBA tokens carry only ``sub``, not the ``username`` profile claim). The
# two users that actually authenticate use their real WSO2 IS user IDs (these
# match ``hr_server/service/store._SEED_CUBICLE_ASSIGNMENTS``); jane.doe /
# bob.smith are report-table filler and never sign in, so their subs are stable
# placeholders. The reporting helper joins on ``username``.
_SEED_USERS = [
    {
        "username": "employee_user",
        "email": "employee.user@example.com",
        "sub": "2048ad8c-16a6-4ec1-bb63-b38300118f28",
        "name": "Employee User",
    },
    {
        "username": "hr_admin_user",
        "email": "hr.admin.user@example.com",
        "sub": "15fab9e7-18ec-4f6b-be0f-7aa1ddcebfb7",
        "name": "HR Admin User",
    },
    {
        "username": "jane.doe",
        "email": "jane.doe@example.com",
        "sub": "jane.doe-sub-uuid-0003",
        "name": "Jane Doe",
    },
    {
        "username": "bob.smith",
        "email": "bob.smith@example.com",
        "sub": "bob.smith-sub-uuid-0004",
        "name": "Bob Smith",
    },
]

# ─── Asset Catalogue (demo: assets available for issuance) ──────────────────

#: Sprint 4 S4.0: relocated here from it_server/mcp/tools.py:_CANNED_ASSET_CATALOGUE
#: so the MCP tool delegates into the service layer rather than carrying its own
#: hardcoded data. Keyed by asset_id; consumed by list_available_assets.
_ASSET_CATALOGUE: List[Dict] = [
    {"asset_id": "MBP-14-001", "model": "MacBook Pro 14", "type": "laptop", "available_count": 3},
    {"asset_id": "MBP-16-001", "model": "MacBook Pro 16", "type": "laptop", "available_count": 1},
    {"asset_id": "MON-LG-001", "model": "LG 27UK850", "type": "monitor", "available_count": 5},
    {"asset_id": "MON-DEL-001", "model": "Dell UltraSharp 27", "type": "monitor", "available_count": 2},
    {"asset_id": "PHN-IP15-001", "model": "iPhone 15 Pro", "type": "phone", "available_count": 4},
]


def get_asset_catalogue(asset_type: str | None = None) -> List[Dict]:
    """Return the asset catalogue, optionally filtered by asset_type."""
    if asset_type is None:
        return list(_ASSET_CATALOGUE)
    return [a for a in _ASSET_CATALOGUE if a["type"] == asset_type]


# ─── Mutable In-Memory Stores ────────────────────────────────────────────────

assets: List[Dict] = []
# Sprint 4 S4.2: pre-seeded by reset_data(); keyed by sub (UUID). Each value
# is ``{username, email, sub, name, first_seen}``. ``ensure_user`` continues
# to upsert by sub at runtime if a fresh sub appears (auto-register pattern).
users: Dict[str, Dict] = {}


def reset_data() -> None:
    """Reset all stores. Asset seed re-applied; named-user seed re-applied."""
    global assets, users
    prior_users = len(users) if users else 0
    assets = copy.deepcopy(_SEED_ASSETS)
    users = {}
    for entry in _SEED_USERS:
        sub = entry["sub"]
        users[sub] = {
            "username": entry["username"],
            "email": entry["email"],
            "sub": sub,
            "name": entry["name"],
            "first_seen": str(dt_date.today()),
        }
    if prior_users:
        logger.warning(
            "[STORE RESET] IT data reset — cleared %d user(s); seed assets re-applied",
            prior_users,
        )


def ensure_user(sub: str, first_name: str = "", last_name: str = "") -> Dict:
    """Ensure a user record exists. Mirrors hr_server's pattern.

    Returns the user record. Auto-registers a sub seen for the first time;
    the named-user seed (``_SEED_USERS``) is loaded by ``reset_data`` so
    callers that hit a pre-seeded user just get the existing record back.
    """
    full_name = f"{first_name} {last_name}".strip()
    if sub not in users:
        users[sub] = {
            "sub": sub,
            "username": "",
            "email": "",
            "name": full_name,
            "first_seen": str(dt_date.today()),
        }
        logger.info(
            "[USER SEEN] sub=%s name=%s",
            sub, full_name or "(no name)",
        )
    return users[sub]


def get_assets_for_username(username: str) -> List[Dict]:
    """Return all assets currently assigned to *username*.

    Sprint 4 S4.2: replaces the legacy ``get_assets_for_employee(employee_id)``
    helper. Match is case-sensitive (usernames are pre-normalised in the seed).
    """
    if not username:
        return []
    return [a for a in assets if a["username"] == username]


def get_asset_by_id(asset_id: str) -> Dict | None:
    for a in assets:
        if a["asset_id"] == asset_id:
            return a
    return None


def lookup_user_by_username(username: str) -> Dict | None:
    """Return the user record whose ``username`` matches, or ``None``.

    Used by ``it_service.get_all_asset_assignments`` to surface ``email``
    alongside ``username`` in the report rows. The store carries both as
    soon as the named-user seed loads.
    """
    if not username:
        return None
    for record in users.values():
        if record.get("username") == username:
            return record
    return None


# S5.11 — canonical per-user key. Mirrors hr_server/service/store.user_key: the
# orchestrator-mcp-client token (token-A, used by the /api/me/assets sidebar
# proxy) carries an email-style sub <username>@example.com, while the it_agent
# CIBA token (token-C, used by the it.get_my_assets MCP tool) carries the
# user-id UUID — both must resolve to the same user. Unknown subs pass through.
_DEMO_USERNAME_TO_UUID: Dict[str, str] = {
    "employee_user": "2048ad8c-16a6-4ec1-bb63-b38300118f28",
    "hr_admin_user": "15fab9e7-18ec-4f6b-be0f-7aa1ddcebfb7",
}
_DEMO_UUIDS = frozenset(_DEMO_USERNAME_TO_UUID.values())


def user_key(sub: str) -> str:
    """Collapse a token ``sub`` to the canonical per-user key (the demo user-id
    UUID for the demo users; unchanged for anything else)."""
    if not sub:
        return ""
    if sub in _DEMO_UUIDS:
        return sub
    if "@" in sub:
        mapped = _DEMO_USERNAME_TO_UUID.get(sub.split("@", 1)[0])
        if mapped:
            return mapped
    return sub


def lookup_user_by_sub(sub: str) -> Dict | None:
    """Return the user record for a JWT ``sub`` (canonicalised), or ``None``.

    The self-service IT-asset path resolves ``sub`` → ``username`` here. Tokens
    from different OAuth apps emit different ``sub`` forms for the same user
    (UUID vs ``<username>@example.com``); ``user_key`` collapses them so the
    seed (keyed by UUID) is found either way.
    """
    if not sub:
        return None
    return users.get(user_key(sub)) or users.get(sub)


# Initialize on import.
reset_data()
