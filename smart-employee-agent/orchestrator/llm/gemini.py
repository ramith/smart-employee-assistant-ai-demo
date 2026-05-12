"""``GeminiLLMClient`` — the production ``LLMClient``, backed by Gemini via
``langchain-google-genai``.

This is the ONLY module under ``orchestrator/`` that imports langchain. It is
imported lazily by ``orchestrator/main.py`` (inside the ``LLM_FALLBACK_MODE=llm``
+ key branch), so keyword-only deployments and the test suite never need the
package. Everything else depends only on ``orchestrator.llm.client``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

# Module-top langchain imports are fine here precisely because this module is
# only imported when LLM mode is active (the prod image has the deps).
from langchain_core.messages import (  # type: ignore[import-not-found]
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore[import-not-found]

from orchestrator.llm import prompts as _prompts
from orchestrator.llm.client import (
    ChatHistory,
    LLMError,
    RoutedToolCall,
    ToolCatalogueEntry,
    ToolOutcome,
    describe_llm_exc,
)

__all__ = ["GeminiLLMClient"]

logger = logging.getLogger(__name__)


def _history_messages(history: ChatHistory | None) -> list[BaseMessage]:
    """Map ``[(role, text), ...]`` prior turns to LangChain messages.

    Per the LangChain "conversation history" pattern: prior user turns become
    ``HumanMessage``, prior assistant turns become ``AIMessage``, in order,
    placed between the ``SystemMessage`` and the current ``HumanMessage`` when
    invoking the chat model. Empty/blank texts are skipped (a degenerate turn
    shouldn't break the sequence).
    """
    out: list[BaseMessage] = []
    for role, text in history or []:
        text = (text or "").strip()
        if not text:
            continue
        if role == "assistant":
            out.append(AIMessage(content=text))
        else:  # "user" (or anything else — treat as user input)
            out.append(HumanMessage(content=text))
    return out


class GeminiLLMClient:
    """LLM router + composer over Gemini.

    Two separate ``ChatGoogleGenerativeAI`` handles: the router runs at
    ``temperature=0`` (deterministic-ish tool selection), the composer at
    ``temperature=0.3`` (slightly more natural prose). Each call gets a hard
    ``asyncio.wait_for`` timeout; any failure (transport, quota, auth, parse,
    timeout) is re-raised as :class:`LLMError`, which the caller catches and
    falls back from. Error logs carry only ``type(exc).__name__`` + a truncated
    ``str(exc)`` — never ``repr(exc)`` or the request object — so the API key
    can't slip into a log line (defence-in-depth on top of the redaction filter).
    """

    def __init__(
        self, *, api_key: str, model: str, timeout_s: float, max_output_tokens: int
    ) -> None:
        # max_retries=2 → at most one quick retry. The default (6) means a 429
        # (quota exhausted — common on the Gemini free tier: 20 generate_content
        # req/day) burns ~60s of exponential backoff *inside* the call, which our
        # wait_for(timeout_s) then cancels as a cryptic TimeoutError. With a low
        # retry count the 429 surfaces in ~3s as a clear ResourceExhausted, so
        # the keyword fallback kicks in fast and the log says *why*. (The design
        # already wants "any LLM failure → fall back to keyword", so retrying
        # hard inside the LLM call works against that.)
        self._router_llm = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            temperature=0.0,
            max_output_tokens=max_output_tokens,
            max_retries=2,
        )
        self._composer_llm = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            temperature=0.3,
            max_output_tokens=max_output_tokens,
            max_retries=2,
        )
        self._timeout_s = float(timeout_s)

    # -- LLMClient -----------------------------------------------------------

    async def route(
        self,
        user_message: str,
        catalogue: list[ToolCatalogueEntry],
        *,
        history: ChatHistory | None = None,
    ) -> list[RoutedToolCall]:
        system = _prompts.router_system(catalogue, today=date.today().isoformat())
        # Canonical LangChain conversation sequence: system, then the prior
        # turns (HumanMessage / AIMessage), then the current user message.
        messages = [
            SystemMessage(content=system),
            *_history_messages(history),
            HumanMessage(content=user_message),
        ]
        try:
            resp = await asyncio.wait_for(
                self._router_llm.ainvoke(messages), timeout=self._timeout_s
            )
            return _prompts.parse_router_output(getattr(resp, "content", None))
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 — langchain raises a zoo; any failure → fall back
            raise LLMError(f"router call failed: {describe_llm_exc(exc)}") from exc

    async def compose(
        self,
        user_message: str,
        outcomes: list[ToolOutcome],
        *,
        history: ChatHistory | None = None,
    ) -> str:
        system = _prompts.composer_system()
        body = _prompts.render_outcomes(outcomes)
        messages = [
            SystemMessage(content=system),
            *_history_messages(history),
            HumanMessage(content=f"{body}\n\nThe user just said: {user_message}"),
        ]
        try:
            resp = await asyncio.wait_for(
                self._composer_llm.ainvoke(messages), timeout=self._timeout_s
            )
            text = _prompts._coerce_text(getattr(resp, "content", None)).strip()
            if not text:
                raise LLMError("composer returned empty text")
            return text
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"composer call failed: {describe_llm_exc(exc)}") from exc
