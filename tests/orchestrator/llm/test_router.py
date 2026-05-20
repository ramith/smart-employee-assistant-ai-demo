"""Tests for orchestrator/llm/prompts.{strip_sensitive, render_outcomes,
router_bind_system, composer_system} and orchestrator/llm/router.{build_catalogue,
resolve_tool_calls}. No network, no langchain — uses FakeLLMClient.

Note: the router uses OpenAI function-calling via ``ChatOpenAI.bind_tools()`` —
there is no JSON-array parsing of the router output (the old
``parse_router_output`` was removed in the bind_tools migration), and the tool
catalogue is injected as function schemas by ``bind_tools`` rather than listed
in the prompt body."""

from __future__ import annotations

import pytest

from orchestrator.chat.keyword_fallback import ToolCall
from orchestrator.llm import prompts
from orchestrator.llm.client import LLMError, RoutedToolCall, ToolOutcome
from orchestrator.llm.router import build_catalogue, resolve_tool_calls

from tests.orchestrator.llm.conftest import FakeLLMClient, make_deps


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


# ── router_bind_system ───────────────────────────────────────────────────────


def test_router_bind_system_includes_date_and_rules() -> None:
    # bind_tools injects the tool catalogue as function schemas, so the prompt
    # body does NOT list tools — it only carries the routing rules + today's date.
    text = prompts.router_bind_system(today="2026-05-11")
    assert "Today is 2026-05-11" in text
    assert "routing layer" in text.lower()
    # The leave-routing guardrail must be present (CRITICAL leave routing rule).
    assert "hr.apply_leave" in text
    assert "hr.read_policy" in text


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
    llm = FakeLLMClient(route_result=LLMError("LLM exploded"))
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


# ── describe_llm_exc: human-readable LLM-failure summaries (quota / auth / timeout) ──


def test_describe_llm_exc_quota_429() -> None:
    from orchestrator.llm.client import describe_llm_exc

    class ResourceExhausted(Exception):
        pass

    msg = describe_llm_exc(ResourceExhausted(
        "429 You exceeded your current quota ... limit: 20, model: gpt-4.1"
    ))
    assert "quota" in msg.lower()
    assert "OPENAI_MODEL" in msg
    assert "Falling back to keyword routing" in msg


def test_describe_llm_exc_auth() -> None:
    from orchestrator.llm.client import describe_llm_exc

    class PermissionDenied(Exception):
        pass

    msg = describe_llm_exc(PermissionDenied("403 API key not valid"))
    assert "OPENAI_API_KEY" in msg


def test_describe_llm_exc_timeout() -> None:
    from orchestrator.llm.client import describe_llm_exc

    msg = describe_llm_exc(TimeoutError())
    assert "LLM_TIMEOUT_S" in msg


def test_describe_llm_exc_generic() -> None:
    from orchestrator.llm.client import describe_llm_exc

    msg = describe_llm_exc(ValueError("something odd"))
    assert "ValueError" in msg and "something odd" in msg
