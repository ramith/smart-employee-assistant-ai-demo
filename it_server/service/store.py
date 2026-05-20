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


# ─── Hardware Allocation Policy (static, public-facing) ─────────────────────
#
# This constant is the single source of truth for the hardware policy.
# It is embedded verbatim in orchestrator/chat/public_handler.py so the
# pre-login Info Bot can answer hardware questions without calling this server.
# KEEP BOTH COPIES IN SYNC. A Stage-10 snapshot test enforces the invariant.

_SEED_HARDWARE_POLICY = {
    "standard_allocation": {
        "description": (
            "Every permanent employee receives a standard hardware kit on their first day. "
            "The exact model depends on their role (see role_overrides below)."
        ),
        "default_kit": {
            "laptop": "MacBook Pro 14-inch (M4 Pro, 24 GB RAM, 512 GB SSD)",
            "monitor": "27-inch 4K display (on-site employees only)",
            "peripherals": "Wireless keyboard and mouse",
            "phone": "iPhone 15 (128 GB)",
        },
    },
    "role_overrides": {
        "Engineer / Developer": {
            "laptop": "MacBook Pro 14-inch (M4 Pro, 36 GB RAM, 1 TB SSD)",
            "monitor": "Dual 27-inch 4K displays (on-site employees only)",
            "peripherals": "Wireless keyboard, mouse, and USB-C hub",
            "phone": "iPhone 15 Pro (256 GB)",
            "notes": "Developers may request a Linux workstation in place of the MacBook.",
        },
        "Management / Executive": {
            "laptop": "MacBook Pro 16-inch (M4 Max, 48 GB RAM, 1 TB SSD)",
            "monitor": "32-inch 4K display (on-site employees only)",
            "peripherals": "Wireless keyboard, mouse, and USB-C hub",
            "phone": "iPhone 15 Pro Max (256 GB)",
        },
        "Operations / Finance / HR": {
            "laptop": "MacBook Pro 14-inch (M4, 16 GB RAM, 512 GB SSD)",
            "monitor": "27-inch 4K display (on-site employees only)",
            "peripherals": "Wireless keyboard and mouse",
            "phone": "iPhone 15 (128 GB)",
        },
    },
    "remote_first": {
        "description": (
            "Employees designated Remote-First receive the laptop and phone for their role "
            "only. Monitors and peripherals are not provided — a Home Office Allowance of "
            "AED 1,500 (one-time) is available to purchase a personal monitor."
        ),
    },
    "replacement_cycle": {
        "laptop": {
            "years": 3,
            "notes": (
                "A replacement is issued if the device fails a hardware assessment before the "
                "3-year mark. Trade-in of the old device is mandatory."
            ),
        },
        "phone": {
            "years": 2,
            "notes": "Phones are replaced on the standard 2-year cycle or on carrier contract renewal.",
        },
        "monitor": {
            "years": 5,
            "notes": "Monitors are replaced only on failure or role change that requires an upgrade.",
        },
    },
    "request_process": {
        "new_hire": (
            "Hardware is pre-ordered by HR Admin when the onboarding checklist is completed "
            "(UC-18). No action required from the employee on Day 1."
        ),
        "replacement_or_additional": (
            "Raise a request through the IT Help Desk portal or contact your line manager. "
            "Approvals: line manager + IT Admin. Typical turnaround: 3–5 business days."
        ),
        "lost_or_stolen": (
            "Report immediately to IT Security and your line manager. The device will be "
            "remotely wiped. A loaner may be issued pending investigation."
        ),
    },
    "personal_device_policy": (
        "Personal devices (BYOD) are not permitted to access corporate systems unless "
        "enrolled in Mobile Device Management (MDM). Speak to IT Security for enrolment."
    ),
}


def get_hardware_policy() -> Dict:
    """Return the hardware allocation policy (static, same for all employees)."""
    return dict(_SEED_HARDWARE_POLICY)


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


def catalogue_entry_by_id(asset_id: str) -> Dict | None:
    """Return the *catalogue* entry for ``asset_id`` (``model`` / ``type`` /
    ``available_count``), or ``None`` if it isn't a catalogued asset."""
    if not asset_id:
        return None
    for c in _ASSET_CATALOGUE:
        if c["asset_id"] == asset_id:
            return c
    return None


def record_issuance(
    asset_id: str,
    username: str,
    *,
    model: str = "",
    asset_type: str = "",
) -> Dict:
    """Create — or move — an asset-assignment row, and return it.

    A physical asset has at most one holder, so the match is on ``asset_id``:
    an existing row is reassigned to ``username``; otherwise a new row is
    appended. ``status`` is set to ``"outstanding"``. When the id is
    catalogued and this is a *new* assignment, ``available_count`` is
    decremented (floor 0). Stdlib-only, no I/O — the store is in-memory.
    """
    for a in assets:
        if a["asset_id"] == asset_id:
            a["username"] = username
            a["status"] = "outstanding"
            if model:
                a["model"] = model
            if asset_type:
                a["type"] = asset_type
            logger.info(
                "[ASSET ISSUED] asset_id=%s reassigned -> username=%s", asset_id, username
            )
            return a
    row = {
        "asset_id": asset_id,
        "username": username,
        "type": asset_type,
        "model": model or asset_id,
        "status": "outstanding",
    }
    assets.append(row)
    cat = catalogue_entry_by_id(asset_id)
    if cat is not None and int(cat.get("available_count", 0)) > 0:
        cat["available_count"] = int(cat["available_count"]) - 1
    logger.info(
        "[ASSET ISSUED] asset_id=%s -> username=%s type=%s",
        asset_id, username, asset_type or "(uncatalogued)",
    )
    return row


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
