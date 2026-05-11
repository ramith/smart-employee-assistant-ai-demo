"""LLM-driven tool routing for the orchestrator chat loop.

``resolve_tool_calls`` is the single entry point ``chat/routes.py`` calls
instead of ``keyword_router.route(...)``:

1. If ``LLM_FALLBACK_MODE != "llm"`` or there's no ``llm_client`` → keyword router.
2. Otherwise: one Gemini call → parse → **validate every returned tool against
   the agent registry** (drop unknown ``agent_id``/``tool_id``; strip
   hallucinated arg keys + non-scalar values; drop the agent-internal
   ``hr.lookup_employee`` if it ever appears) → if any survive, use them;
   else fall back to the keyword router.
3. Any LLM failure (transport/parse/timeout) → keyword router.

The surviving list is ``list[ToolCall]`` — the exact shape ``keyword_router.route``
produces — so the rest of the fan-out in ``chat/routes.py`` is unchanged.

Stdlib + ``orchestrator.llm.client`` + ``orchestrator.chat.keyword_fallback``
(both stdlib-only) — never imports langchain.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from orchestrator.chat.keyword_fallback import ToolCall
from orchestrator.llm.client import LLMError, RoutedToolCall, ToolCatalogueEntry

if TYPE_CHECKING:  # pragma: no cover
    from orchestrator.chat.routes import ChatRouterDeps

__all__ = ["resolve_tool_calls", "build_catalogue"]

logger = logging.getLogger(__name__)

# Tools the LLM is not allowed to route to even if (mistakenly) carded — these
# are agent-internal helpers, not user-facing chat intents, and their results
# carry data we don't surface (sprint-5.md §2.7). Belt-and-braces; the card
# fixtures already omit them from ``skills[]``.
_INTERNAL_ONLY_TOOLS = frozenset({"hr.lookup_employee"})

_JSON_SCALARS = (str, int, float, bool)


def build_catalogue(agent_registry: Any) -> list[ToolCatalogueEntry]:
    """Build the tool catalogue from ``AgentRegistry.llm_tool_list()``."""
    out: list[ToolCatalogueEntry] = []
    for entry in agent_registry.llm_tool_list():
        out.append(
            ToolCatalogueEntry(
                agent_id=str(entry.get("agent_id", "")),
                tool_id=str(entry.get("tool_id", "")),
                label=str(entry.get("label", entry.get("tool_id", ""))),
                description=str(entry.get("description", "")),
                args=tuple(str(a) for a in (entry.get("args") or ())),
            )
        )
    return out


def _validate(routed: list[RoutedToolCall], deps: "ChatRouterDeps") -> list[ToolCall]:
    """Drop anything not in the registry; filter hallucinated args."""
    out: list[ToolCall] = []
    registry = deps.agent_registry
    a2a_clients = deps.a2a_clients
    for rc in routed:
        if rc.tool_id in _INTERNAL_ONLY_TOOLS:
            logger.warning("llm_router_dropped_internal_tool tool=%s", rc.tool_id)
            continue
        if rc.agent_id not in a2a_clients:
            logger.warning(
                "llm_router_dropped_unknown_agent agent=%s tool=%s", rc.agent_id, rc.tool_id
            )
            continue
        card = registry.get(rc.agent_id)
        if card is None:
            logger.warning(
                "llm_router_dropped_unknown_agent agent=%s tool=%s", rc.agent_id, rc.tool_id
            )
            continue
        skill = next((s for s in getattr(card, "skills", []) if getattr(s, "tool_id", None) == rc.tool_id), None)
        if skill is None:
            logger.warning(
                "llm_router_dropped_unknown_tool agent=%s tool=%s", rc.agent_id, rc.tool_id
            )
            continue
        # Allowed arg names for this skill (the card is the contract).
        allowed: set[str] = set(getattr(skill, "args", []) or [])
        filtered = {
            k: v
            for k, v in (rc.args or {}).items()
            if k in allowed and isinstance(v, _JSON_SCALARS)
        }
        out.append(ToolCall(agent_id=rc.agent_id, tool_id=rc.tool_id, args=filtered))
    return out


async def resolve_tool_calls(user_message: str, deps: "ChatRouterDeps") -> list[ToolCall]:
    """Resolve the user's message to an ordered list of ``ToolCall``s.

    LLM-primary when configured; the keyword router is the fallback for an LLM
    failure, an empty/all-invalid LLM response, or keyword-mode deployments.
    """
    use_llm = getattr(deps.config, "llm_fallback_mode", "keyword") == "llm" and deps.llm_client is not None
    if use_llm:
        try:
            catalogue = build_catalogue(deps.agent_registry)
            routed = await deps.llm_client.route(user_message, catalogue)  # type: ignore[union-attr]
            tool_calls = _validate(routed, deps)
            if tool_calls:
                logger.info("llm_router_ok tools=%s", [tc.tool_id for tc in tool_calls])
                return tool_calls
            logger.info("llm_router_empty_or_all_invalid falling_back_to_keyword")
        except (LLMError, Exception) as exc:  # noqa: BLE001 — any LLM-path failure → keyword fallback
            logger.warning("llm_router_failed reason=%s falling_back_to_keyword", exc)
    return deps.keyword_router.route(user_message)
