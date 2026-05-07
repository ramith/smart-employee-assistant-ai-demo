"""Tests for orchestrator/chat/keyword_fallback.py — Wave 5, Sprint 1.

Coverage targets
----------------
 1.  ``route("what's my leave balance")``
     → ``[ToolCall("hr_agent", "hr.read_balance", {})]``
 2.  ``route("show me available laptops")``
     → ``[ToolCall("it_agent", "it.list_available_assets", {})]``
 3.  ``route("leave + laptops")``
     → ``[hr_agent first, it_agent second]`` (serial fan-out, DEFAULT_RULES order)
 4.  ``route("hello")`` → ``[]``
 5.  ``route("LEAVE")`` — case-insensitive match on "leave"
 6.  ``route("How much vacation do I have?")`` — matches "vacation"
 7.  ``route("PTO")`` — matches "pto"
 8.  ``route("can I have time off in june")`` — matches "time off"
 9.  Single rule fires only once even when multiple keywords from the same
     rule appear in the message.
10.  ``KeywordRouter(rules=(custom,))`` uses the provided custom rules and
     ignores DEFAULT_RULES.
11.  ``explain()`` returns a descriptive string mentioning the matched rules.
12.  ``explain()`` returns ``"no match"`` for an unrecognised message.
13.  :class:`ToolCall` is frozen — assigning to a field raises
     :class:`dataclasses.FrozenInstanceError`.
14.  :class:`KeywordRule` is frozen — assigning to a field raises
     :class:`dataclasses.FrozenInstanceError`.
15.  ``route("hardware")`` — matches via the "hardware" keyword.
16.  ``route("I need equipment for my computer")`` — two IT keywords; rule
     still fires exactly once, yielding a single IT ToolCall.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import pathlib
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Module isolation — load keyword_fallback without touching other __init__.py
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str) -> None:
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    stub.__path__ = [str(_ROOT / dotted_name.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


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


for _pkg in ("orchestrator", "orchestrator.chat"):
    _ensure_pkg(_pkg)

_mod = _load_module(
    "orchestrator.chat.keyword_fallback",
    "orchestrator/chat/keyword_fallback.py",
)

KeywordRouter = _mod.KeywordRouter
KeywordRule = _mod.KeywordRule
ToolCall = _mod.ToolCall
DEFAULT_RULES = _mod.DEFAULT_RULES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hr_call() -> ToolCall:
    return ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})


def _it_call() -> ToolCall:
    return ToolCall(agent_id="it_agent", tool_id="it.list_available_assets", args={})


# ---------------------------------------------------------------------------
# Test 1 — leave balance query → single HR ToolCall
# ---------------------------------------------------------------------------


def test_route_leave_balance() -> None:
    """'what's my leave balance' must route to hr_agent / hr.read_balance."""
    router = KeywordRouter()
    result = router.route("what's my leave balance")
    assert result == [_hr_call()]


# ---------------------------------------------------------------------------
# Test 2 — laptop query → single IT ToolCall
# ---------------------------------------------------------------------------


def test_route_available_laptops() -> None:
    """'show me available laptops' must route to it_agent / it.list_available_assets."""
    router = KeywordRouter()
    result = router.route("show me available laptops")
    assert result == [_it_call()]


# ---------------------------------------------------------------------------
# Test 3 — combined message → HR first, IT second (rule order preserved)
# ---------------------------------------------------------------------------


def test_route_combined_leave_and_laptop_preserves_order() -> None:
    """'leave + laptops' must produce HR call first, IT call second."""
    router = KeywordRouter()
    result = router.route("leave + laptops")
    assert len(result) == 2
    assert result[0] == _hr_call()
    assert result[1] == _it_call()


# ---------------------------------------------------------------------------
# Test 4 — no keywords → empty list
# ---------------------------------------------------------------------------


def test_route_no_match_returns_empty_list() -> None:
    """'hello' must produce an empty ToolCall list."""
    router = KeywordRouter()
    result = router.route("hello")
    assert result == []


# ---------------------------------------------------------------------------
# Test 5 — LEAVE (upper-case) matches "leave" case-insensitively
# ---------------------------------------------------------------------------


def test_route_case_insensitive_leave_upper() -> None:
    """'LEAVE' must match the 'leave' keyword despite being all-caps."""
    router = KeywordRouter()
    result = router.route("LEAVE")
    assert result == [_hr_call()]


# ---------------------------------------------------------------------------
# Test 6 — "vacation" keyword fires HR rule
# ---------------------------------------------------------------------------


def test_route_vacation_keyword() -> None:
    """'How much vacation do I have?' must trigger the HR rule via 'vacation'."""
    router = KeywordRouter()
    result = router.route("How much vacation do I have?")
    assert result == [_hr_call()]


# ---------------------------------------------------------------------------
# Test 7 — "PTO" keyword fires HR rule
# ---------------------------------------------------------------------------


def test_route_pto_keyword() -> None:
    """'PTO' must match the 'pto' keyword case-insensitively."""
    router = KeywordRouter()
    result = router.route("PTO")
    assert result == [_hr_call()]


# ---------------------------------------------------------------------------
# Test 8 — "time off" (multi-word keyword) fires HR rule
# ---------------------------------------------------------------------------


def test_route_time_off_multi_word_keyword() -> None:
    """'can I have time off in june' must match the multi-word keyword 'time off'."""
    router = KeywordRouter()
    result = router.route("can I have time off in june")
    assert result == [_hr_call()]


# ---------------------------------------------------------------------------
# Test 9 — duplicate keyword hits on the same rule still fire only once
# ---------------------------------------------------------------------------


def test_route_multiple_keywords_same_rule_fires_once() -> None:
    """A message with both 'leave' and 'vacation' must still produce one HR ToolCall."""
    router = KeywordRouter()
    result = router.route("I have vacation but also leave remaining")
    assert len(result) == 1
    assert result[0] == _hr_call()


# ---------------------------------------------------------------------------
# Test 10 — custom rules override DEFAULT_RULES entirely
# ---------------------------------------------------------------------------


def test_route_custom_rules_ignores_defaults() -> None:
    """A router built with custom rules must match those rules and not DEFAULT_RULES."""
    custom_rule = KeywordRule(
        keywords=("payslip", "salary"),
        agent_id="payroll-agent",
        tool_id="payroll.get_slip",
        args={"format": "pdf"},
    )
    router = KeywordRouter(rules=(custom_rule,))

    # Custom keyword fires
    result = router.route("show me my payslip")
    assert len(result) == 1
    assert result[0].agent_id == "payroll-agent"
    assert result[0].tool_id == "payroll.get_slip"
    assert result[0].args == {"format": "pdf"}

    # Default keyword must NOT fire (DEFAULT_RULES not loaded)
    assert router.route("leave balance") == []


# ---------------------------------------------------------------------------
# Test 11 — explain() returns descriptive text for a matching message
# ---------------------------------------------------------------------------


def test_explain_returns_descriptive_text_on_match() -> None:
    """explain() must mention matched tool_id(s) and agent_id(s) for a hitting message."""
    router = KeywordRouter()
    explanation = router.explain("I want to check my vacation days and find a laptop")

    assert "hr.read_balance" in explanation
    assert "hr_agent" in explanation
    assert "it.list_available_assets" in explanation
    assert "it_agent" in explanation
    assert explanation.startswith("matched:")


# ---------------------------------------------------------------------------
# Test 12 — explain() returns "no match" for unrecognised input
# ---------------------------------------------------------------------------


def test_explain_returns_no_match_for_unknown_message() -> None:
    """explain() must return exactly 'no match' when no rule fires."""
    router = KeywordRouter()
    result = router.explain("what is the weather today?")
    assert result == "no match"


# ---------------------------------------------------------------------------
# Test 13 — ToolCall is frozen (mutation raises FrozenInstanceError)
# ---------------------------------------------------------------------------


def test_toolcall_is_frozen() -> None:
    """Assigning to any ToolCall field must raise dataclasses.FrozenInstanceError."""
    call = ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        call.agent_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 14 — KeywordRule is frozen (mutation raises FrozenInstanceError)
# ---------------------------------------------------------------------------


def test_keywordrule_is_frozen() -> None:
    """Assigning to any KeywordRule field must raise dataclasses.FrozenInstanceError."""
    rule = KeywordRule(
        keywords=("leave",), agent_id="hr_agent", tool_id="hr.read_balance"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.agent_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 15 — "hardware" keyword fires IT rule
# ---------------------------------------------------------------------------


def test_route_hardware_keyword() -> None:
    """'hardware' must trigger the IT rule via the 'hardware' keyword."""
    router = KeywordRouter()
    result = router.route("I need new hardware for my desk")
    assert result == [_it_call()]


# ---------------------------------------------------------------------------
# Test 16 — multiple IT keywords in same message: rule fires only once
# ---------------------------------------------------------------------------


def test_route_multiple_it_keywords_fires_once() -> None:
    """'I need equipment for my computer' contains two IT keywords but yields one ToolCall."""
    router = KeywordRouter()
    result = router.route("I need equipment for my computer")
    assert len(result) == 1
    assert result[0] == _it_call()
