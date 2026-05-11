"""The ``LLMClient`` Protocol and the data types that cross it.

Stdlib-only on purpose: every other ``orchestrator/`` module imports from here
(never from ``gemini.py``), so the package stays importable without
``langchain-google-genai`` installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "LLMError",
    "ToolCatalogueEntry",
    "RoutedToolCall",
    "ToolOutcome",
    "LLMClient",
]


class LLMError(Exception):
    """Any LLM transport / parse / timeout failure.

    Callers (``router.resolve_tool_calls`` / ``composer.compose_reply``) catch
    this (plus, defensively, any other ``Exception``) and fall back to the
    keyword router / the ``_render_result`` concatenation. The demo never
    hard-fails on a Gemini hiccup.
    """


@dataclass(frozen=True)
class ToolCatalogueEntry:
    """One entry of the tool catalogue handed to the LLM router prompt.

    Derived from the agent cards (``AgentRegistry.llm_tool_list()``) — carries
    only public-ish metadata (no scopes-as-policy, no URLs, no client ids).
    """

    agent_id: str
    tool_id: str
    label: str
    description: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutedToolCall:
    """A raw routing decision from the LLM, *before* registry validation.

    ``args`` is whatever JSON object the model emitted; the router filters it
    down to the catalogue entry's declared arg names + JSON scalars before it
    becomes a chat ``ToolCall``.
    """

    agent_id: str
    tool_id: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutcome:
    """The result of one tool that ran during the fan-out, fed to the composer.

    On success ``ok=True`` and ``data`` is the tool's result dict (the composer
    sees a *sanitised* copy — see ``prompts.strip_sensitive``). On failure
    ``ok=False`` and ``error_id`` / ``reason`` describe what went wrong
    (``ERR-CIBA-005`` for a declined consent, ``ERR-AGENT-002`` for missing
    args, etc.).
    """

    agent_id: str
    tool_id: str
    ok: bool
    data: dict | None = None
    error_id: str | None = None
    reason: str | None = None


@runtime_checkable
class LLMClient(Protocol):
    """What ``orchestrator/chat`` depends on. ``GeminiLLMClient`` implements it;
    tests inject a ``FakeLLMClient``."""

    async def route(
        self, user_message: str, catalogue: list[ToolCatalogueEntry]
    ) -> list[RoutedToolCall]:
        """Pick zero or more tools + extract their args. Raises ``LLMError`` on failure."""
        ...

    async def compose(self, user_message: str, outcomes: list[ToolOutcome]) -> str:
        """Turn the tool outcomes into one natural-language reply. Raises ``LLMError`` on failure."""
        ...
