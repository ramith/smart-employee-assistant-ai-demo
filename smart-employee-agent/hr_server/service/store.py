"""
 Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.

  In-Memory HR Data Store

  All user-specific data is keyed by the JWT `sub` claim. Since S5.12 every
  OAuth app in the demo asserts `email` as the OIDC subject (Subject type =
  Public), so `sub` is `<emailaddress>` for users that have one and the
  user-id UUID otherwise — consistent across token-A (REST proxy) and token-C
  (agent CIBA/OBO). See `docs/architecture/identity-subject-mismatch.md`.
  Users are auto-registered on first interaction from JWT claims: the REST
  auth path carries ``username``/``email`` (token-A profile claims) into
  ``ensure_user`` so ``lookup_employee`` and the report username→email joins
  work for any real user; OBO/CIBA tool calls (token-C — ``sub`` only) reuse
  whatever record the REST path already created.
  Global data (holidays, leave policy) is pre-populated seed data.
  User data (requests, balances) starts empty.

  ``cubicles``: 100 entries across 4 floors (25 per floor, IDs ``C-001`` ..
  ``C-100``), all VACANT on a fresh start — assignments are made live
  (UC-11 ``assign_cubicle``). Each row carries the assigned-to fields
  (username + email + sub + assigned_at); ``assigned_to_sub`` is internal
  join data only — never returned by any report endpoint (sprint-4.md §7).
  There is no seeded named-user / pre-assigned-cubicle data — the demo runs
  against whatever IS users sign in, not a fixed roster (S5.12).
"""

import copy
import logging
from datetime import date as dt_date
from typing import Dict, List

logger = logging.getLogger(__name__)

# ─── Global Seed Data (static, same for all users) ──────────────────────────

_SEED_LEAVE_POLICY = {
    "Annual Leave": {
        "max_days_per_year": 20,
        "requires_approval": True,
        "min_notice_days": 7,
        "description": "Paid annual vacation leave",
    },
    "Sick Leave": {
        "max_days_per_year": 10,
        "requires_approval": True,
        "min_notice_days": 0,
        "description": "Medical leave. Certificate required for 3+ consecutive days.",
    },
    "Personal Leave": {
        "max_days_per_year": 5,
        "requires_approval": True,
        "min_notice_days": 3,
        "description": "Unpaid personal leave for emergencies or personal matters",
    },
}

_SEED_HOLIDAYS = [
    {"date": "2026-01-01", "name": "New Year's Day"},
    {"date": "2026-03-20", "name": "Eid Al Fitr (expected)"},
    {"date": "2026-05-27", "name": "Arafat Day (expected)"},
    {"date": "2026-05-28", "name": "Eid Al Adha (expected)"},
    {"date": "2026-07-18", "name": "Islamic New Year (expected)"},
    {"date": "2026-12-01", "name": "Commemoration Day"},
    {"date": "2026-12-02", "name": "UAE National Day"},
]

_DEFAULT_LEAVE_BALANCE = {
    "annual": 20,
    "sick": 10,
    "personal": 5,
}

# Per-user state in these stores is keyed directly by the token ``sub`` claim.
#
# History (S5.11 → S5.12): the OAuth apps used to emit a *different* ``sub`` for
# the same user — ``orchestrator-mcp-client`` (token-A, behind the SPA-proxied
# REST endpoints) used an email subject, while the specialist-agent CIBA apps
# (token-C, used by the MCP tools) used the WSO2 user-id UUID — so a leave
# applied via an agent was invisible to "My Leaves". That divergence was closed
# at the IdP: ``hr-agent`` / ``it-agent`` (and ``orchestrator-mcp-client``) all
# assert ``email`` as the OIDC subject (Subject type = Public), so token-A.sub ==
# token-C.sub for any user with an ``emailaddress`` attribute, and falls back to
# the user-id UUID consistently on *both* sides for users without one. So there
# is no ``user_key`` shim and no hard-coded user map — callers key on ``sub``
# verbatim. See ``docs/architecture/identity-subject-mismatch.md``.


def _seed_cubicles() -> List[Dict]:
    """Build the 100-cubicle seed (4 floors, 25 each), all VACANT.

    Distribution: floor 1 → C-001..C-025, floor 2 → C-026..C-050,
    floor 3 → C-051..C-075, floor 4 → C-076..C-100. No cubicle is
    pre-assigned — assignments are made live via ``assign_cubicle`` so the
    demo data tracks whoever actually signs in (S5.12).
    """
    rows: List[Dict] = []
    for n in range(1, 101):
        floor = ((n - 1) // 25) + 1
        rows.append(
            {
                "cubicle_id": f"C-{n:03d}",
                "floor": floor,
                "occupied": False,
                "assigned_to_username": None,
                "assigned_to_email": None,
                "assigned_to_sub": None,
                "assigned_at": None,
            }
        )
    return rows


# ─── Mutable In-Memory Stores ────────────────────────────────────────────────

leave_policy: Dict = {}
holidays: List = []

# User data — keyed by JWT sub
users: Dict[str, Dict] = {}            # sub -> {name, sub, first_seen, ...}
leave_balances: Dict[str, Dict] = {}   # sub -> {annual, sick, personal}
leave_requests: Dict[str, Dict] = {}   # request_id -> {user_sub, user_name, ...}
leave_request_counter: int = 0

# Sprint 4 S4.1 — flat list (100 rows). Lookups iterate; demo scale.
cubicles: List[Dict] = []


def reset_data() -> None:
    """Reset all stores. Global data re-seeded; user data cleared."""
    global leave_policy, holidays, users, leave_balances, leave_requests
    global leave_request_counter, cubicles
    prior_users = len(users) if users else 0
    prior_requests = len(leave_requests) if leave_requests else 0
    leave_policy = copy.deepcopy(_SEED_LEAVE_POLICY)
    holidays = copy.deepcopy(_SEED_HOLIDAYS)
    users = {}
    leave_balances = {}
    leave_requests = {}
    leave_request_counter = 0
    cubicles = _seed_cubicles()  # all vacant; assignments happen live
    if prior_users or prior_requests:
        logger.warning(
            "[STORE RESET] HR data reset — cleared %d user(s) and %d leave request(s); seed data re-applied",
            prior_users, prior_requests,
        )


def next_request_id() -> str:
    """Allocate the next leave-request reference ID (e.g., LR007)."""
    global leave_request_counter
    leave_request_counter += 1
    return f"LR{leave_request_counter:03d}"


def default_balance() -> Dict[str, int]:
    """A fresh copy of the default per-user leave balance."""
    return copy.deepcopy(_DEFAULT_LEAVE_BALANCE)


def ensure_user(
    sub: str,
    first_name: str = "",
    last_name: str = "",
    *,
    username: str | None = None,
    email: str | None = None,
) -> Dict:
    """Ensure a user record exists. Creates one with defaults if new.

    Called on every identity-aware tool invocation. ``username``/``email`` are
    the token's profile claims when available (the REST auth path / token-A
    carries them; OBO/CIBA tool calls — token-C — pass ``None`` and don't
    overwrite). Persisting them is what lets ``lookup_employee`` and the
    report username→email joins resolve real users without a hard-coded seed.
    Returns the user record.
    """
    full_name = f"{first_name} {last_name}".strip()
    if sub not in users:
        users[sub] = {
            "first_name": first_name,
            "last_name": last_name,
            "name": full_name,
            "sub": sub,
            "username": username or "",
            "email": email or "",
            "first_seen": str(dt_date.today()),
        }
        leave_balances[sub] = default_balance()
        logger.info(
            "[USER AUTO-REGISTERED] sub=%s name=%s default_balance=%s",
            sub, full_name or "(no name)", _DEFAULT_LEAVE_BALANCE,
        )
        return users[sub]
    record = users[sub]
    if full_name and full_name != record.get("name"):
        # Update name if it changed in the IdP.
        old_name = record.get("name")
        record["first_name"] = first_name
        record["last_name"] = last_name
        record["name"] = full_name
        logger.info("[USER NAME UPDATED] sub=%s old=%s new=%s", sub, old_name, full_name)
    # Backfill / refresh profile claims if this call carried them and the
    # stored record was created without (e.g. a token-C tool call landed first).
    if username and not record.get("username"):
        record["username"] = username
    if email and not record.get("email"):
        record["email"] = email
    return record


# Initialize on import so the server starts with seed data.
reset_data()
