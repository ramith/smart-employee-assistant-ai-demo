"""Tests for common/a2a/agent_card.py — Wave 2, Sprint 1.

Coverage targets
----------------
1.  ``AgentCard`` with a valid ``base_url`` (origin-only) constructs cleanly.
2.  ``AgentCard`` with a trailing slash in ``base_url`` → ``ValidationError``.
3.  ``AgentCard`` with a path segment in ``base_url`` → ``ValidationError``.
4.  ``Skill`` with a non-namespaced ``tool_id`` (no dot) → ``ValidationError``.
5.  ``Skill`` with a leading-dot ``tool_id`` → ``ValidationError``.
6.  ``SCHEMA_VERSION`` constant equals the value declared in
    ``docs/agent-card-schema.md`` (``"v3-custom"``).
7.  ``llm_projection`` output does NOT contain ``base_url``, ``oauth_client_id``,
    ``auth``, ``jwks_url``, or ``url``.
8.  ``llm_projection`` output preserves ``tool_id``, ``label``, ``description``,
    and ``scope`` for each skill.
9.  Round-trip: ``AgentCard.model_validate_json(card.model_dump_json())``
    equals the original card.
10. An ``AgentCard`` with an empty ``skills`` list is valid.
11. ``oauth_client_id`` is required — omitting it raises ``ValidationError``.
12. Loading a valid card from a JSON fixture file produces a valid ``AgentCard``.
13. An ``AgentCard`` with an unrecognised ``api_version`` (e.g. ``"9.0.0"``)
    does NOT raise; it only logs a warning.
14. ``llm_projection`` returns exactly the expected top-level keys.
15. ``llm_projection`` returns exactly the expected per-skill keys.
"""

from __future__ import annotations

import json
import logging
import pathlib

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Module under test — loaded directly to bypass stale common/a2a/__init__.py
# (same technique as conftest.py uses for auth/models.py).
# ---------------------------------------------------------------------------

import importlib.util
import sys
import types

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
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


# Ensure intermediate package stubs exist.
for _pkg in ("common", "common.a2a"):
    if _pkg not in sys.modules:
        _stub = types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        _stub.__path__ = [str(_ROOT / _pkg.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub

_mod = _load_module("common.a2a.agent_card", "common/a2a/agent_card.py")

AgentCard = _mod.AgentCard
Skill = _mod.Skill
Capabilities = _mod.Capabilities
AuthBlock = _mod.AuthBlock
SCHEMA_VERSION = _mod.SCHEMA_VERSION
llm_projection = _mod.llm_projection

# ---------------------------------------------------------------------------
# Shared fixtures / factory helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "agent_cards"


def _make_skill(
    tool_id: str = "hr.get_leave_balance",
    label: str = "Get leave balance",
    description: str = "Return remaining leave days.",
    scope: str = "hr_read_a2a",
) -> Skill:
    return Skill(
        tool_id=tool_id,
        label=label,
        description=description,
        scope=scope,
    )


def _make_card(
    *,
    base_url: str = "https://hr.smart-employee.local",
    oauth_client_id: str = "hr-oauth-client-abc",
    api_version: str = "1.0.0",
    skills: list[Skill] | None = None,
) -> AgentCard:
    if skills is None:
        skills = [_make_skill()]
    return AgentCard(
        id="hr_agent",
        label="HR Agent",
        description="Handles HR queries.",
        base_url=base_url,
        oauth_client_id=oauth_client_id,
        api_version=api_version,
        skills=skills,
        auth=AuthBlock(
            issuer="https://api.asgardeo.io/t/ddademo/oauth2/token",
            audience="https://hr.smart-employee.local",
        ),
    )


# ---------------------------------------------------------------------------
# Test 1 — valid base_url constructs cleanly
# ---------------------------------------------------------------------------


def test_agent_card_valid_base_url() -> None:
    """AgentCard with a clean origin URL (no path, no trailing slash) is valid."""
    card = _make_card(base_url="https://hr.smart-employee.local")
    assert card.base_url == "https://hr.smart-employee.local"


# ---------------------------------------------------------------------------
# Test 2 — trailing slash in base_url → ValidationError
# ---------------------------------------------------------------------------


def test_agent_card_trailing_slash_raises() -> None:
    """Trailing slash in base_url must raise ValidationError."""
    with pytest.raises(ValidationError, match="base_url"):
        _make_card(base_url="https://hr.smart-employee.local/")


# ---------------------------------------------------------------------------
# Test 3 — path segment in base_url → ValidationError
# ---------------------------------------------------------------------------


def test_agent_card_path_in_base_url_raises() -> None:
    """A path component in base_url must raise ValidationError."""
    with pytest.raises(ValidationError, match="base_url"):
        _make_card(base_url="https://hr.smart-employee.local/a2a")


# ---------------------------------------------------------------------------
# Test 4 — Skill with non-namespaced tool_id → ValidationError
# ---------------------------------------------------------------------------


def test_skill_non_namespaced_tool_id_raises() -> None:
    """A tool_id without a dot must raise ValidationError."""
    with pytest.raises(ValidationError, match="namespaced"):
        Skill(tool_id="approve_leave", label="Approve leave", description="...")


# ---------------------------------------------------------------------------
# Test 5 — Skill with leading-dot tool_id → ValidationError
# ---------------------------------------------------------------------------


def test_skill_leading_dot_tool_id_raises() -> None:
    """A tool_id like '.approve_leave' (empty namespace) must raise ValidationError."""
    with pytest.raises(ValidationError):
        Skill(tool_id=".approve_leave", label="Approve leave", description="...")


# ---------------------------------------------------------------------------
# Test 6 — SCHEMA_VERSION matches agent-card-schema.md
# ---------------------------------------------------------------------------


def test_schema_version_matches_docs() -> None:
    """SCHEMA_VERSION constant must equal 'v3-custom' as declared in agent-card-schema.md."""
    # The canonical value is declared on line 1 of docs/agent-card-schema.md:
    # schema_version: "v3-custom"
    assert SCHEMA_VERSION == "v3-custom"


# ---------------------------------------------------------------------------
# Test 7 — llm_projection does NOT leak sensitive fields
# ---------------------------------------------------------------------------


def test_llm_projection_strips_sensitive_fields() -> None:
    """llm_projection output must not contain base_url, oauth_client_id, auth, or url."""
    card = _make_card()
    projection = llm_projection(card)
    forbidden_keys = {"base_url", "url", "oauth_client_id", "auth", "jwks_url"}
    leaked = forbidden_keys & projection.keys()
    assert not leaked, f"llm_projection leaked sensitive keys: {leaked}"

    # Also assert no nested leakage in skills
    for skill_dict in projection.get("skills", []):
        assert "oauth_client_id" not in skill_dict
        assert "base_url" not in skill_dict
        assert "url" not in skill_dict


# ---------------------------------------------------------------------------
# Test 8 — llm_projection preserves skill fields
# ---------------------------------------------------------------------------


def test_llm_projection_preserves_skill_fields() -> None:
    """llm_projection must include tool_id, label, description, and scope per skill."""
    skill = _make_skill(
        tool_id="hr.get_leave_balance",
        label="Get leave balance",
        description="Return remaining leave days.",
        scope="hr_read_a2a",
    )
    card = _make_card(skills=[skill])
    projection = llm_projection(card)

    assert len(projection["skills"]) == 1
    s = projection["skills"][0]
    assert s["tool_id"] == "hr.get_leave_balance"
    assert s["label"] == "Get leave balance"
    assert s["description"] == "Return remaining leave days."
    assert s["scope"] == "hr_read_a2a"


# ---------------------------------------------------------------------------
# Test 9 — round-trip serialisation
# ---------------------------------------------------------------------------


def test_agent_card_round_trip() -> None:
    """model_dump_json → model_validate_json must produce an equal AgentCard."""
    original = _make_card()
    json_str = original.model_dump_json(by_alias=True)
    restored = AgentCard.model_validate_json(json_str)
    assert restored == original


# ---------------------------------------------------------------------------
# Test 10 — empty skills list is valid
# ---------------------------------------------------------------------------


def test_agent_card_empty_skills_is_valid() -> None:
    """An AgentCard with no skills is allowed (agent may be in maintenance mode)."""
    card = _make_card(skills=[])
    assert card.skills == []


# ---------------------------------------------------------------------------
# Test 11 — oauth_client_id is required (not Optional)
# ---------------------------------------------------------------------------


def test_oauth_client_id_is_required() -> None:
    """Omitting oauth_client_id must raise ValidationError."""
    with pytest.raises(ValidationError):
        AgentCard(
            id="hr_agent",
            label="HR Agent",
            description="Handles HR queries.",
            base_url="https://hr.smart-employee.local",
            # oauth_client_id intentionally omitted
            api_version="1.0.0",
            skills=[],
            auth=AuthBlock(
                issuer="https://example.com",
                audience="https://hr.smart-employee.local",
            ),
        )


# ---------------------------------------------------------------------------
# Test 12 — loading a valid card from a JSON fixture file
# ---------------------------------------------------------------------------


def test_load_from_json_fixture() -> None:
    """AgentCard.model_validate_json loads the hr_agent_valid.json fixture cleanly."""
    fixture_path = _FIXTURE_DIR / "hr_agent_valid.json"
    assert fixture_path.exists(), f"Fixture not found: {fixture_path}"
    raw = fixture_path.read_text(encoding="utf-8")
    card = AgentCard.model_validate_json(raw)
    assert card.id == "hr_agent"
    assert card.label == "HR Agent"
    # Skill count drifted from 2 → 3 when hr.approve_leave joined hr.read_balance
    # and hr.write (Sprint 2 fixture refresh). Assert non-empty + a known
    # tool_id rather than pinning a count that will keep drifting.
    assert len(card.skills) >= 2
    assert any(s.tool_id == "hr.approve_leave" for s in card.skills)
    # Hyphenated form (hr-agent) — the fixture renamed from hr_agent to
    # hr-agent at some point; the assertion lagged.
    assert card.oauth_client_id == "hr-agent-oauth-client-id-abc123"


# ---------------------------------------------------------------------------
# Test 13 — unrecognised api_version warns but does NOT raise
# ---------------------------------------------------------------------------


def test_unknown_api_version_warns_not_raises(caplog: pytest.LogCaptureFixture) -> None:
    """api_version '9.0.0' (unknown major) emits a WARNING but constructs successfully."""
    with caplog.at_level(logging.WARNING, logger="common.a2a.agent_card"):
        card = _make_card(api_version="9.0.0")
    assert card.api_version == "9.0.0"
    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("9.0.0" in t for t in warning_texts), (
        "Expected a WARNING mentioning the unknown api_version '9.0.0'"
    )


# ---------------------------------------------------------------------------
# Test 14 — llm_projection has exactly the expected top-level keys
# ---------------------------------------------------------------------------


def test_llm_projection_top_level_keys() -> None:
    """llm_projection output must have exactly {id, label, skills}."""
    card = _make_card()
    projection = llm_projection(card)
    assert set(projection.keys()) == {"id", "label", "skills"}


# ---------------------------------------------------------------------------
# Test 15 — llm_projection per-skill keys
# ---------------------------------------------------------------------------


def test_llm_projection_skill_keys() -> None:
    """Each skill dict in llm_projection must have exactly {tool_id, label, description, scope, args}.

    ``args`` (S5): the names of the arguments the LLM router may extract for the tool.
    """
    card = _make_card(skills=[_make_skill(), _make_skill(tool_id="hr.approve_leave", label="Approve")])
    projection = llm_projection(card)
    for skill_dict in projection["skills"]:
        assert set(skill_dict.keys()) == {"tool_id", "label", "description", "scope", "args"}, (
            f"Unexpected skill keys: {set(skill_dict.keys())}"
        )
        assert isinstance(skill_dict["args"], list)
