"""Tests for it_server/service/store.py + it_service.py — Sprint 4 S4.2 (UC-12).

Coverage targets:
    1. Seed shape: ``_SEED_ASSETS`` rows are keyed by ``username``; no
       ``employee_id`` field anywhere in the seed.
    2. ``users`` dict pre-seeded with the four named demo users.
    3. ``get_my_assets(username)`` returns the right rows, ``{assets, total}`` shape.
    4. ``get_my_assets("")`` / unknown username returns ``{assets: [], total: 0}``.
    5. ``get_all_asset_assignments`` joins ``email`` from users, never surfaces
       ``sub`` (sprint-4.md §7).
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


for _pkg in ("it_server", "it_server.service"):
    _ensure_pkg(_pkg)

_store = _load("it_server.service.store", "it_server/service/store.py")
_svc = _load("it_server.service.it_service", "it_server/service/it_service.py")


# ---------------------------------------------------------------------------
# Per-test fixture — reset the in-memory store so each test is deterministic.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    _store.reset_data()
    yield
    _store.reset_data()


# ---------------------------------------------------------------------------
# 1. Seed shape — username-keyed, no employee_id field
# ---------------------------------------------------------------------------


def test_seed_assets_keyed_by_username_no_employee_id() -> None:
    """Sprint 4 S4.2: assets carry ``username``, never ``employee_id``."""
    assert len(_store.assets) >= 5
    for row in _store.assets:
        assert "username" in row
        assert "employee_id" not in row
        # Required fields per Stage 5 §E1
        assert {"asset_id", "username", "type", "model", "status"}.issubset(row.keys())


# ---------------------------------------------------------------------------
# 2. Users dict pre-seeded with named demo users
# ---------------------------------------------------------------------------


def test_users_seed_contains_named_demo_users() -> None:
    """The four named demo users (UC-11/UC-12) must be pre-loaded."""
    usernames = {rec["username"] for rec in _store.users.values()}
    assert {"employee_user", "hr_admin_user", "jane.doe", "bob.smith"}.issubset(
        usernames
    )
    # Every seeded user record carries username + email + sub.
    for rec in _store.users.values():
        assert rec.get("username")
        assert rec.get("email")
        assert rec.get("sub")


# ---------------------------------------------------------------------------
# 3. get_my_assets returns the right rows for a known username
# ---------------------------------------------------------------------------


def test_get_my_assets_returns_user_rows() -> None:
    """employee_user has 1 laptop + 1 phone in the seed."""
    result = _svc.get_my_assets("employee_user")
    assert result["total"] == 2
    asset_ids = {a["asset_id"] for a in result["assets"]}
    assert "AST-12345" in asset_ids
    assert "AST-12346" in asset_ids


# ---------------------------------------------------------------------------
# 4. get_my_assets returns empty for unknown / empty username
# ---------------------------------------------------------------------------


def test_get_my_assets_unknown_username_returns_empty() -> None:
    """Unknown username → empty list, not an error."""
    result = _svc.get_my_assets("nobody.here")
    assert result == {"assets": [], "total": 0}


def test_get_my_assets_empty_username_returns_empty() -> None:
    """Empty username (defensive) → empty list."""
    result = _svc.get_my_assets("")
    assert result == {"assets": [], "total": 0}


# ---------------------------------------------------------------------------
# 5. get_all_asset_assignments joins email, never surfaces sub
# ---------------------------------------------------------------------------


def test_get_all_asset_assignments_joins_email_no_sub() -> None:
    """Report rows include username + email; ``sub`` is internal-only (§7)."""
    rows = _svc.get_all_asset_assignments()
    assert len(rows) == len(_store.assets)
    # Find the employee_user laptop.
    laptops = [r for r in rows if r["asset_id"] == "AST-12345"]
    assert len(laptops) == 1
    row = laptops[0]
    assert row["username"] == "employee_user"
    assert row["email"] == "employee.user@example.com"
    # Critical: ``sub`` must NEVER appear in a report row.
    for r in rows:
        assert "sub" not in r
