"""Tests for orchestrator/agent_registry/cards.py — Wave 4, Sprint 1.

Coverage targets
----------------
1.  ``add()`` + ``get()`` round-trip — inserted card is retrievable by id.
2.  ``add()`` of duplicate id replaces the existing card (does not raise).
3.  ``all()`` reflects insertion order across multiple cards.
4.  ``find_by_tool()`` with a known tool_id returns the owning card.
5.  ``find_by_tool()`` with a non-existent tool_id returns ``None``.
6.  ``llm_tool_list()`` contains entries for every skill of every card.
7.  ``llm_tool_list()`` entries do NOT include ``base_url``, ``oauth_client_id``,
    or any other private field from ``AgentCard``.
8.  ``llm_tool_list()`` entries each carry ``agent_id`` and ``agent_label``.
9.  ``from_files()`` successfully parses two valid JSON card files.
10. ``from_files()`` skips a bad-JSON file and logs a WARNING.
11. ``from_files()`` returns an empty registry when all files are bad.
12. ``get()`` returns ``None`` for an unknown agent_id.
13. Empty registry returns an empty list from ``all()`` and ``llm_tool_list()``.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import pathlib
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers — same isolation technique used throughout the suite
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/
_FIXTURE_DIR = _ROOT / "tests" / "fixtures" / "agent_cards"


def _ensure_pkg(dotted_name: str) -> None:
    """Register a bare package stub in sys.modules if not already present."""
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    stub.__path__ = [str(_ROOT / dotted_name.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules under dotted_name."""
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]
    file_path = _ROOT / rel_path
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {file_path}"
    module = importlib.util.module_from_spec(spec)
    module.__package__ = dotted_name.rsplit(".", 1)[0] if "." in dotted_name else ""
    sys.modules[dotted_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# Ensure all intermediate package namespaces exist before loading modules.
for _pkg in ("common", "common.a2a", "orchestrator", "orchestrator.agent_registry"):
    _ensure_pkg(_pkg)

# Load Wave-2 dependency first, then the module under test.
_agent_card_mod = _load_module("common.a2a.agent_card", "common/a2a/agent_card.py")
_cards_mod = _load_module(
    "orchestrator.agent_registry.cards",
    "orchestrator/agent_registry/cards.py",
)

AgentCard = _agent_card_mod.AgentCard
Skill = _agent_card_mod.Skill
AuthBlock = _agent_card_mod.AuthBlock
AgentRegistry = _cards_mod.AgentRegistry

# ---------------------------------------------------------------------------
# Shared factory helpers
# ---------------------------------------------------------------------------


def _make_skill(
    tool_id: str = "hr.get_leave_balance",
    label: str = "Get leave balance",
    description: str = "Return remaining leave days.",
    scope: str = "hr_read_a2a",
) -> Skill:
    return Skill(tool_id=tool_id, label=label, description=description, scope=scope)


def _make_card(
    agent_id: str = "hr_agent",
    name: str = "HR Agent",
    base_url: str = "https://hr.smart-employee.local",
    oauth_client_id: str = "hr-oauth-client-abc",
    skills: list[Skill] | None = None,
) -> AgentCard:
    if skills is None:
        skills = [_make_skill()]
    return AgentCard(
        id=agent_id,
        label=name,
        description="Handles HR queries.",
        base_url=base_url,
        oauth_client_id=oauth_client_id,
        api_version="1.0.0",
        skills=skills,
        auth=AuthBlock(
            issuer="https://api.asgardeo.io/t/ddademo/oauth2/token",
            audience=base_url,
        ),
    )


def _make_hr_card() -> AgentCard:
    return _make_card(
        agent_id="hr_agent",
        name="HR Agent",
        base_url="https://hr.smart-employee.local",
        oauth_client_id="hr-oauth-client-abc",
        skills=[
            _make_skill("hr.get_leave_balance", "Get leave balance", "Return leave days.", "hr_read_a2a"),
            _make_skill("hr.approve_leave", "Approve leave", "Approve a leave request.", "hr_approve_a2a"),
        ],
    )


def _make_it_card() -> AgentCard:
    return _make_card(
        agent_id="it_agent",
        name="IT Agent",
        base_url="https://it.smart-employee.local",
        oauth_client_id="it-oauth-client-xyz",
        skills=[
            _make_skill("it.list_available_assets", "List assets", "Return available IT assets.", "it_read_a2a"),
        ],
    )


# ---------------------------------------------------------------------------
# Test 1 — add() + get() round-trip
# ---------------------------------------------------------------------------


def test_add_and_get_round_trip() -> None:
    """A card inserted via add() must be retrievable by its id via get()."""
    registry = AgentRegistry()
    card = _make_hr_card()
    registry.add(card)
    retrieved = registry.get("hr_agent")
    assert retrieved is card


# ---------------------------------------------------------------------------
# Test 2 — add() of duplicate id replaces (does not raise)
# ---------------------------------------------------------------------------


def test_add_duplicate_id_replaces() -> None:
    """A second add() for the same agent id must silently replace the first card."""
    registry = AgentRegistry()
    original = _make_card(agent_id="hr_agent", name="HR Agent v1")
    replacement = _make_card(agent_id="hr_agent", name="HR Agent v2")

    registry.add(original)
    registry.add(replacement)

    # Exactly one entry under "hr_agent"
    assert len(registry.all()) == 1
    assert registry.get("hr_agent") is replacement
    assert registry.get("hr_agent").label == "HR Agent v2"


# ---------------------------------------------------------------------------
# Test 3 — all() preserves insertion order
# ---------------------------------------------------------------------------


def test_all_preserves_insertion_order() -> None:
    """all() must return cards in the order they were first added."""
    registry = AgentRegistry()
    hr = _make_hr_card()
    it = _make_it_card()
    registry.add(hr)
    registry.add(it)

    cards = registry.all()
    assert len(cards) == 2
    assert cards[0].id == "hr_agent"
    assert cards[1].id == "it_agent"


# ---------------------------------------------------------------------------
# Test 4 — find_by_tool() with a known tool_id
# ---------------------------------------------------------------------------


def test_find_by_tool_returns_correct_card() -> None:
    """find_by_tool('hr.get_leave_balance') must return the HR Agent card."""
    registry = AgentRegistry()
    registry.add(_make_hr_card())
    registry.add(_make_it_card())

    card = registry.find_by_tool("hr.get_leave_balance")
    assert card is not None
    assert card.id == "hr_agent"


# ---------------------------------------------------------------------------
# Test 4b — find_by_tool() locates a skill in the second card
# ---------------------------------------------------------------------------


def test_find_by_tool_from_second_card() -> None:
    """find_by_tool() must find a skill in the second registered card."""
    registry = AgentRegistry()
    registry.add(_make_hr_card())
    registry.add(_make_it_card())

    card = registry.find_by_tool("it.list_available_assets")
    assert card is not None
    assert card.id == "it_agent"


# ---------------------------------------------------------------------------
# Test 5 — find_by_tool() with a non-existent tool_id returns None
# ---------------------------------------------------------------------------


def test_find_by_tool_nonexistent_returns_none() -> None:
    """find_by_tool() for an unknown tool_id must return None."""
    registry = AgentRegistry()
    registry.add(_make_hr_card())

    result = registry.find_by_tool("nonexistent.tool")
    assert result is None


# ---------------------------------------------------------------------------
# Test 6 — llm_tool_list() contains entries for all skills from all cards
# ---------------------------------------------------------------------------


def test_llm_tool_list_contains_all_skills() -> None:
    """llm_tool_list() must include one entry per skill across both cards."""
    registry = AgentRegistry()
    hr = _make_hr_card()   # 2 skills
    it = _make_it_card()   # 1 skill
    registry.add(hr)
    registry.add(it)

    tool_list = registry.llm_tool_list()
    tool_ids = {entry["tool_id"] for entry in tool_list}

    assert "hr.get_leave_balance" in tool_ids
    assert "hr.approve_leave" in tool_ids
    assert "it.list_available_assets" in tool_ids
    assert len(tool_list) == 3  # 2 HR + 1 IT


# ---------------------------------------------------------------------------
# Test 7 — llm_tool_list() does NOT include private fields
# ---------------------------------------------------------------------------


def test_llm_tool_list_strips_private_fields() -> None:
    """llm_tool_list() entries must not contain base_url, oauth_client_id, or auth."""
    registry = AgentRegistry()
    registry.add(_make_hr_card())
    registry.add(_make_it_card())

    forbidden = {"base_url", "url", "oauth_client_id", "auth", "jwks_url"}
    for entry in registry.llm_tool_list():
        leaked = forbidden & entry.keys()
        assert not leaked, f"llm_tool_list entry leaked private fields: {leaked}"


# ---------------------------------------------------------------------------
# Test 8 — llm_tool_list() entries include agent_id and agent_label
# ---------------------------------------------------------------------------


def test_llm_tool_list_includes_agent_routing_fields() -> None:
    """Every entry in llm_tool_list() must carry agent_id and agent_label."""
    registry = AgentRegistry()
    registry.add(_make_hr_card())
    registry.add(_make_it_card())

    for entry in registry.llm_tool_list():
        assert "agent_id" in entry, f"Missing agent_id in {entry}"
        assert "agent_label" in entry, f"Missing agent_label in {entry}"
        assert entry["agent_id"] in {"hr_agent", "it_agent"}

    # Spot-check the label mapping
    hr_entries = [e for e in registry.llm_tool_list() if e["agent_id"] == "hr_agent"]
    assert all(e["agent_label"] == "HR Agent" for e in hr_entries)


# ---------------------------------------------------------------------------
# Test 9 — from_files() parses two valid JSON fixture files
# ---------------------------------------------------------------------------


def test_from_files_parses_two_valid_fixtures() -> None:
    """from_files() with two valid card JSON files must produce a registry of size 2."""
    hr_path = _FIXTURE_DIR / "hr_agent_valid.json"
    it_path = _FIXTURE_DIR / "it_agent_valid.json"
    assert hr_path.exists(), f"Fixture missing: {hr_path}"
    assert it_path.exists(), f"Fixture missing: {it_path}"

    registry = AgentRegistry.from_files([hr_path, it_path])

    assert len(registry.all()) == 2
    assert registry.get("hr_agent") is not None
    assert registry.get("it_agent") is not None
    assert registry.get("hr_agent").label == "HR Agent"
    assert registry.get("it_agent").label == "IT Agent"


# ---------------------------------------------------------------------------
# Test 10 — from_files() skips a bad-JSON file with a WARNING log
# ---------------------------------------------------------------------------


def test_from_files_skips_bad_json_with_warning(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file containing invalid JSON must be skipped; a WARNING must be emitted."""
    bad_file = tmp_path / "bad_card.json"
    bad_file.write_text("{ this is not valid JSON }", encoding="utf-8")

    hr_path = _FIXTURE_DIR / "hr_agent_valid.json"

    with caplog.at_level(logging.WARNING, logger="orchestrator.agent_registry.cards"):
        registry = AgentRegistry.from_files([hr_path, bad_file])

    # Valid card was loaded; bad card was skipped
    assert registry.get("hr_agent") is not None
    assert len(registry.all()) == 1

    # A WARNING must have been emitted for the bad file
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(bad_file) in msg for msg in warning_messages), (
        f"Expected a WARNING mentioning {bad_file}; got: {warning_messages}"
    )


# ---------------------------------------------------------------------------
# Test 11 — from_files() returns empty registry when all files are bad
# ---------------------------------------------------------------------------


def test_from_files_returns_empty_registry_when_all_bad(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If every file fails to parse, from_files() must return an empty registry."""
    bad1 = tmp_path / "bad1.json"
    bad2 = tmp_path / "bad2.json"
    bad1.write_text("NOT JSON", encoding="utf-8")
    bad2.write_text("{}", encoding="utf-8")  # Valid JSON but fails AgentCard validation

    with caplog.at_level(logging.WARNING, logger="orchestrator.agent_registry.cards"):
        registry = AgentRegistry.from_files([bad1, bad2])

    assert registry.all() == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # one per bad file


# ---------------------------------------------------------------------------
# Test 12 — get() returns None for an unknown agent_id
# ---------------------------------------------------------------------------


def test_get_returns_none_for_unknown_id() -> None:
    """get() for an id that was never added must return None."""
    registry = AgentRegistry()
    registry.add(_make_hr_card())

    assert registry.get("unknown-agent") is None
    assert registry.get("") is None


# ---------------------------------------------------------------------------
# Test 13 — empty registry returns empty collections
# ---------------------------------------------------------------------------


def test_empty_registry_returns_empty_collections() -> None:
    """A freshly constructed registry must return [] from all() and llm_tool_list()."""
    registry = AgentRegistry()

    assert registry.all() == []
    assert registry.llm_tool_list() == []
    assert registry.get("anything") is None
    assert registry.find_by_tool("hr.get_leave_balance") is None
