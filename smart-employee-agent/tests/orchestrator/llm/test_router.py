"""Tests for orchestrator/llm/prompts.parse_router_output, strip_sensitive,
render_outcomes, router_system, and orchestrator/llm/router.{build_catalogue,
resolve_tool_calls}. No network, no langchain — uses FakeLLMClient."""

from __future__ import annotations

import pytest

from orchestrator.chat.keyword_fallback import ToolCall
from orchestrator.llm import prompts
from orchestrator.llm.client import LLMError, RoutedToolCall, ToolOutcome
from orchestrator.llm.router import build_catalogue, resolve_tool_calls

from tests.orchestrator.llm.conftest import FakeLLMClient, make_deps


# ── parse_router_output ──────────────────────────────────────────────────────


def test_parse_valid_array() -> None:
    out = prompts.parse_router_output(
        '[{"agent_id":"hr_agent","tool_id":"hr.read_balance","args":{}},'
        '{"agent_id":"it_agent","tool_id":"it.get_my_assets","args":{}}]'
    )
    assert out == [
        RoutedToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={}),
        RoutedToolCall(agent_id="it_agent", tool_id="it.get_my_assets", args={}),
    ]


def test_parse_empty_array_is_valid() -> None:
    assert prompts.parse_router_output("[]") == []


def test_parse_markdown_fenced_array() -> None:
    out = prompts.parse_router_output('```json\n[{"agent_id":"hr_agent","tool_id":"hr.read_policy"}]\n```')
    assert out == [RoutedToolCall(agent_id="hr_agent", tool_id="hr.read_policy", args={})]


def test_parse_list_of_content_blocks() -> None:
    # langchain sometimes returns content as a list of blocks.
    out = prompts.parse_router_output([{"type": "text", "text": '[{"agent_id":"hr_agent","tool_id":"hr.read_balance"}]'}])
    assert out == [RoutedToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})]


@pytest.mark.parametrize("bad", ["not json at all", "{}", '{"agent_id":"x"}', "", None, '"a string"'])
def test_parse_unparseable_raises_llmerror(bad) -> None:
    with pytest.raises(LLMError):
        prompts.parse_router_output(bad)


def test_parse_mixed_valid_and_malformed_keeps_valid() -> None:
    out = prompts.parse_router_output(
        '[{"agent_id":"hr_agent","tool_id":"hr.read_balance"}, "garbage", {"tool_id":"missing_agent"}, 42]'
    )
    assert out == [RoutedToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})]


def test_parse_all_malformed_nonempty_raises() -> None:
    with pytest.raises(LLMError):
        prompts.parse_router_output('["garbage", 42, {"no":"keys"}]')


# ── strip_sensitive ──────────────────────────────────────────────────────────


def test_strip_sensitive_drops_sub_and_uuid_values_and_tokens() -> None:
    raw = {
        "username": "employee_user",
        "sub": "2048ad8c-16a6-4ec1-bb63-b38300118f28",
        "assigned_to_sub": "x",
        "reviewed_by_sub": "y",
        "issued_by": "z",
        "employee_id": "15fab9e7-18ec-4f6b-be0f-7aa1ddcebfb7",  # UUID -> dropped
        "cubicle_id": "C-005",
        "access_token": "eyJ...",
        "nested": {"floor": 2, "secret_thing": "shh", "id": "AST-12345"},
        "list": [{"sub": "drop"}, {"keep": "kept"}],
    }
    out = prompts.strip_sensitive(raw)
    assert out == {
        "username": "employee_user",
        "cubicle_id": "C-005",
        "nested": {"floor": 2, "id": "AST-12345"},
        "list": [{}, {"keep": "kept"}],
    }


def test_strip_sensitive_passes_scalars_and_non_uuid_strings() -> None:
    assert prompts.strip_sensitive("hello") == "hello"
    assert prompts.strip_sensitive(42) == 42
    assert prompts.strip_sensitive(["a", "b"]) == ["a", "b"]


# ── render_outcomes ──────────────────────────────────────────────────────────


def test_render_outcomes_success_strips_sub() -> None:
    rendered = prompts.render_outcomes(
        [ToolOutcome("hr_agent", "hr.read_balance", True, data={"balance": {"annual": 20}, "sub": "abc"})]
    )
    assert "hr.read_balance (success)" in rendered
    assert '"annual": 20' in rendered
    assert "sub" not in rendered  # stripped


def test_render_outcomes_failure_shows_error_id_and_reason() -> None:
    rendered = prompts.render_outcomes(
        [ToolOutcome("hr_agent", "hr.apply_leave", False, error_id="ERR-AGENT-002", reason="Missing required arguments for hr.apply_leave: ['end_date','start_date']")]
    )
    assert "hr.apply_leave (failed)" in rendered
    assert "ERR-AGENT-002" in rendered
    assert "start_date" in rendered


def test_render_outcomes_empty() -> None:
    assert prompts.render_outcomes([]) == "(no tools ran)"


# ── router_system ────────────────────────────────────────────────────────────


def test_router_system_lists_every_catalogue_tool(agent_registry) -> None:
    catalogue = build_catalogue(agent_registry)
    text = prompts.router_system(catalogue, today="2026-05-11")
    assert "Today is 2026-05-11" in text
    for entry in catalogue:
        assert f'tool_id="{entry.tool_id}"' in text
    # The agent-internal helper must NOT be in the catalogue.
    assert 'tool_id="hr.lookup_employee"' not in text


# ── build_catalogue ──────────────────────────────────────────────────────────


def test_build_catalogue_from_registry(agent_registry) -> None:
    catalogue = {e.tool_id: e for e in build_catalogue(agent_registry)}
    assert "hr.apply_leave" in catalogue
    assert catalogue["hr.apply_leave"].agent_id == "hr_agent"
    assert set(catalogue["hr.apply_leave"].args) == {"leave_type", "start_date", "end_date", "reason"}
    assert "hr.lookup_employee" not in catalogue  # off the card


# ── resolve_tool_calls ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_uses_llm_tool_calls_when_valid(agent_registry) -> None:
    llm = FakeLLMClient(route_result=[
        RoutedToolCall("hr_agent", "hr.read_balance", {}),
        RoutedToolCall("it_agent", "it.get_my_assets", {}),
    ])
    deps = make_deps(llm_client=llm, mode="llm", agent_registry=agent_registry)
    result = await resolve_tool_calls("how much leave do I have and what's my laptop", deps)
    assert result == [
        ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={}),
        ToolCall(agent_id="it_agent", tool_id="it.get_my_assets", args={}),
    ]
    assert llm.route_calls  # the LLM was consulted


@pytest.mark.asyncio
async def test_resolve_filters_hallucinated_args_and_keeps_valid_ones(agent_registry) -> None:
    llm = FakeLLMClient(route_result=[
        RoutedToolCall("hr_agent", "hr.apply_leave", {
            "leave_type": "Annual Leave", "start_date": "2026-06-10", "end_date": "2026-06-14",
            "reason": "trip", "role": "admin", "evil": {"nested": 1},  # hallucinated / non-scalar
        }),
    ])
    deps = make_deps(llm_client=llm, mode="llm", agent_registry=agent_registry)
    result = await resolve_tool_calls("apply for annual leave June 10-14", deps)
    assert result == [ToolCall(agent_id="hr_agent", tool_id="hr.apply_leave", args={
        "leave_type": "Annual Leave", "start_date": "2026-06-10", "end_date": "2026-06-14", "reason": "trip",
    })]


@pytest.mark.asyncio
async def test_resolve_drops_unknown_agent_and_tool(agent_registry) -> None:
    llm = FakeLLMClient(route_result=[
        RoutedToolCall("system", "admin.grant_role", {"role": "admin"}),       # unknown agent
        RoutedToolCall("hr_agent", "hr.delete_everything", {}),                  # unknown tool
        RoutedToolCall("hr_agent", "hr.read_balance", {}),                       # valid → survives
    ])
    deps = make_deps(llm_client=llm, mode="llm", agent_registry=agent_registry)
    result = await resolve_tool_calls("do everything", deps)
    assert result == [ToolCall(agent_id="hr_agent", tool_id="hr.read_balance", args={})]


@pytest.mark.asyncio
async def test_resolve_drops_internal_only_tool(agent_registry) -> None:
    llm = FakeLLMClient(route_result=[RoutedToolCall("hr_agent", "hr.lookup_employee", {"name": "jane.doe"})])
    deps = make_deps(llm_client=llm, mode="llm", agent_registry=agent_registry)
    # All routed tools dropped → falls back to keyword router (which has no rule
    # for "look up jane.doe" → empty).
    result = await resolve_tool_calls("look up jane.doe", deps)
    assert result == []


@pytest.mark.asyncio
async def test_resolve_falls_back_to_keyword_on_llm_error(agent_registry) -> None:
    llm = FakeLLMClient(route_result=LLMError("gemini exploded"))
    deps = make_deps(llm_client=llm, mode="llm", agent_registry=agent_registry)
    result = await resolve_tool_calls("what's my leave balance", deps)
    # keyword router routes "leave balance" → hr.read_balance
    assert [tc.tool_id for tc in result] == ["hr.read_balance"]


@pytest.mark.asyncio
async def test_resolve_falls_back_to_keyword_when_llm_returns_empty(agent_registry) -> None:
    llm = FakeLLMClient(route_result=[])
    deps = make_deps(llm_client=llm, mode="llm", agent_registry=agent_registry)
    result = await resolve_tool_calls("what's my leave balance", deps)
    assert [tc.tool_id for tc in result] == ["hr.read_balance"]


@pytest.mark.asyncio
async def test_resolve_uses_keyword_only_when_mode_is_keyword(agent_registry) -> None:
    # Even with an llm_client present, mode=keyword → never calls it.
    llm = FakeLLMClient(route_result=[RoutedToolCall("hr_agent", "hr.apply_leave", {})])
    deps = make_deps(llm_client=llm, mode="keyword", agent_registry=agent_registry)
    result = await resolve_tool_calls("what's my leave balance", deps)
    assert [tc.tool_id for tc in result] == ["hr.read_balance"]
    assert not llm.route_calls


@pytest.mark.asyncio
async def test_resolve_uses_keyword_only_when_no_llm_client(agent_registry) -> None:
    deps = make_deps(llm_client=None, mode="llm", agent_registry=agent_registry)
    result = await resolve_tool_calls("what's my leave balance", deps)
    assert [tc.tool_id for tc in result] == ["hr.read_balance"]


# ── S5.6: chat-history is threaded through to the LLM router ──────────────────


@pytest.mark.asyncio
async def test_resolve_passes_history_to_llm(agent_registry) -> None:
    llm = FakeLLMClient(route_result=[RoutedToolCall("hr_agent", "hr.apply_leave", {
        "leave_type": "Sick Leave", "start_date": "2026-05-18", "end_date": "2026-05-20",
    })])
    deps = make_deps(llm_client=llm, mode="llm", agent_registry=agent_registry)
    history = [
        ("user", "I want to apply for leave next monday to wednesday"),
        ("assistant", "Sure — what type of leave? Annual, Sick, or Personal?"),
    ]
    result = await resolve_tool_calls("it will be a sick leave", deps, history=history)
    assert result == [ToolCall(agent_id="hr_agent", tool_id="hr.apply_leave", args={
        "leave_type": "Sick Leave", "start_date": "2026-05-18", "end_date": "2026-05-20",
    })]
    # The fake recorded (user_message, catalogue, history).
    assert llm.route_calls[0][0] == "it will be a sick leave"
    assert llm.route_calls[0][2] == history


@pytest.mark.asyncio
async def test_resolve_history_defaults_to_none(agent_registry) -> None:
    llm = FakeLLMClient(route_result=[RoutedToolCall("hr_agent", "hr.read_balance", {})])
    deps = make_deps(llm_client=llm, mode="llm", agent_registry=agent_registry)
    await resolve_tool_calls("what's my balance", deps)
    assert llm.route_calls[0][2] is None
