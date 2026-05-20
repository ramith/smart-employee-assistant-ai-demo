"""Tests for hr_service cubicle functions — UC-11 (cubicles), S5.12 (no seed).

Coverage targets:
    1. Seed shape: 100 cubicles across 4 floors, ALL vacant on a fresh start
       (assignments are made live; no pre-assigned demo roster).
    2. ``get_cubicle_summary`` aggregates totals + vacant counts per floor.
    3. ``get_vacant_cubicles_on_floor(2)`` returns floor 2 IDs.
    4. ``get_vacant_cubicles_on_floor(invalid)`` returns ``invalid_floor`` error.
    5. ``assign_cubicle`` happy path: vacant → occupied, fields populated.
    6. ``assign_cubicle`` idempotent: same ``(cubicle_id, username)`` → success.
    7. ``assign_cubicle`` collision: different username → ``cubicle_already_occupied``.
    8. ``assign_cubicle`` unknown id → ``cubicle_not_found``.
    9. ``lookup_employee`` resolves a user registered via ``ensure_user`` by
       username and by email (case-insensitive); unknown → not found.
   10. ``get_my_cubicle`` for unassigned user → ``assigned: False``.
   11. ``get_my_cubicle`` for assigned user → cubicle_id + floor (matched by sub
       *and* by username).
   12. Per-user data keying: a leave/cubicle written under sub X is visible via
       sub X and NOT via a different sub (token-A and token-C carry the same
       ``sub`` for a user since S5.12 — no ``user_key`` shim, no hard-coded map).
   13. ``get_all_cubicle_assignments`` excludes ``sub`` (security audit F-12).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Module isolation (matches pattern used elsewhere in the suite)
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted: str) -> None:
    if dotted in sys.modules:
        return
    stub = types.ModuleType(dotted)
    stub.__package__ = dotted
    stub.__path__ = [str(_ROOT / dotted.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted] = stub


def _load(dotted: str, rel: str) -> types.ModuleType:
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, _ROOT / rel)
    assert spec and spec.loader, f"Cannot load {rel}"
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


for _pkg in ("hr_server", "hr_server.service"):
    _ensure_pkg(_pkg)

_store = _load("hr_server.service.store", "hr_server/service/store.py")
_svc = _load("hr_server.service.hr_service", "hr_server/service/hr_service.py")


# ---------------------------------------------------------------------------
# Per-test fixture — reset the in-memory store so each test is deterministic.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    _store.reset_data()
    yield
    _store.reset_data()


# ---------------------------------------------------------------------------
# 1. Seed shape — 100 cubicles, all vacant on a fresh start
# ---------------------------------------------------------------------------


def test_cubicles_seed_has_100_rows_across_4_floors_all_vacant() -> None:
    assert len(_store.cubicles) == 100
    floors = {row["floor"] for row in _store.cubicles}
    assert floors == {1, 2, 3, 4}
    # 25 per floor.
    for floor in range(1, 5):
        rows = [r for r in _store.cubicles if r["floor"] == floor]
        assert len(rows) == 25
    # No cubicle is pre-assigned — assignments happen live.
    occupied = [r for r in _store.cubicles if r["occupied"]]
    assert occupied == []
    for r in _store.cubicles:
        assert r["assigned_to_username"] is None
        assert r["assigned_to_email"] is None
        assert r["assigned_to_sub"] is None
        assert r["assigned_at"] is None
    # IDs are C-001 .. C-100.
    ids = [r["cubicle_id"] for r in _store.cubicles]
    assert ids[0] == "C-001"
    assert ids[-1] == "C-100"


# ---------------------------------------------------------------------------
# 2. Summary aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_aggregates_per_floor_all_vacant_on_fresh_start() -> None:
    summary = await _svc.get_cubicle_summary()
    assert set(summary.keys()) == {"floor_1", "floor_2", "floor_3", "floor_4"}
    for floor in ("floor_1", "floor_2", "floor_3", "floor_4"):
        assert summary[floor] == {"total": 25, "vacant": 25}


@pytest.mark.asyncio
async def test_summary_reflects_a_live_assignment() -> None:
    await _svc.assign_cubicle(
        cubicle_id="C-005", employee_username="alice",
        employee_email="alice@example.com", sub="alice@example.com",
    )
    summary = await _svc.get_cubicle_summary()
    assert summary["floor_1"] == {"total": 25, "vacant": 24}
    assert summary["floor_2"] == {"total": 25, "vacant": 25}


# ---------------------------------------------------------------------------
# 3. Floor filter happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vacant_floor_2_returns_all_25_then_24_after_assignment() -> None:
    result = await _svc.get_vacant_cubicles_on_floor(2)
    assert result["floor"] == 2
    assert "error" not in result
    assert len(result["vacant"]) == 25
    assert "C-026" in result["vacant"]
    assert "C-050" in result["vacant"]
    assert "C-030" in result["vacant"]

    await _svc.assign_cubicle(
        cubicle_id="C-030", employee_username="bob",
        employee_email="bob@example.com", sub="bob@example.com",
    )
    result = await _svc.get_vacant_cubicles_on_floor(2)
    assert len(result["vacant"]) == 24
    assert "C-030" not in result["vacant"]


# ---------------------------------------------------------------------------
# 4. Floor filter invalid input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vacant_floor_invalid_returns_error() -> None:
    result = await _svc.get_vacant_cubicles_on_floor(99)
    assert result.get("error") == "invalid_floor"


# ---------------------------------------------------------------------------
# 5. Assign happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_cubicle_happy_path() -> None:
    result = await _svc.assign_cubicle(
        cubicle_id="C-027",
        employee_username="jane.doe",
        employee_email="jane.doe@example.com",
        sub="jane.doe@example.com",
    )
    assert result["success"] is True
    assert result["cubicle_id"] == "C-027"
    assert result["floor"] == 2
    assert result["assigned_to"] == {
        "username": "jane.doe",
        "email": "jane.doe@example.com",
    }
    assert result["assigned_at"] is not None

    # Store now reflects the assignment.
    row = next(r for r in _store.cubicles if r["cubicle_id"] == "C-027")
    assert row["occupied"] is True
    assert row["assigned_to_username"] == "jane.doe"
    assert row["assigned_to_sub"] == "jane.doe@example.com"


# ---------------------------------------------------------------------------
# 6. Idempotent re-assign — same user.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_cubicle_idempotent_same_user() -> None:
    await _svc.assign_cubicle(
        cubicle_id="C-027",
        employee_username="jane.doe",
        employee_email="jane.doe@example.com",
        sub="sub-1",
    )
    again = await _svc.assign_cubicle(
        cubicle_id="C-027",
        employee_username="jane.doe",
        employee_email="jane.doe@example.com",
        sub="sub-1",
    )
    # Returns success with the existing record (no error).
    assert again["success"] is True
    assert again["cubicle_id"] == "C-027"


# ---------------------------------------------------------------------------
# 7. Collision — different username on already-occupied cubicle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_cubicle_collision_returns_already_occupied() -> None:
    await _svc.assign_cubicle(
        cubicle_id="C-027",
        employee_username="bob.smith",
        employee_email="bob.smith@example.com",
        sub="sub-2",
    )
    result = await _svc.assign_cubicle(
        cubicle_id="C-027",
        employee_username="jane.doe",
        employee_email="jane.doe@example.com",
        sub="sub-3",
    )
    assert result.get("error") == "cubicle_already_occupied"
    assert result["current_holder"]["username"] == "bob.smith"
    assert "success" not in result or not result.get("success")


# ---------------------------------------------------------------------------
# 8. Unknown cubicle id.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_cubicle_unknown_id_returns_not_found() -> None:
    result = await _svc.assign_cubicle(
        cubicle_id="C-999",
        employee_username="jane.doe",
        employee_email="jane.doe@example.com",
        sub="sub-1",
    )
    assert result.get("error") == "cubicle_not_found"


# ---------------------------------------------------------------------------
# 9. lookup_employee — resolves a user registered via ensure_user.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_employee_by_username_and_email() -> None:
    # No hard-coded roster — register a user the way the REST auth path does.
    _store.ensure_user(
        "jane.doe@example.com", "Jane", "Doe",
        username="jane.doe", email="jane.doe@example.com",
    )
    by_username = await _svc.lookup_employee("jane.doe")
    assert by_username["found"] is True
    assert by_username["username"] == "jane.doe"
    assert by_username["email"] == "jane.doe@example.com"
    assert by_username["sub"] == "jane.doe@example.com"

    by_email = await _svc.lookup_employee("JANE.DOE@example.com")
    assert by_email["found"] is True
    assert by_email["username"] == "jane.doe"


@pytest.mark.asyncio
async def test_lookup_employee_unknown_returns_not_found() -> None:
    result = await _svc.lookup_employee("nonexistent.person")
    assert result.get("found") is False


# ---------------------------------------------------------------------------
# 10. get_my_cubicle — unassigned.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_cubicle_unassigned() -> None:
    assert await _svc.get_my_cubicle(username="jane.doe") == {"assigned": False}
    assert await _svc.get_my_cubicle(sub="no-such-sub") == {"assigned": False}


# ---------------------------------------------------------------------------
# 11. get_my_cubicle — after assignment (matched by sub *and* by username).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_cubicle_after_assignment() -> None:
    await _svc.assign_cubicle(
        cubicle_id="C-027",
        employee_username="jane.doe",
        employee_email="jane.doe@example.com",
        sub="jane.doe@example.com",
    )
    by_sub = await _svc.get_my_cubicle(sub="jane.doe@example.com")
    assert by_sub["assigned"] is True
    assert by_sub["cubicle_id"] == "C-027"
    assert by_sub["floor"] == 2

    by_username = await _svc.get_my_cubicle(username="jane.doe")
    assert by_username["cubicle_id"] == "C-027"

    # A *different* sub must NOT see jane's cubicle.
    other = await _svc.get_my_cubicle(sub="someone.else@example.com")
    assert other == {"assigned": False}


# ---------------------------------------------------------------------------
# 12. Per-user data keying — same sub round-trips; a different sub does not.
#     (token-A and token-C carry the same `sub` for a user since S5.12.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leave_round_trips_under_same_sub_only() -> None:
    emp = "employee_user@example.com"
    other = "other_user@example.com"
    res = await _svc.apply_leave(
        sub=emp, first_name="Employee", last_name="User",
        leave_type="Sick Leave", start_date="2026-06-10", end_date="2026-06-12",
        reason="surgery",
    )
    assert res.get("success") is True
    rid = res["request_id"]
    mine = await _svc.get_my_leave_requests(sub=emp, first_name="", last_name="")
    assert any(r["request_id"] == rid for r in mine), mine
    theirs = await _svc.get_my_leave_requests(sub=other, first_name="", last_name="")
    assert all(r["request_id"] != rid for r in theirs)


@pytest.mark.asyncio
async def test_leave_balance_is_per_sub() -> None:
    emp = "employee_user@example.com"
    other = "other_user@example.com"
    result = await _svc.apply_leave(
        sub=emp, first_name="Employee", last_name="User",
        leave_type="Sick Leave", start_date="2026-07-01", end_date="2026-07-03", reason="x",
    )
    # Approve the leave so the balance is deducted (apply_leave creates Pending only).
    await _svc.approve_leave_request(
        request_id=result["request_id"],
        reviewer_sub="hr-admin-sub",
        reviewer_name="HR Admin",
    )
    b_emp = await _svc.get_my_leave_balance(emp, "Employee", "User")
    b_other = await _svc.get_my_leave_balance(other, "Other", "User")
    # Same record on repeat read; a different sub gets a fresh default balance.
    assert (await _svc.get_my_leave_balance(emp, "", ""))["balance"] == b_emp["balance"]
    assert b_other["balance"] == _store.default_balance()
    # emp had 3 sick days deducted; other still has full default sick balance.
    assert b_emp["balance"] != b_other["balance"]
    assert b_emp["balance"]["sick"] == _store.default_balance()["sick"] - 3


# ---------------------------------------------------------------------------
# 13. get_all_cubicle_assignments — never returns ``sub`` (F-12).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_cubicle_assignments_never_returns_sub() -> None:
    await _svc.assign_cubicle(
        cubicle_id="C-027",
        employee_username="jane.doe",
        employee_email="jane.doe@example.com",
        sub="jane-internal-sub",
    )
    rows = await _svc.get_all_cubicle_assignments()
    assert len(rows) == 1  # only the one just assigned; no seeded assignments
    for row in rows:
        assert "sub" not in row
        assert "assigned_to_sub" not in row
        assert {"username", "email", "cubicle_id", "floor", "assigned_at"} <= set(row.keys())
    jane_row = next(r for r in rows if r["username"] == "jane.doe")
    assert jane_row["cubicle_id"] == "C-027"
    assert jane_row["floor"] == 2
