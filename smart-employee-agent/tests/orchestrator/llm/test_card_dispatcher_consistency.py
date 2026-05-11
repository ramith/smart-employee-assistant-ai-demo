"""S5: the agent cards' ``skills[]`` must stay in lock-step with each agent's
dispatcher ``_TOOL_REGISTRY`` — every routable tool is carded, every carded
tool is routable — and each carded skill's ``args`` must cover the keys that
tool's ``kwargs_builder`` reads (else the LLM router's ``_validate`` would
silently strip a legitimate argument → ERR-AGENT-002).

The one documented exception: ``hr.lookup_employee`` is an agent-internal
helper (used to resolve a CIBA ``login_hint`` for ``hr.cubicle_assign``); it is
in the dispatcher registry but deliberately *omitted* from the card so the LLM
can't route to it and its ``sub``-bearing result can't reach a prompt
(sprint-5.md §2.7).
"""

from __future__ import annotations

from pathlib import Path

from hr_agent.ciba.orchestrator import _REQUIRED_ARGS as HR_REQUIRED
from hr_agent.ciba.orchestrator import _TOOL_REGISTRY as HR_REGISTRY
from it_agent.ciba.orchestrator import _REQUIRED_ARGS as IT_REQUIRED
from it_agent.ciba.orchestrator import _TOOL_REGISTRY as IT_REGISTRY

from orchestrator.agent_registry.cards import AgentRegistry

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "agent_cards"

# Tools in a dispatcher registry that are deliberately NOT carded.
_OFF_CARD = {"hr_agent": {"hr.lookup_employee"}, "it_agent": set()}

# Expected arg-name set per carded tool (must match the kwargs_builder keys 1:1
# AND be a superset of the tool's _REQUIRED_ARGS).
_EXPECTED_ARGS = {
    "hr.read_policy": set(),
    "hr.read_balance": set(),
    "hr.read_history": set(),
    "hr.apply_leave": {"leave_type", "start_date", "end_date", "reason"},
    "hr.approve_leave": {"leave_id"},
    "hr.reject_leave": {"leave_id", "reason"},
    "hr.cubicle_summary": set(),
    "hr.cubicle_list_floor": {"floor"},
    "hr.cubicle_assign": {"cubicle_id", "employee_username", "employee_email"},
    "hr.cubicle_lookup_self": set(),
    "it.list_available_assets": {"asset_type"},
    "it.get_my_assets": set(),
    "it.issue_asset": {"asset_id", "employee_id"},
}


def _registry() -> AgentRegistry:
    return AgentRegistry.from_files([_FIXTURES / "hr_agent_valid.json", _FIXTURES / "it_agent_valid.json"])


def _card_skills(reg: AgentRegistry, agent_id: str) -> dict[str, set[str]]:
    """Return ``{tool_id: set(args)}`` for the given agent, from the loaded cards."""
    return {
        e["tool_id"]: set(e.get("args", []))
        for e in reg.llm_tool_list()
        if e["agent_id"] == agent_id
    }


def test_hr_card_matches_dispatcher_registry() -> None:
    reg = _registry()
    carded = _card_skills(reg, "hr_agent")
    expected_carded = set(HR_REGISTRY) - _OFF_CARD["hr_agent"]
    assert set(carded) == expected_carded, (
        f"hr_agent card/registry drift: card-only={set(carded) - expected_carded}, "
        f"registry-only={expected_carded - set(carded)}"
    )
    for tool_id, args in carded.items():
        assert args == _EXPECTED_ARGS[tool_id], f"{tool_id}: card args {args} != expected {_EXPECTED_ARGS[tool_id]}"
    # _REQUIRED_ARGS must be a subset of the carded args (else a "required" arg
    # the LLM extracts gets stripped by _validate).
    for tool_id, req in HR_REQUIRED.items():
        if tool_id in _OFF_CARD["hr_agent"]:
            continue
        assert set(req) <= carded[tool_id], f"{tool_id}: required {req} not all carded ({carded[tool_id]})"


def test_it_card_matches_dispatcher_registry() -> None:
    reg = _registry()
    carded = _card_skills(reg, "it_agent")
    expected_carded = set(IT_REGISTRY) - _OFF_CARD["it_agent"]
    assert set(carded) == expected_carded
    for tool_id, args in carded.items():
        assert args == _EXPECTED_ARGS[tool_id], f"{tool_id}: card args {args} != expected {_EXPECTED_ARGS[tool_id]}"
    for tool_id, req in IT_REQUIRED.items():
        assert set(req) <= carded[tool_id]


def test_lookup_employee_is_in_dispatcher_but_not_carded() -> None:
    reg = _registry()
    assert "hr.lookup_employee" in HR_REGISTRY
    assert reg.find_by_tool("hr.lookup_employee") is None
    hr_card = reg.get("hr_agent")
    assert hr_card is not None
    assert "hr.lookup_employee" not in {s.tool_id for s in hr_card.skills}
