"""Tests for orchestrator/llm/composer.compose_reply. No network, no langchain."""

from __future__ import annotations

import pytest

from orchestrator.llm.client import LLMError, ToolOutcome
from orchestrator.llm.composer import compose_reply

from tests.orchestrator.llm.conftest import FakeLLMClient, make_deps

_OUTCOMES = [ToolOutcome("hr_agent", "hr.read_balance", True, data={"balance": {"annual": 20}})]


@pytest.mark.asyncio
async def test_compose_uses_llm_reply_when_available() -> None:
    llm = FakeLLMClient(compose_result="You have 20 annual leave days left.")
    deps = make_deps(llm_client=llm, mode="llm")
    reply = await compose_reply("how much leave", _OUTCOMES, fallback_text="FALLBACK", deps=deps)
    assert reply == "You have 20 annual leave days left."
    assert llm.compose_calls and llm.compose_calls[0][1] == _OUTCOMES


@pytest.mark.asyncio
async def test_compose_falls_back_on_llm_error() -> None:
    llm = FakeLLMClient(compose_result=LLMError("LLM timed out"))
    deps = make_deps(llm_client=llm, mode="llm")
    reply = await compose_reply("how much leave", _OUTCOMES, fallback_text="FALLBACK", deps=deps)
    assert reply == "FALLBACK"


@pytest.mark.asyncio
async def test_compose_returns_fallback_when_no_outcomes() -> None:
    llm = FakeLLMClient(compose_result="should not be used")
    deps = make_deps(llm_client=llm, mode="llm")
    reply = await compose_reply("hi", [], fallback_text="FALLBACK", deps=deps)
    assert reply == "FALLBACK"
    assert not llm.compose_calls


@pytest.mark.asyncio
async def test_compose_returns_fallback_in_keyword_mode() -> None:
    llm = FakeLLMClient(compose_result="should not be used")
    deps = make_deps(llm_client=llm, mode="keyword")
    reply = await compose_reply("hi", _OUTCOMES, fallback_text="FALLBACK", deps=deps)
    assert reply == "FALLBACK"
    assert not llm.compose_calls


@pytest.mark.asyncio
async def test_compose_returns_fallback_when_no_llm_client() -> None:
    deps = make_deps(llm_client=None, mode="llm")
    reply = await compose_reply("hi", _OUTCOMES, fallback_text="FALLBACK", deps=deps)
    assert reply == "FALLBACK"


# ── S5.6: chat-history is threaded through to the LLM composer ────────────────


@pytest.mark.asyncio
async def test_compose_passes_history_to_llm() -> None:
    llm = FakeLLMClient(compose_result="Your sick leave for May 18-20 has been submitted.")
    deps = make_deps(llm_client=llm, mode="llm")
    history = [("user", "I want to apply for leave"), ("assistant", "What type?")]
    outcomes = [ToolOutcome("hr_agent", "hr.apply_leave", True, data={"success": True, "request_id": "LR007"})]
    reply = await compose_reply("it will be sick leave May 18-20", outcomes, fallback_text="F", deps=deps, history=history)
    assert reply == "Your sick leave for May 18-20 has been submitted."
    assert llm.compose_calls[0][0] == "it will be sick leave May 18-20"
    assert llm.compose_calls[0][1] == outcomes
    assert llm.compose_calls[0][2] == history
