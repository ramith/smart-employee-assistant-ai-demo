"""
 Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.

  HR Business Logic

  Pure async functions that operate against the in-memory `store`.
  Shared by the MCP tool layer and the REST dashboard handler — no
  framework imports here, just data and policy.
"""

from datetime import date as dt_date
from typing import Dict, List, Optional

# Sprint 4 S4.0 (Track B): runtime reconciliation. The previous orphan-state
# `from service import store` only resolved when the cwd was hr_server/; under
# proper package wiring (hr_server.main mounting the rest router), the
# fully-qualified package path is required.
from hr_server.service import store


# ─── hr_basic ───────────────────────────────────────────────────────────────

async def get_holidays() -> List[Dict]:
    """Return all company holidays."""
    return [{"date": h["date"], "name": h["name"]} for h in store.holidays]


async def get_leave_policy() -> List[Dict]:
    """Return all leave policy types with their rules."""
    return [
        {
            "leave_type": lt,
            "max_days_per_year": p["max_days_per_year"],
            "requires_approval": p["requires_approval"],
            "min_notice_days": p["min_notice_days"],
            "description": p["description"],
        }
        for lt, p in store.leave_policy.items()
    ]


# ─── hr_self ────────────────────────────────────────────────────────────────

async def get_my_leave_balance(sub: str, first_name: str, last_name: str = "") -> Dict:
    """Get leave balance for the authenticated user. Auto-registers if new."""
    sub = store.user_key(sub)  # S5.11: canonicalise so token-A and token-C agree
    user = store.ensure_user(sub, first_name, last_name)
    balance = store.leave_balances[sub]
    return {
        "employee": user["name"],
        "balance": {
            "annual": balance["annual"],
            "sick": balance["sick"],
            "personal": balance["personal"],
        },
    }


async def get_my_leave_requests(sub: str, first_name: str, last_name: str = "") -> List[Dict]:
    """Get all leave requests for the authenticated user."""
    sub = store.user_key(sub)  # S5.11: canonicalise (My Leaves panel reads under token-A's sub)
    store.ensure_user(sub, first_name, last_name)
    return [
        {
            "request_id": req_id,
            "type": req["leave_type"],
            "start_date": req["start_date"],
            "end_date": req["end_date"],
            "days_requested": req["days_requested"],
            "status": req["status"],
            "reason": req["reason"],
        }
        for req_id, req in store.leave_requests.items()
        if req["user_sub"] == sub
    ]


async def apply_leave(
    sub: str,
    first_name: str,
    last_name: str,
    leave_type: str,
    start_date: str,
    end_date: str,
    reason: str,
) -> Dict:
    """Submit a new leave request for the authenticated user."""
    sub = store.user_key(sub)  # S5.11: store under the canonical key so My Leaves can read it back
    user = store.ensure_user(sub, first_name, last_name)

    if leave_type not in store.leave_policy:
        valid_types = ", ".join(store.leave_policy.keys())
        return {
            "error": "invalid_leave_type",
            "message": f"'{leave_type}' is not a valid leave type. Valid types: {valid_types}",
        }

    try:
        start = dt_date.fromisoformat(start_date)
        end = dt_date.fromisoformat(end_date)
    except ValueError:
        return {
            "error": "invalid_dates",
            "message": "Dates must be in YYYY-MM-DD format.",
        }

    days = (end - start).days + 1
    if days <= 0:
        return {
            "error": "invalid_dates",
            "message": "End date must be on or after start date.",
        }

    min_notice_days = store.leave_policy[leave_type].get("min_notice_days", 0)
    notice_days = (start - dt_date.today()).days
    if notice_days < min_notice_days:
        return {
            "error": "insufficient_notice",
            "message": (
                f"{leave_type} requires at least {min_notice_days} days notice; "
                f"start date is {notice_days} day(s) away."
            ),
        }

    balance = store.leave_balances[sub]
    balance_key = leave_type.split()[0].lower()  # "Annual Leave" -> "annual"
    if balance.get(balance_key, 0) < days:
        return {
            "error": "insufficient_balance",
            "message": (
                f"You only have {balance.get(balance_key, 0)} {leave_type} days remaining, "
                f"but requested {days} days."
            ),
        }

    new_id = store.next_request_id()
    store.leave_requests[new_id] = {
        "user_sub": sub,
        "user_name": user["name"],
        "leave_type": leave_type,
        "start_date": start_date,
        "end_date": end_date,
        "days_requested": days,
        "status": "Pending",
        "reason": reason,
        "reviewed_by_sub": None,
        "reviewed_by_name": None,
        "rejection_reason": None,
    }
    return {"success": True, "request_id": new_id}


# ─── hr_read ────────────────────────────────────────────────────────────────

async def get_all_leave_requests(
    status: Optional[str] = None,
    employee_name: Optional[str] = None,
) -> List[Dict]:
    """Get all leave requests with optional status and employee name filters."""
    results = []
    for req_id, req in store.leave_requests.items():
        if status and req["status"].lower() != status.lower():
            continue
        if employee_name and employee_name.lower() not in req["user_name"].lower():
            continue
        results.append({
            "request_id": req_id,
            "employee": req["user_name"],
            "type": req["leave_type"],
            "start_date": req["start_date"],
            "end_date": req["end_date"],
            "days_requested": req["days_requested"],
            "status": req["status"],
        })
    return results


async def get_leave_request_details(request_id: str) -> Optional[Dict]:
    """Get detailed info about a specific leave request."""
    req = store.leave_requests.get(request_id)
    if not req:
        return None
    balance = store.leave_balances.get(req["user_sub"], {})
    return {
        "request_id": request_id,
        "employee": req["user_name"],
        "type": req["leave_type"],
        "start_date": req["start_date"],
        "end_date": req["end_date"],
        "days_requested": req["days_requested"],
        "status": req["status"],
        "reason": req["reason"],
        "leave_balance": {
            "annual": balance.get("annual", 0),
            "sick": balance.get("sick", 0),
            "personal": balance.get("personal", 0),
        },
    }


# ─── hr_approve ─────────────────────────────────────────────────────────────

async def approve_leave_request(
    request_id: str, reviewer_sub: str, reviewer_name: str
) -> Dict:
    """Approve a pending leave request. Deducts from employee's balance."""
    req = store.leave_requests.get(request_id)
    if not req:
        return {
            "error": "not_found",
            "message": f"Leave request '{request_id}' not found.",
        }
    if req["status"] != "Pending":
        return {
            "error": "invalid_status",
            "message": f"Leave request {request_id} is already {req['status']}.",
        }

    # Deduct leave balance — reject if it would overdraw.
    balance = store.leave_balances.get(req["user_sub"])
    if balance:
        balance_key = req["leave_type"].split()[0].lower()
        if balance_key in balance:
            remaining = balance[balance_key]
            if remaining < req["days_requested"]:
                return {
                    "error": "insufficient_balance",
                    "message": (
                        f"Cannot approve {request_id}: {req['user_name']} only has "
                        f"{remaining} {balance_key} day(s) remaining, but the request "
                        f"is for {req['days_requested']} day(s)."
                    ),
                }
            balance[balance_key] = remaining - req["days_requested"]

    req["status"] = "Approved"
    req["reviewed_by_sub"] = reviewer_sub
    req["reviewed_by_name"] = reviewer_name

    return {
        "success": True,
        "request_id": request_id,
        "new_status": "Approved",
        "employee": req["user_name"],
        "notification": f"Leave request {request_id} for {req['user_name']} has been approved.",
    }


async def reject_leave_request(
    request_id: str, reason: str, reviewer_sub: str, reviewer_name: str
) -> Dict:
    """Reject a pending leave request with a reason."""
    req = store.leave_requests.get(request_id)
    if not req:
        return {
            "error": "not_found",
            "message": f"Leave request '{request_id}' not found.",
        }
    if req["status"] != "Pending":
        return {
            "error": "invalid_status",
            "message": f"Leave request {request_id} is already {req['status']}.",
        }

    req["status"] = "Rejected"
    req["reviewed_by_sub"] = reviewer_sub
    req["reviewed_by_name"] = reviewer_name
    req["rejection_reason"] = reason

    return {
        "success": True,
        "request_id": request_id,
        "new_status": "Rejected",
        "employee": req["user_name"],
        "notification": f"Leave request {request_id} for {req['user_name']} has been rejected.",
    }


# ─── Dashboard REST ─────────────────────────────────────────────────────────

async def get_leaves_for_dashboard(
    user_sub: Optional[str] = None,
    status: Optional[str] = None,
    employee_name: Optional[str] = None,
) -> List[Dict]:
    """Get leaves for dashboard.
    If user_sub is provided, returns only that user's requests (hr_self).
    Otherwise returns all requests with optional filters (hr_read).
    """
    results = []
    for req_id, req in store.leave_requests.items():
        if user_sub and req["user_sub"] != user_sub:
            continue
        if status and req["status"].lower() != status.lower():
            continue
        if employee_name and employee_name.lower() not in req["user_name"].lower():
            continue
        results.append({
            "employee": req["user_name"],
            "type": req["leave_type"],
            "start_date": req["start_date"],
            "end_date": req["end_date"],
            "days_requested": req["days_requested"],
            "status": req["status"],
        })
    return results


# ─── Cubicle service (Sprint 4 S4.1, UC-11) ─────────────────────────────────
#
# Identity model: ``username`` is the primary key, ``email`` is optional.
# ``sub`` is recorded internally for audit but never returned to chat or
# report endpoints (sprint-4.md §7).
#
# F-04 (security audit): assign_cubicle is idempotent for same-user re-calls;
# different-user collisions return ``cubicle_already_occupied`` with the
# current holder. Single-process / single-uvicorn-worker (BLOCK-I) keeps the
# in-process dict access serial — no TOCTOU within one process.


async def get_cubicle_summary() -> Dict:
    """Return per-floor counts: ``{floor_N: {total, vacant}}`` for floors 1..4."""
    summary: Dict[str, Dict[str, int]] = {
        f"floor_{n}": {"total": 0, "vacant": 0} for n in range(1, 5)
    }
    for row in store.cubicles:
        key = f"floor_{row['floor']}"
        if key not in summary:
            # Defensive — cubicle data is seeded with floor 1..4 only, but a
            # future seed change shouldn't break the aggregation.
            summary[key] = {"total": 0, "vacant": 0}
        summary[key]["total"] += 1
        if not row["occupied"]:
            summary[key]["vacant"] += 1
    return summary


async def get_vacant_cubicles_on_floor(floor: int) -> Dict:
    """Return the vacant-cubicle list for *floor*.

    Returns ``{error: "invalid_floor"}`` for floor outside 1..4 — keeps the
    handler 200-with-error-envelope contract consistent (auth passed; data
    request invalid).
    """
    if not isinstance(floor, int) or floor < 1 or floor > 4:
        return {"error": "invalid_floor", "message": f"Floor {floor!r} is not 1..4."}
    vacant = [
        row["cubicle_id"]
        for row in store.cubicles
        if row["floor"] == floor and not row["occupied"]
    ]
    vacant.sort()
    return {"floor": floor, "vacant": vacant}


async def get_my_cubicle(sub: str = "", username: str | None = None) -> Dict:
    """Return the caller's cubicle assignment, if any.

    Match priority: ``sub`` (the JWT subject — always present on OBO/CIBA
    tokens) then ``username`` (a fallback for tokens that carry the profile
    claim). Returns ``{assigned: False}`` when the caller has no cubicle.
    """
    # S5.11: canonicalise the sub on both sides so token-A's email-style sub
    # matches a seed assignment recorded under the UUID.
    sub_needle = store.user_key((sub or "").strip())
    name_needle = (username or "").strip().lower()
    if not sub_needle and not name_needle:
        return {"assigned": False}
    for row in store.cubicles:
        if not row["occupied"]:
            continue
        row_sub = store.user_key((row["assigned_to_sub"] or "").strip())
        row_name = (row["assigned_to_username"] or "").strip().lower()
        if (sub_needle and row_sub == sub_needle) or (name_needle and row_name == name_needle):
            return {
                "assigned": True,
                "cubicle_id": row["cubicle_id"],
                "floor": row["floor"],
                "assigned_at": row["assigned_at"],
            }
    return {"assigned": False}


async def assign_cubicle(
    cubicle_id: str,
    employee_username: str,
    employee_email: str,
    sub: Optional[str] = None,
) -> Dict:
    """Assign *cubicle_id* to (*employee_username*, *employee_email*, *sub*).

    Idempotency rule (Stage 5 §D3, Stage 8 F-04): if the cubicle is already
    held by the same username, return the existing record with
    ``success=True`` (no error). Different username → ``cubicle_already_occupied``.

    Note (F-04): demo POC, single uvicorn worker. Two concurrent admin
    browsers racing for the same ``(cubicle_id, employee_username)`` would
    both see ``success=True``; this is acceptable (the second CIBA was
    "wasted" but no data corruption). Production would use a per-cubicle
    lock or a transactional write here.
    """
    # Find target cubicle.
    target = None
    for row in store.cubicles:
        if row["cubicle_id"] == cubicle_id:
            target = row
            break
    if target is None:
        return {
            "error": "cubicle_not_found",
            "message": f"Cubicle {cubicle_id!r} does not exist.",
        }

    if target["occupied"]:
        existing = (target["assigned_to_username"] or "").strip().lower()
        incoming = (employee_username or "").strip().lower()
        if existing == incoming and incoming:
            # Idempotent — same user, return existing record.
            return {
                "success": True,
                "cubicle_id": target["cubicle_id"],
                "floor": target["floor"],
                "assigned_to": {
                    "username": target["assigned_to_username"],
                    "email": target["assigned_to_email"],
                },
                "assigned_at": target["assigned_at"],
            }
        return {
            "error": "cubicle_already_occupied",
            "current_holder": {
                "username": target["assigned_to_username"],
                "email": target["assigned_to_email"],
            },
        }

    # Vacant — assign now.
    from datetime import datetime as _dt, timezone as _tz
    now_iso = _dt.now(tz=_tz.utc).isoformat()
    target["occupied"] = True
    target["assigned_to_username"] = employee_username
    target["assigned_to_email"] = employee_email or None
    target["assigned_to_sub"] = sub
    target["assigned_at"] = now_iso
    return {
        "success": True,
        "cubicle_id": target["cubicle_id"],
        "floor": target["floor"],
        "assigned_to": {
            "username": employee_username,
            "email": employee_email or None,
        },
        "assigned_at": now_iso,
    }


async def lookup_employee(username_or_email: str) -> Dict:
    """Resolve a username-or-email to ``{found, username, email, sub}``.

    Email matching is case-insensitive; username matching is case-sensitive
    (usernames are pre-normalised lower-case in the seed). Returns
    ``{found: False}`` when no match.
    """
    if not username_or_email:
        return {"found": False}
    needle = username_or_email.strip()
    needle_lower = needle.lower()
    for record in store.users.values():
        username = record.get("username", "")
        email = record.get("email", "")
        if username and username == needle:
            return {
                "found": True,
                "username": username,
                "email": email or "",
                "sub": record.get("sub", ""),
            }
        if email and email.lower() == needle_lower:
            return {
                "found": True,
                "username": username or "",
                "email": email,
                "sub": record.get("sub", ""),
            }
    return {"found": False}


async def get_all_cubicle_assignments() -> List[Dict]:
    """Return all assigned cubicles as report rows (NEVER includes ``sub``).

    Used by the UC-16 Cubicles report; only username/email/cubicle_id/floor/
    assigned_at are surfaced. ``sub`` is internal join data only
    (sprint-4.md §7).
    """
    rows: List[Dict] = []
    for row in store.cubicles:
        if not row["occupied"]:
            continue
        rows.append({
            "username": row["assigned_to_username"],
            "email": row["assigned_to_email"],
            "cubicle_id": row["cubicle_id"],
            "floor": row["floor"],
            "assigned_at": row["assigned_at"],
        })
    rows.sort(key=lambda r: (r["floor"], r["cubicle_id"]))
    return rows
