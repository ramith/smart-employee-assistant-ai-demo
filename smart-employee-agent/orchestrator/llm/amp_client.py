"""``OpenAILLMClient`` — ``LLMClient`` backed by OpenAI directly via ``langchain-openai``.

Each public method is decorated with ``@atask`` (traceloop-sdk) which creates a
``traceloop.span.kind=task`` span. Inside each method we manually set
``traceloop.entity.input`` (system prompt + user message) and
``traceloop.entity.output`` (routing decisions / composed text) so the full
LLM context is visible in the AMP console.

``opentelemetry-instrumentation-langchain`` additionally patches
``ChatOpenAI.ainvoke()`` producing a nested ``ChatOpenAI.chat`` span with
``gen_ai.*`` attributes (raw API prompts, completions, token counts).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date

from langchain_core.messages import (  # type: ignore[import-not-found]
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_openai import ChatOpenAI  # type: ignore[import-not-found]
from opentelemetry import trace as otel_trace
from traceloop.sdk.decorators import atask  # type: ignore[import-not-found]

from orchestrator.llm import prompts as _prompts
from orchestrator.llm.client import (
    ChatHistory,
    LLMError,
    RoutedToolCall,
    ToolCatalogueEntry,
    ToolOutcome,
    describe_llm_exc,
)

__all__ = ["OpenAILLMClient"]

logger = logging.getLogger(__name__)


def _catalogue_to_tools(
    catalogue: list[ToolCatalogueEntry],
) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Build OpenAI tool spec dicts and a safe_name→(agent_id, tool_id) reverse map.

    OpenAI tool names must match ^[a-zA-Z0-9_-]{1,64}$, so dots in tool_ids
    are replaced with underscores. Tool IDs are globally unique (hr.* / it.*
    namespacing), so the reverse map is unambiguous.
    """
    specs: list[dict] = []
    name_map: dict[str, tuple[str, str]] = {}
    for e in catalogue:
        safe = e.tool_id.replace(".", "_")  # "hr.leave.apply" → "hr_leave_apply"
        name_map[safe] = (e.agent_id, e.tool_id)
        properties = {arg: {"type": "string", "description": arg} for arg in (e.args or ())}
        specs.append({
            "type": "function",
            "function": {
                "name": safe,
                "description": f"[{e.agent_id}] {e.description}",
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [],
                },
            },
        })
    return specs, name_map


def _history_messages(history: ChatHistory | None) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for role, text in history or []:
        text = (text or "").strip()
        if not text:
            continue
        if role == "assistant":
            out.append(AIMessage(content=text))
        else:
            out.append(HumanMessage(content=text))
    return out


class OpenAILLMClient:
    """LLM router + composer using OpenAI directly.

    Two ``ChatOpenAI`` handles: router at temp=0 (deterministic tool selection),
    composer at temp=0.3 (natural prose). Each method is instrumented with
    traceloop ``@atask`` and sets ``traceloop.entity.input/output`` manually so
    the full system prompt, user message, and LLM decision appear in AMP traces.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float,
        max_output_tokens: int,
        public_timeout_s: float | None = None,
        base_url: str | None = None,
        api_header: str = "api-key",
    ) -> None:
        if base_url:
            # AMP AI Gateway: key sent via custom header; bearer token is unused
            common: dict = dict(
                base_url=base_url,
                api_key="not-used",
                model=model,
                max_tokens=max_output_tokens,
                max_retries=2,
                default_headers={api_header: api_key},
            )
        else:
            # Standard OpenAI: Authorization: Bearer <api_key>
            common = dict(
                api_key=api_key,
                model=model,
                max_tokens=max_output_tokens,
                max_retries=2,
            )
        self._router_llm = ChatOpenAI(**common, temperature=0.0)
        self._composer_llm = ChatOpenAI(**common, temperature=0.3)
        self._timeout_s = float(timeout_s)
        self._public_timeout_s = (
            float(public_timeout_s) if public_timeout_s is not None else self._timeout_s
        )

    @atask(name="llm.route")
    async def route(
        self,
        user_message: str,
        catalogue: list[ToolCatalogueEntry],
        *,
        history: ChatHistory | None = None,
    ) -> list[RoutedToolCall]:
        system = _prompts.router_bind_system(today=date.today().isoformat())
        tool_specs, name_map = _catalogue_to_tools(catalogue)
        llm_with_tools = self._router_llm.bind_tools(tool_specs)
        span = otel_trace.get_current_span()
        span.set_attribute("traceloop.entity.input", json.dumps({
            "user_message": user_message,
            "system_prompt": system,
            "tools": [s["function"]["name"] for s in tool_specs],
            "history_turns": len(history or []),
        }))
        messages: list[BaseMessage] = [
            SystemMessage(content=system),
            *_history_messages(history),
            HumanMessage(content=user_message),
        ]
        try:
            resp = await asyncio.wait_for(
                llm_with_tools.ainvoke(messages), timeout=self._timeout_s
            )
            raw_calls: list[dict] = getattr(resp, "tool_calls", None) or []
            result: list[RoutedToolCall] = []
            for tc in raw_calls:
                name = tc.get("name", "")
                if name not in name_map:
                    logger.warning("bind_tools returned unknown tool name=%s", name)
                    continue
                agent_id, tool_id = name_map[name]
                result.append(RoutedToolCall(
                    agent_id=agent_id,
                    tool_id=tool_id,
                    args=tc.get("args") or {},
                ))
            span.set_attribute("traceloop.entity.output", json.dumps([
                {"agent_id": r.agent_id, "tool_id": r.tool_id, "args": r.args}
                for r in result
            ]))
            return result
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"router call failed: {describe_llm_exc(exc)}") from exc

    @atask(name="llm.compose")
    async def compose(
        self,
        user_message: str,
        outcomes: list[ToolOutcome],
        *,
        history: ChatHistory | None = None,
    ) -> str:
        system = _prompts.composer_system()
        body = _prompts.render_outcomes(outcomes)
        span = otel_trace.get_current_span()
        span.set_attribute("traceloop.entity.input", json.dumps({
            "user_message": user_message,
            "system_prompt": system,
            "outcomes": [{"tool_id": o.tool_id, "ok": o.ok} for o in outcomes],
        }))
        messages: list[BaseMessage] = [
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
            span.set_attribute("traceloop.entity.output", text)
            return text
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"composer call failed: {describe_llm_exc(exc)}") from exc

    @atask(name="llm.compose_public")
    async def compose_public(self, system_prompt: str, user_msg: str) -> str:
        span = otel_trace.get_current_span()
        span.set_attribute("traceloop.entity.input", json.dumps({
            "system_prompt": system_prompt,
            "user_message": user_msg,
        }))
        messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"<user_message>{user_msg}</user_message>"),
        ]
        try:
            resp = await asyncio.wait_for(
                self._composer_llm.ainvoke(messages), timeout=self._public_timeout_s
            )
            text = _prompts._coerce_text(getattr(resp, "content", None)).strip()
            if not text:
                raise LLMError("compose_public returned empty text")
            span.set_attribute("traceloop.entity.output", text)
            return text
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"compose_public call failed: {describe_llm_exc(exc)}") from exc
