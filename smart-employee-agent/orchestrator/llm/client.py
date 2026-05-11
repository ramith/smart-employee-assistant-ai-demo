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
    "ChatTurn",
    "ChatHistory",
    "LLMClient",
    "describe_llm_exc",
]

# S5.6: a chat turn is ``(role, text)`` where ``role`` is ``"user"`` or
# ``"assistant"``; a history is the chronologically-ordered list of prior turns
# (not including the current message). Replayed into the LLM router/composer
# prompts as the canonical LangChain message sequence (SystemMessage, then
# alternating HumanMessage/AIMessage, then the current HumanMessage).
ChatTurn = tuple[str, str]
ChatHistory = list[ChatTurn]


class LLMError(Exception):
    """Any LLM transport / parse / timeout failure.

    Callers (``router.resolve_tool_calls`` / ``composer.compose_reply``) catch
    this (plus, defensively, any other ``Exception``) and fall back to the
    keyword router / the ``_render_result`` concatenation. The demo never
    hard-fails on a Gemini hiccup.
    """


def describe_llm_exc(exc: BaseException) -> str:
    """A short, log-safe description of an LLM-call failure.

    Calls out the common Gemini free-tier case (HTTP 429 / quota exhausted —
    20 generate_content req/day per model) and auth errors, so the orchestrator
    log says *why* the keyword fallback kicked in. ``str(exc)`` from
    langchain/google-genai does not contain the API key; we still cap the
    length. Stdlib-only — usable from any module.
    """
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    if name == "ResourceExhausted" or "429" in text or "quota" in low or "rate limit" in low or "ratelimit" in low:
        return (
            f"{name}: Gemini quota / rate limit exceeded — likely the free-tier "
            f"daily cap (20 generate_content req/day per model). Enable billing on "
            f"the Google Cloud project, or set GEMINI_MODEL to a model with more "
            f"headroom. Falling back to keyword routing. [{text[:160]}]"
        )
    if name in {"PermissionDenied", "Unauthenticated"} or "api key" in low or "api_key" in low or " 401" in text or " 403" in text:
        return f"{name}: Gemini auth/permission error — check GEMINI_API_KEY. [{text[:160]}]"
    if name in {"TimeoutError", "CancelledError"}:
        return f"{name}: Gemini call exceeded the LLM_TIMEOUT_S budget (often a slow network or backoff after a 429)."
    return f"{name}: {text[:200]}"


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
        self,
        user_message: str,
        catalogue: list[ToolCatalogueEntry],
        *,
        history: ChatHistory | None = None,
    ) -> list[RoutedToolCall]:
        """Pick zero or more tools + extract their args, given the prior turns.
        Raises ``LLMError`` on failure."""
        ...

    async def compose(
        self,
        user_message: str,
        outcomes: list[ToolOutcome],
        *,
        history: ChatHistory | None = None,
    ) -> str:
        """Turn the tool outcomes into one natural-language reply, given the
        prior turns. Raises ``LLMError`` on failure."""
        ...
