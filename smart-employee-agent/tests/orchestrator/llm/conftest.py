"""Shared fixtures for the orchestrator LLM-layer tests.

``FakeLLMClient`` satisfies the ``LLMClient`` Protocol structurally; tests
construct it with what ``route()`` / ``compose()`` should do (a value to
return, or an ``Exception`` to raise) and inspect what it was called with.
No network, no langchain.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from orchestrator.agent_registry.cards import AgentRegistry
from orchestrator.chat.keyword_fallback import KeywordRouter
from orchestrator.llm.client import RoutedToolCall, ToolCatalogueEntry, ToolOutcome

_FIXTURE_CARDS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "agent_cards"


class FakeLLMClient:
    """An ``LLMClient`` stand-in for tests."""

    def __init__(
        self,
        *,
        route_result: list[RoutedToolCall] | Exception | None = None,
        compose_result: str | Exception = "Here is your answer.",
    ) -> None:
        self._route_result = [] if route_result is None else route_result
        self._compose_result = compose_result
        self.route_calls: list[tuple[str, list[ToolCatalogueEntry]]] = []
        self.compose_calls: list[tuple[str, list[ToolOutcome]]] = []

    async def route(
        self, user_message: str, catalogue: list[ToolCatalogueEntry]
    ) -> list[RoutedToolCall]:
        self.route_calls.append((user_message, catalogue))
        if isinstance(self._route_result, Exception):
            raise self._route_result
        return list(self._route_result)

    async def compose(self, user_message: str, outcomes: list[ToolOutcome]) -> str:
        self.compose_calls.append((user_message, outcomes))
        if isinstance(self._compose_result, Exception):
            raise self._compose_result
        return self._compose_result


@pytest.fixture()
def agent_registry() -> AgentRegistry:
    """The real demo agent cards (hr_agent + it_agent), loaded from the fixtures dir."""
    json_paths = [
        _FIXTURE_CARDS_DIR / "hr_agent_valid.json",
        _FIXTURE_CARDS_DIR / "it_agent_valid.json",
    ]
    return AgentRegistry.from_files(json_paths)


def make_deps(
    *,
    llm_client: object | None = None,
    mode: str = "llm",
    agent_registry: AgentRegistry | None = None,
) -> types.SimpleNamespace:
    """Build a minimal duck-typed ``ChatRouterDeps`` substitute for the
    router/composer (which only read ``.config.llm_fallback_mode``, ``.llm_client``,
    ``.keyword_router``, ``.agent_registry``, ``.a2a_clients``)."""
    reg = agent_registry
    if reg is None:
        reg = AgentRegistry.from_files(
            [
                _FIXTURE_CARDS_DIR / "hr_agent_valid.json",
                _FIXTURE_CARDS_DIR / "it_agent_valid.json",
            ]
        )
    return types.SimpleNamespace(
        config=types.SimpleNamespace(llm_fallback_mode=mode),
        llm_client=llm_client,
        keyword_router=KeywordRouter(),
        agent_registry=reg,
        a2a_clients={"hr_agent": object(), "it_agent": object()},
    )
