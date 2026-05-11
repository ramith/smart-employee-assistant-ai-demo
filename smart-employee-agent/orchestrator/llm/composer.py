"""LLM-driven reply composition for the orchestrator chat loop.

``compose_reply`` is called by ``chat/routes.py`` at the end of the fan-out:
in LLM mode it asks Gemini for one natural-language reply covering every
tool's outcome; on any LLM failure (or in keyword mode) it returns
``fallback_text`` — the Sprint-1..4 ``_render_result`` concatenation. So a
total Gemini outage degrades the chat reply to exactly the keyword-mode
behaviour, never a hard error.

Stdlib + ``orchestrator.llm.client`` only — never imports langchain.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from orchestrator.llm.client import LLMError, ToolOutcome

if TYPE_CHECKING:  # pragma: no cover
    from orchestrator.chat.routes import ChatRouterDeps

__all__ = ["compose_reply"]

logger = logging.getLogger(__name__)


async def compose_reply(
    user_message: str,
    outcomes: list[ToolOutcome],
    fallback_text: str,
    deps: "ChatRouterDeps",
) -> str:
    """Return the chat reply.

    LLM-composed when ``LLM_FALLBACK_MODE=llm``, an ``llm_client`` is wired, and
    there is at least one outcome to talk about; otherwise (and on any LLM
    failure) ``fallback_text``.
    """
    use_llm = (
        getattr(deps.config, "llm_fallback_mode", "keyword") == "llm"
        and deps.llm_client is not None
        and bool(outcomes)
    )
    if use_llm:
        try:
            reply = await deps.llm_client.compose(user_message, outcomes)  # type: ignore[union-attr]
            logger.info("llm_composer_ok outcomes=%d", len(outcomes))
            return reply
        except (LLMError, Exception) as exc:  # noqa: BLE001 — any LLM failure → fallback text
            logger.warning("llm_composer_failed reason=%s falling_back", exc)
    return fallback_text
