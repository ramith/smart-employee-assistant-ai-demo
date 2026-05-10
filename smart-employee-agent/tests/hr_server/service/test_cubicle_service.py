"""Tests for hr_service cubicle functions — Sprint 4 S4.1 (UC-11).

Coverage targets:
    1. Seed shape: 100 cubicles across 4 floors, all initially vacant.
    2. ``get_cubicle_summary`` aggregates totals + vacant counts per floor.
    3. ``get_vacant_cubicles_on_floor(2)`` returns floor 2 IDs.
    4. ``get_vacant_cubicles_on_floor(invalid)`` returns ``invalid_floor`` error.
    5. ``assign_cubicle`` happy path: vacant → occupied, fields populated.
    6. ``assign_cubicle`` idempotent: same ``(cubicle_id, username)`` → success.
    7. ``assign_cubicle`` collision: different username → ``cubicle_already_occupied``.
    8. ``assign_cubicle`` unknown id → ``cubicle_not_found``.
    9. ``lookup_employee`` by username → found.
   10. ``lookup_employee`` by email (case-insensitive) → found.
   11. ``lookup_employee`` unknown → not found.
   12. ``get_my_cubicle`` for unassigned user → ``assigned: False``.
   13. ``get_my_cubicle`` for assigned user → cubicle_id + floor.
   14. ``get_all_cubicle_assignments`` excludes ``sub`` (security audit F-12).
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
# 1. Seed shape
# ---------------------------------------------------------------------------


def test_cubicles_seed_has_100_rows_across_4_floors() -> None:
    assert len(_store.cubicles) == 100
    floors = {row["floor"] for row in _store.cubicles}
    assert floors == {1, 2, 3, 4}
    # 25 per floor.
    for floor in range(1, 5):
        rows = [r for r in _store.cubicles if r["floor"] == floor]
        assert len(rows) == 25
    # All initially vacant.
    assert all(not r["occupied"] for r in _store.cubicles)
    # IDs are C-001 .. C-100.
    ids = [r["cubicle_id"] for r in _store.cubicles]
    assert ids[0] == "C-001"
    assert ids[-1] == "C-100"


# ---------------------------------------------------------------------------
# 2. Summary aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_aggregates_per_floor_initially_all_vacant() -> None:
    summary = await _svc.get_cubicle_summary()
    assert set(summary.keys()) == {"floor_1", "floor_2", "floor_3", "floor_4"}
    for k, v in summary.items():
        assert v == {"total": 25, "vacant": 25}, f"unexpected {k}={v}"


# ---------------------------------------------------------------------------
# 3. Floor filter happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vacant_floor_2_returns_25_ids() -> None:
    result = await _svc.get_vacant_cubicles_on_floor(2)
    assert result["floor"] == 2
    assert "error" not in result
    assert len(result["vacant"]) == 25
    assert "C-026" in result["vacant"]
    assert "C-050" in result["vacant"]


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
        sub="jane.doe-sub-uuid-0003",
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
    assert row["assigned_to_sub"] == "jane.doe-sub-uuid-0003"


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
# 9. lookup_employee — by username.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_employee_by_username() -> None:
    result = await _svc.lookup_employee("jane.doe")
    assert result["found"] is True
    assert result["username"] == "jane.doe"
    assert result["email"] == "jane.doe@example.com"
    assert result["sub"]  # non-empty


# ---------------------------------------------------------------------------
# 10. lookup_employee — by email (case-insensitive).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_employee_by_email_case_insensitive() -> None:
    result = await _svc.lookup_employee("JANE.DOE@example.com")
    assert result["found"] is True
    assert result["username"] == "jane.doe"


# ---------------------------------------------------------------------------
# 11. lookup_employee — unknown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_employee_unknown_returns_not_found() -> None:
    result = await _svc.lookup_employee("nonexistent.person")
    assert result.get("found") is False


# ---------------------------------------------------------------------------
# 12. get_my_cubicle — unassigned.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_cubicle_unassigned() -> None:
    result = await _svc.get_my_cubicle("jane.doe")
    assert result == {"assigned": False}


# ---------------------------------------------------------------------------
# 13. get_my_cubicle — after assignment.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_cubicle_after_assignment() -> None:
    await _svc.assign_cubicle(
        cubicle_id="C-027",
        employee_username="jane.doe",
        employee_email="jane.doe@example.com",
        sub="sub-1",
    )
    result = await _svc.get_my_cubicle("jane.doe")
    assert result["assigned"] is True
    assert result["cubicle_id"] == "C-027"
    assert result["floor"] == 2


# ---------------------------------------------------------------------------
# 14. get_all_cubicle_assignments — never returns ``sub`` (F-12).
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
    assert len(rows) == 1
    row = rows[0]
    assert "sub" not in row
    assert "assigned_to_sub" not in row
    assert row["username"] == "jane.doe"
    assert row["cubicle_id"] == "C-027"
    assert row["floor"] == 2
