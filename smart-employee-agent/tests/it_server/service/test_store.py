"""Tests for it_server/service/store.py + it_service.py — S5.12 (no seed roster).

Coverage targets:
    1. Store starts empty — no seeded assets, no seeded users.
    2. ``ensure_user`` registers a user from token claims (``username``/``email``);
       a later token-C-style call (no profile claims) doesn't clobber them.
    3. ``lookup_user_by_sub`` / ``lookup_user_by_username`` resolve a registered
       user; unknown → ``None``.
    4. ``get_my_assets(username)`` returns the right rows, ``{assets, total}`` shape;
       resolving ``sub`` → ``username`` works once the user is registered.
    5. ``get_my_assets("")`` / unknown username returns ``{assets: [], total: 0}``.
    6. ``get_all_asset_assignments`` joins ``email`` from users, never surfaces
       ``sub`` (sprint-4.md §7).
    7. ``it_service.issue_asset`` / ``store.record_issuance`` persist the
       assignment (S5.17 — the write path was a no-op stub): fills model/type
       from the catalogue, normalises an email recipient, re-issue moves the
       holder, uncatalogued ids are still recorded, ``available_count`` is
       decremented exactly once.
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


def _issue(asset_id: str, username: str, type_: str = "laptop",
           model: str = "MBP 14 M3", status: str = "outstanding") -> None:
    """Append an asset row directly (stand-in for the live issuance flow)."""
    _store.assets.append(
        {"asset_id": asset_id, "username": username, "type": type_,
         "model": model, "status": status}
    )


# ---------------------------------------------------------------------------
# 1. Store starts empty — no seed roster, no seed assets.
# ---------------------------------------------------------------------------


def test_store_starts_empty() -> None:
    assert _store.assets == []
    assert _store.users == {}


# ---------------------------------------------------------------------------
# 2. ensure_user registers from token claims; token-C call doesn't clobber.
# ---------------------------------------------------------------------------


def test_ensure_user_persists_profile_claims() -> None:
    rec = _store.ensure_user(
        "alice@example.com", "Alice", "Smith",
        username="alice", email="alice@example.com",
    )
    assert rec["sub"] == "alice@example.com"
    assert rec["username"] == "alice"
    assert rec["email"] == "alice@example.com"
    assert rec["name"] == "Alice Smith"
    # A later OBO/CIBA-style call (no profile claims) must not wipe them.
    again = _store.ensure_user("alice@example.com", "", "")
    assert again["username"] == "alice"
    assert again["email"] == "alice@example.com"
    # If the record was created without a username, a later call backfills it.
    _store.ensure_user("bob@example.com", "Bob", "Jones")
    assert _store.users["bob@example.com"]["username"] == ""
    _store.ensure_user("bob@example.com", "Bob", "Jones", username="bob", email="bob@example.com")
    assert _store.users["bob@example.com"]["username"] == "bob"


# ---------------------------------------------------------------------------
# 3. lookup_user_by_sub / by_username.
# ---------------------------------------------------------------------------


def test_lookup_user_by_sub_and_username() -> None:
    assert _store.lookup_user_by_sub("alice@example.com") is None
    assert _store.lookup_user_by_username("alice") is None
    _store.ensure_user("alice@example.com", "Alice", "Smith", username="alice", email="alice@example.com")
    by_sub = _store.lookup_user_by_sub("alice@example.com")
    assert by_sub is not None and by_sub["username"] == "alice"
    by_name = _store.lookup_user_by_username("alice")
    assert by_name is not None and by_name["sub"] == "alice@example.com"
    assert _store.lookup_user_by_sub("nobody@example.com") is None


# ---------------------------------------------------------------------------
# 4. get_my_assets — by username and resolved from sub.
# ---------------------------------------------------------------------------


def test_get_my_assets_by_username() -> None:
    _issue("AST-1", "alice")
    _issue("AST-2", "alice", type_="phone", model="iPhone 15", status="returned")
    _issue("AST-3", "bob")
    result = _svc.get_my_assets("alice")
    assert result["total"] == 2
    assert {a["asset_id"] for a in result["assets"]} == {"AST-1", "AST-2"}


def test_get_my_assets_resolves_username_from_sub() -> None:
    # token-C carries only `sub`; the REST auth path registered `username`.
    _store.ensure_user("alice@example.com", "Alice", "Smith", username="alice", email="alice@example.com")
    _issue("AST-1", "alice")
    result = _svc.get_my_assets("", sub="alice@example.com")
    assert result["total"] == 1
    assert result["assets"][0]["asset_id"] == "AST-1"


# ---------------------------------------------------------------------------
# 5. get_my_assets returns empty for unknown / empty username.
# ---------------------------------------------------------------------------


def test_get_my_assets_unknown_username_returns_empty() -> None:
    result = _svc.get_my_assets("nobody.here")
    assert result == {"assets": [], "total": 0}


def test_get_my_assets_empty_username_returns_empty() -> None:
    result = _svc.get_my_assets("")
    assert result == {"assets": [], "total": 0}


# ---------------------------------------------------------------------------
# 6. get_all_asset_assignments joins email, never surfaces sub.
# ---------------------------------------------------------------------------


def test_get_all_asset_assignments_joins_email_no_sub() -> None:
    _store.ensure_user("alice@example.com", "Alice", "Smith", username="alice", email="alice@example.com")
    _issue("AST-1", "alice")
    rows = _svc.get_all_asset_assignments()
    assert len(rows) == len(_store.assets) == 1
    row = rows[0]
    assert row["asset_id"] == "AST-1"
    assert row["username"] == "alice"
    assert row["email"] == "alice@example.com"
    # Critical: ``sub`` must NEVER appear in a report row.
    for r in rows:
        assert "sub" not in r


# ---------------------------------------------------------------------------
# 7. issue_asset / record_issuance — the write path persists the assignment.
# ---------------------------------------------------------------------------


def test_issue_asset_persists_and_fills_from_catalogue() -> None:
    cat_before = _store.catalogue_entry_by_id("MBP-14-001")
    assert cat_before is not None
    start = int(cat_before["available_count"])
    row = _svc.issue_asset("MBP-14-001", "alice")
    assert row == {
        "asset_id": "MBP-14-001", "username": "alice", "type": "laptop",
        "model": "MacBook Pro 14", "status": "outstanding",
    }
    # It is now visible to the employee panel and the report.
    assert _svc.get_my_assets("alice")["total"] == 1
    assert any(r["asset_id"] == "MBP-14-001" for r in _svc.get_all_asset_assignments())
    # A new catalogued issuance decrements available_count.
    assert int(_store.catalogue_entry_by_id("MBP-14-001")["available_count"]) == start - 1


def test_issue_asset_normalises_email_recipient() -> None:
    _svc.issue_asset("PHN-IP15-001", "alice@example.com")
    rows = _store.get_assets_for_username("alice")
    assert len(rows) == 1 and rows[0]["asset_id"] == "PHN-IP15-001"
    assert _store.get_assets_for_username("alice@example.com") == []


def test_issue_asset_reissue_moves_holder_no_double_count() -> None:
    start = int(_store.catalogue_entry_by_id("MBP-14-001")["available_count"])
    _svc.issue_asset("MBP-14-001", "alice")
    _svc.issue_asset("MBP-14-001", "bob")  # same physical asset → moves to bob
    assert _store.get_assets_for_username("alice") == []
    assert len(_store.get_assets_for_username("bob")) == 1
    assert len([a for a in _store.assets if a["asset_id"] == "MBP-14-001"]) == 1
    # Only the *first* (new-row) issuance decremented the catalogue.
    assert int(_store.catalogue_entry_by_id("MBP-14-001")["available_count"]) == start - 1


def test_issue_asset_uncatalogued_id_still_recorded() -> None:
    row = _svc.issue_asset("AST-99999", "alice")
    assert row["asset_id"] == "AST-99999"
    assert row["username"] == "alice"
    assert row["model"] == "AST-99999"  # falls back to the id
    assert row["status"] == "outstanding"
    assert _svc.get_my_assets("alice")["total"] == 1
