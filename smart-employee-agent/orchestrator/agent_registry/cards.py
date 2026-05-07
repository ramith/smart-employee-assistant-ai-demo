"""In-memory registry of AgentCards for the orchestrator.

This module is the Wave 4 deliverable for Sprint 1 (F-12 reassignment).
It depends only on Wave 1+2 artefacts: ``common.a2a.agent_card``.

Typical lifecycle
-----------------
At orchestrator startup, load known cards from local JSON files::

    registry = AgentRegistry.from_files([
        Path("config/hr_agent_card.json"),
        Path("config/it_agent_card.json"),
    ])

During a request the orchestrator uses the registry two ways:

1. ``registry.llm_tool_list()`` → flattened list of skill dicts injected
   into the Gemini system prompt.
2. ``card = registry.find_by_tool("hr.get_leave_balance")`` → resolves the
   card whose ``base_url`` the orchestrator will route the A2A call to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from common.a2a.agent_card import AgentCard, llm_projection

logger = logging.getLogger(__name__)


@dataclass
class AgentRegistry:
    """In-memory store of AgentCards, keyed by AgentCard.id.

    Populated via :meth:`from_files` (local JSON) or by calling
    :meth:`add` directly (e.g. after a remote HTTP fetch).

    Once populated for a request the registry is logically frozen;
    mutations go through :meth:`add` (which silently replaces an existing
    entry with the same id).

    Attributes:
        _cards: Internal ordered dict mapping agent id to AgentCard.
            Insertion order is preserved and reflected by :meth:`all`.
    """

    _cards: dict[str, AgentCard] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def add(self, card: AgentCard) -> None:
        """Insert or replace a card, keyed by ``card.id``.

        Replacing an existing entry does not change its position in
        insertion order — the slot is updated in-place.

        Args:
            card: A validated :class:`~common.a2a.agent_card.AgentCard`
                instance.
        """
        self._cards[card.id] = card

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def get(self, agent_id: str) -> AgentCard | None:
        """Return the card for *agent_id*, or ``None`` if not registered.

        Args:
            agent_id: The opaque slug from ``AgentCard.id``, e.g.
                ``"hr_agent"``.

        Returns:
            The matching :class:`~common.a2a.agent_card.AgentCard`, or
            ``None``.
        """
        return self._cards.get(agent_id)

    def all(self) -> list[AgentCard]:
        """Return a stable-ordered (insertion order) snapshot of all cards.

        Returns:
            A new list of :class:`~common.a2a.agent_card.AgentCard`
            instances; mutating the list does not affect the registry.
        """
        return list(self._cards.values())

    def find_by_tool(self, tool_id: str) -> AgentCard | None:
        """Return the first card whose skills contain *tool_id*.

        Tool IDs are namespaced (e.g. ``"hr.read_balance"``); the search
        is an exact match against :attr:`~common.a2a.agent_card.Skill.tool_id`
        across all registered cards, in insertion order.

        Args:
            tool_id: The fully-qualified tool identifier to look up.

        Returns:
            The first matching :class:`~common.a2a.agent_card.AgentCard`,
            or ``None`` if no card advertises *tool_id*.
        """
        for card in self._cards.values():
            for skill in card.skills:
                if skill.tool_id == tool_id:
                    return card
        return None

    def llm_tool_list(self) -> list[dict]:
        """Project all cards' skills into the LLM tool description list.

        Calls :func:`~common.a2a.agent_card.llm_projection` per card and
        flattens the resulting skill dicts across all cards.  Each entry
        is enriched with ``agent_id`` and ``agent_label`` so the LLM can
        correlate a chosen skill back to the agent that owns it.

        The output is intentionally stripped of all auth and infrastructure
        metadata: ``base_url``, ``oauth_client_id``, and any other private
        fields from :class:`~common.a2a.agent_card.AgentCard` are never
        included.

        Returns:
            A list of dicts, each with the shape::

                {
                    "tool_id": "<ns.verb>",
                    "label": "<human name>",
                    "description": "<one sentence>",
                    "scope": "<a2a scope string>",
                    "agent_id": "<agent slug>",
                    "agent_label": "<agent display name>",
                }
        """
        result: list[dict] = []
        for card in self._cards.values():
            projection = llm_projection(card)
            for skill in projection["skills"]:
                result.append(
                    {
                        "tool_id": skill["tool_id"],
                        "label": skill["label"],
                        "description": skill["description"],
                        "scope": skill["scope"],
                        "agent_id": projection["id"],
                        "agent_label": projection["label"],
                    }
                )
        return result

    # ------------------------------------------------------------------
    # Class-method constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_files(cls, paths: list[str | Path]) -> "AgentRegistry":
        """Load cards from JSON files, skipping unparseable ones.

        Each file is read as UTF-8 text and validated via
        :meth:`~common.a2a.agent_card.AgentCard.model_validate_json`.
        If validation or I/O fails the file is skipped with a ``WARNING``
        log entry rather than raising; Sprint 2 may tighten this policy.

        Args:
            paths: Sequence of file paths (``str`` or ``pathlib.Path``)
                pointing to agent-card JSON documents.

        Returns:
            A new :class:`AgentRegistry` populated with all cards that
            parsed successfully.  Returns an empty registry if every file
            fails to parse.
        """
        registry = cls()
        for raw_path in paths:
            path = Path(raw_path)
            try:
                text = path.read_text(encoding="utf-8")
                card = AgentCard.model_validate_json(text)
                registry.add(card)
                logger.debug("Loaded agent card '%s' from %s", card.id, path)
            except Exception as exc:  # noqa: BLE001 — intentional broad catch per spec
                logger.warning(
                    "Skipping agent card at '%s': %s — %s",
                    path,
                    type(exc).__name__,
                    exc,
                )
        return registry
