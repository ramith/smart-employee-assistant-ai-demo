"""In-Memory IT Asset Store.

Mirrors hr_server/service/store.py — same shape, same auto-registration
pattern, same logging style.

Asset rows look like ``{asset_id, username, type, model, status}`` (keyed by
``username``). There is no seeded asset-assignment or named-user data — the
store starts empty for user-specific state and is populated live: the REST
auth path carries the ``username``/``email`` profile claims (token-A) into
``ensure_user`` so ``it_service.get_my_assets`` (and the joined reporting
helper) can resolve ``sub`` → ``username`` → ``email`` for whoever signs in,
and asset assignments are created via the issuance tool at runtime (S5.12).
The asset *catalogue* (``_ASSET_CATALOGUE`` — what's available to issue) is
generic and stays.
"""
import copy
import logging
from datetime import date as dt_date
from typing import Dict, List

logger = logging.getLogger(__name__)

# ─── Global Seed Data ────────────────────────────────────────────────────────
#
# No pre-assigned assets — assignments are made live via the issuance flow so
# the demo data tracks whoever actually signs in (not a fixed roster).
_SEED_ASSETS: List[Dict] = []

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
# Keyed by the JWT ``sub`` claim. Each value is
# ``{username, email, sub, name, first_seen}``. Populated only at runtime:
# the REST auth path calls ``ensure_user`` with the token-A profile claims, so
# ``lookup_user_by_sub`` (used by the token-C ``it.get_my_assets`` MCP tool,
# which has only ``sub``) can resolve ``sub`` → ``username``. Both token forms
# carry the same ``sub`` for a user since S5.12 (see hr_server/service/store).
users: Dict[str, Dict] = {}


def reset_data() -> None:
    """Reset all stores. Asset seed re-applied; user data cleared."""
    global assets, users
    prior_users = len(users) if users else 0
    assets = copy.deepcopy(_SEED_ASSETS)
    users = {}
    if prior_users:
        logger.warning(
            "[STORE RESET] IT data reset — cleared %d user(s); seed assets re-applied",
            prior_users,
        )


def ensure_user(
    sub: str,
    first_name: str = "",
    last_name: str = "",
    *,
    username: str | None = None,
    email: str | None = None,
) -> Dict:
    """Ensure a user record exists. Mirrors hr_server's pattern.

    ``username``/``email`` are the token's profile claims when available — the
    REST auth path / token-A carries them; OBO/CIBA tool calls (token-C) pass
    ``None`` and don't overwrite. Returns the user record.
    """
    full_name = f"{first_name} {last_name}".strip()
    if sub not in users:
        users[sub] = {
            "sub": sub,
            "username": username or "",
            "email": email or "",
            "name": full_name,
            "first_seen": str(dt_date.today()),
        }
        logger.info(
            "[USER SEEN] sub=%s username=%s name=%s",
            sub, username or "(none)", full_name or "(no name)",
        )
        return users[sub]
    record = users[sub]
    if full_name and full_name != record.get("name"):
        record["name"] = full_name
    if username and not record.get("username"):
        record["username"] = username
    if email and not record.get("email"):
        record["email"] = email
    return record


def get_assets_for_username(username: str) -> List[Dict]:
    """Return all assets currently assigned to *username*.

    Match is exact (case-sensitive) — never substring/startswith, or one
    user's assets could leak to another.
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
    alongside ``username`` in the report rows. Match is exact.
    """
    if not username:
        return None
    for record in users.values():
        if record.get("username") == username:
            return record
    return None


def lookup_user_by_sub(sub: str) -> Dict | None:
    """Return the user record for a JWT ``sub``, or ``None``.

    The self-service IT-asset path resolves ``sub`` → ``username`` here.
    token-A (REST proxy) and token-C (it_agent CIBA) carry the same ``sub``
    for a given user since S5.12, so this is a direct lookup. Returns ``None``
    until the user has been seen on the REST auth path (which populates
    ``username``/``email`` from the token-A profile claims).
    """
    if not sub:
        return None
    return users.get(sub)


# Initialize on import.
reset_data()
