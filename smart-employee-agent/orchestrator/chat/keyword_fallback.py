"""Deterministic keyword-based router for the orchestrator chat loop.

This module is the Wave 5, Sprint 1 implementation of F-14
(``LLM_FALLBACK_MODE=keyword``).  It has **zero** runtime dependencies
beyond the Python standard library and may be imported at any point in
the service lifecycle.

Typical usage
-------------
The orchestrator's ``chat/routes.py`` calls this when either:

1. The ``LLM_FALLBACK_MODE=keyword`` environment variable is set (default
   in the demo run-book), **or**
2. The Gemini LLM is unreachable and the chat loop falls back.

Example::

    from orchestrator.chat.keyword_fallback import KeywordRouter, DEFAULT_RULES

    router = KeywordRouter()                        # uses DEFAULT_RULES
    calls  = router.route("What's my leave balance?")
    # → [ToolCall(agent_id='hr_agent', tool_id='hr.read_balance', args={})]

    calls  = router.route("I need a laptop and want time off")
    # → [ToolCall(agent_id='hr_agent', tool_id='hr.read_balance', args={}),
    #    ToolCall(agent_id='it_agent', tool_id='it.list_available_assets', args={})]

Design notes
------------
- Each :class:`KeywordRule` fires **at most once** per message, regardless
  of how many of its keywords appear.
- Returned :class:`ToolCall` ordering mirrors the rule ordering in the
  tuple passed to :class:`KeywordRouter`; this gives deterministic
  serial fan-out per UC-02/UC-03 (HR first, IT second by default).
- :class:`ToolCall` and :class:`KeywordRule` are frozen dataclasses —
  any mutation attempt raises :class:`dataclasses.FrozenInstanceError`.
- Matching is case-insensitive and uses a leading ``\\b`` word-boundary
  anchor so that short tokens like ``"pto"`` do not false-fire mid-word
  (e.g. inside ``"laptops"``), while stem keywords like ``"laptop"``
  still match plural forms like ``"laptops"``.  Multi-word keywords
  (e.g. ``"time off"``) are anchored at the start of the first word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A resolved routing decision — one call the orchestrator should make.

    Attributes:
        agent_id: Slug of the target specialist, e.g. ``"hr_agent"``.
        tool_id:  Fully-qualified tool identifier, e.g. ``"hr.read_balance"``.
        args:     Tool-specific arguments; empty dict for parameter-less tools.
    """

    agent_id: str
    tool_id: str
    args: dict


@dataclass(frozen=True, slots=True)
class KeywordRule:
    """A single routing rule mapping keywords to a specific tool call.

    A rule fires when **any** of its ``keywords`` appears as a
    case-insensitive substring of the user message.  Multiple keyword hits
    on the same rule still produce exactly one :class:`ToolCall`.

    Attributes:
        keywords:  Tuple of lower-case substrings to match against (any hit
                   triggers the rule).
        agent_id:  Target specialist slug, e.g. ``"hr_agent"``.
        tool_id:   Fully-qualified tool identifier, e.g. ``"hr.read_balance"``.
        args:      Forwarded verbatim to :class:`ToolCall`; defaults to ``{}``.
    """

    keywords: tuple[str, ...]
    agent_id: str
    tool_id: str
    args: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Default rules (F-14)
# ---------------------------------------------------------------------------

DEFAULT_RULES: tuple[KeywordRule, ...] = (
    # Specific verbs first; dedup-by-agent in route() means the first match
    # per agent wins. "approve my leave" therefore fires hr.approve_leave only,
    # not also hr.read_balance.
    KeywordRule(
        keywords=("approve", "approval"),
        agent_id="hr_agent",
        tool_id="hr.approve_leave",
    ),
    KeywordRule(
        keywords=("leave", "vacation", "time off", "pto"),
        agent_id="hr_agent",
        tool_id="hr.read_balance",
    ),
    KeywordRule(
        keywords=("laptop", "asset", "equipment", "hardware", "computer"),
        agent_id="it_agent",
        tool_id="it.list_available_assets",
    ),
)
"""Default routing rules shipped with the demo run-book (F-14).

Rule 0 — HR approve (HR Admin write path; D2.7)
    Keywords ``approve``, ``approval`` → ``hr_agent / hr.approve_leave``.

Rule 1 — HR leave read
    Keywords ``leave``, ``vacation``, ``time off``, ``pto`` → ``hr_agent /
    hr.read_balance``.

Rule 2 — IT assets read
    Keywords ``laptop``, ``asset``, ``equipment``, ``hardware``, ``computer``
    → ``it_agent / it.list_available_assets``.

The ``it.issue_asset`` rule is added in Sprint 2A.2 once the MCP tool is built.
"""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _compile_patterns(keywords: Sequence[str]) -> list[re.Pattern[str]]:
    """Compile each keyword into a leading-word-boundary case-insensitive pattern.

    A leading ``\\b`` anchor prevents short tokens like ``"pto"`` from
    matching when they appear **mid-word** (e.g. ``"pto"`` inside
    ``"laptops"`` at character positions 2-4).  No trailing boundary is
    applied so that stem keywords such as ``"laptop"`` still match plural
    forms like ``"laptops"``.

    Multi-word phrases (e.g. ``"time off"``) work naturally because the
    leading ``\\b`` anchors to the start of the first word.

    Args:
        keywords: Iterable of plain-text keyword strings.

    Returns:
        A list of compiled :class:`re.Pattern` objects in the same order as
        *keywords*.
    """
    patterns: list[re.Pattern[str]] = []
    for kw in keywords:
        escaped = re.escape(kw)
        patterns.append(re.compile(r"\b" + escaped, re.IGNORECASE))
    return patterns


class KeywordRouter:
    """Deterministic keyword-based router; no LLM, no network.

    Each call to :meth:`route` scans the user message against the configured
    rules and returns an ordered list of :class:`ToolCall` objects.  The order
    mirrors rule order so that serial fan-out in the chat loop is predictable.

    Matching uses a leading ``\\b`` word-boundary anchor so short tokens such
    as ``"pto"`` do not fire on words that happen to contain those characters
    mid-word (e.g. ``"laptops"``), while stem keywords like ``"laptop"`` still
    match plural forms like ``"laptops"``.

    Args:
        rules: Tuple of :class:`KeywordRule` objects defining the routing
               table.  Defaults to :data:`DEFAULT_RULES` when omitted.
    """

    def __init__(self, rules: tuple[KeywordRule, ...] = DEFAULT_RULES) -> None:
        """Initialise the router and pre-compile keyword patterns.

        Args:
            rules: Routing rules to apply.  Order is significant — the output
                   list from :meth:`route` reflects this order.
        """
        self._rules: tuple[KeywordRule, ...] = rules
        # Pre-compile one pattern list per rule to avoid re-compiling on every call.
        self._patterns: list[list[re.Pattern[str]]] = [
            _compile_patterns(rule.keywords) for rule in rules
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rule_matches(self, rule_index: int, text: str) -> bool:
        """Return True if any keyword pattern for *rule_index* matches *text*.

        Args:
            rule_index: Index into ``self._rules`` / ``self._patterns``.
            text:       The user message to test (original case; patterns are
                        case-insensitive).

        Returns:
            True if at least one keyword pattern produces a match.
        """
        return any(p.search(text) for p in self._patterns[rule_index])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(self, user_message: str) -> list[ToolCall]:
        """Return an ordered list of :class:`ToolCall` objects for *user_message*.

        Each rule is evaluated in order. At most one ToolCall is emitted per
        ``agent_id`` — the first matching rule per agent wins. This lets the
        rule table list specific verbs (e.g. ``approve``) before general nouns
        (e.g. ``leave``) so "approve my leave" routes to ``hr.approve_leave``
        only, not also to ``hr.read_balance``.

        Args:
            user_message: Raw user input string.

        Returns:
            Ordered list of :class:`ToolCall` objects; empty list when no
            rule matches.
        """
        result: list[ToolCall] = []
        seen_agents: set[str] = set()
        for idx, rule in enumerate(self._rules):
            if rule.agent_id in seen_agents:
                continue
            if self._rule_matches(idx, user_message):
                result.append(
                    ToolCall(
                        agent_id=rule.agent_id,
                        tool_id=rule.tool_id,
                        args=rule.args,
                    )
                )
                seen_agents.add(rule.agent_id)
        return result

    def explain(self, user_message: str) -> str:
        """Return a diagnostic string describing which rules fired.

        Intended for ops / debug logs — never exposed to end users.

        Args:
            user_message: Raw user input string.

        Returns:
            A human-readable string such as
            ``"matched: hr.read_balance (hr_agent), it.list_available_assets (it_agent)"``
            or ``"no match"`` when no rule triggered.
        """
        matched: list[str] = []
        for idx, rule in enumerate(self._rules):
            if self._rule_matches(idx, user_message):
                matched.append(f"{rule.tool_id} ({rule.agent_id})")
        if matched:
            return "matched: " + ", ".join(matched)
        return "no match"
