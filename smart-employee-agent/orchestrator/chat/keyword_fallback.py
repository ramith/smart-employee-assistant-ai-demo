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
    #
    # Sprint 4 S4.1 (UC-11): cubicle intents are listed FIRST so that
    # "show me vacant cubicles" doesn't fall through to the generic leave
    # rule, and "assign C-027 to jane.doe" doesn't trigger the IT issue rule
    # (which also has "assign" as a keyword).
    #
    # Sprint 4 S4.2 (UC-12): cubicle.lookup_self comes before cubicle_summary
    # so "where is my cubicle" routes to the self-service tool, not the
    # admin-only summary read.
    # NB: "cubicle" == "seat" == "seating (arrangement)" — synonyms throughout
    # the cubicle rules. ("desk" is deliberately NOT a synonym — it collides
    # with "hardware for my desk" / "desktop".)
    KeywordRule(
        keywords=(
            "where is my cubicle", "my cubicle", "show my cubicle",
            "where is my seat", "my seat", "show my seat",
        ),
        agent_id="hr_agent",
        tool_id="hr.cubicle_lookup_self",
    ),
    KeywordRule(
        keywords=(
            "vacant cubicle", "vacant cubicles", "show cubicles", "cubicle summary",
            "vacant seat", "vacant seats", "available seats", "free seats",
            "show seats", "seat summary",
        ),
        agent_id="hr_agent",
        tool_id="hr.cubicle_summary",
    ),
    KeywordRule(
        keywords=("floor", "show me floor"),
        agent_id="hr_agent",
        tool_id="hr.cubicle_list_floor",
    ),
    KeywordRule(
        keywords=("assign cubicle", "assign c-", "assign seat", "cubicle", "seat", "assign"),
        agent_id="hr_agent",
        tool_id="hr.cubicle_assign",
    ),
    # Sprint 5 — broad cubicle/seat catch-all (the allocation-flow entry point).
    # Comes AFTER the more-specific cubicle rules above (lookup-self,
    # vacant-summary, floor-N, assign-by-id) so those win first; this only
    # fires for vague mentions — "show me the cubicle assignments", "I need to
    # allocate a cubicle", "cubicle allocation", "seating arrangement" — and
    # routes to the per-floor vacancy summary, step 1 of the flow. ("cubicle"
    # / "seat" / "seating" alone is enough, so typos like "assinments" match.)
    KeywordRule(
        keywords=("cubicle", "cubicles", "seat", "seats", "seating"),
        agent_id="hr_agent",
        tool_id="hr.cubicle_summary",
    ),
    # Sprint 5 — HR Admin list-all-leaves intent (hr.read_all_leaves).
    #
    # Placed BEFORE the bare approve/approval rule so that multi-word
    # list-intent phrases like "leave requests I need to approve" and
    # "leaves to approve" route here rather than to hr.approve_leave.
    #
    # "approve LV-004" does NOT match because it contains no multi-word
    # phrase from this rule's keyword list — it doesn't say "leave requests",
    # "all leaves", "pending leaves", etc. — so it falls through to the
    # approve_leave rule below. Verified by test.
    #
    # The _extract_inline_args handler sets status="Pending" when the message
    # signals a pending filter ("pending", "need to approve", etc.).
    KeywordRule(
        keywords=(
            "leave requests",
            "all leaves",
            "leaves to approve",
            "leaves others submitted",
            "pending leaves",
            "need to approve",
            "others submitted",
        ),
        agent_id="hr_agent",
        tool_id="hr.read_all_leaves",
    ),
    KeywordRule(
        keywords=("approve", "approval"),
        agent_id="hr_agent",
        tool_id="hr.approve_leave",
    ),
    KeywordRule(
        keywords=("issue", "give"),
        agent_id="it_agent",
        tool_id="it.issue_asset",
    ),
    # apply_leave: matches when the user names a specific leave type alongside
    # submit/apply/request/book intent. Must precede read_policy so these
    # phrasings don't fall through to the policy-read rule.
    KeywordRule(
        keywords=(
            "annual leave from", "sick leave from", "personal leave from",
            "apply for annual", "apply for sick", "apply for personal",
            "submit annual", "submit sick", "submit personal",
            "request annual", "request sick", "request personal",
            "book annual", "book sick", "book personal",
            "take annual", "take sick", "take personal",
        ),
        agent_id="hr_agent",
        tool_id="hr.apply_leave",
    ),
    # read_policy: purely informational questions with no specific leave type or
    # dates. "apply for" alone (without a leave type) stays here so the user
    # gets the policy hint before they know which type to pick.
    KeywordRule(
        keywords=(
            "leave types", "leave policy", "types of leave", "kinds of leave",
            "what leaves", "which leaves", "what leave can", "apply for",
        ),
        agent_id="hr_agent",
        tool_id="hr.read_policy",
    ),
    KeywordRule(
        keywords=("leave", "vacation", "time off", "pto"),
        agent_id="hr_agent",
        tool_id="hr.read_balance",
    ),
    # Sprint 4 S4.2 (UC-12): IT self-service. First-person possessive phrases
    # route to it.get_my_assets; the legacy laptop rule below still handles
    # "show me available laptops" (Sprint 1 admin-grade browse path).
    KeywordRule(
        keywords=("what laptop do i have", "my laptop", "my assets", "my equipment", "my devices"),
        agent_id="it_agent",
        tool_id="it.get_my_assets",
    ),
    KeywordRule(
        keywords=(
            "laptop", "phone", "monitor", "screen",
            "asset", "equipment", "hardware", "computer",
        ),
        agent_id="it_agent",
        tool_id="it.list_available_assets",
    ),
)
"""Default routing rules shipped with the demo run-book (F-14).

Rule 0 — HR approve (HR Admin write path; D2.7)
    Keywords ``approve``, ``approval`` → ``hr_agent / hr.approve_leave``.

Rule 1 — IT issue (HR Admin write path; D2.8)
    Keywords ``issue``, ``assign``, ``give`` → ``it_agent / it.issue_asset``.

Rule 2 — HR leave read
    Keywords ``leave``, ``vacation``, ``time off``, ``pto`` → ``hr_agent /
    hr.read_balance``.

Rule 3 — IT assets read
    Keywords ``laptop``, ``asset``, ``equipment``, ``hardware``, ``computer``
    → ``it_agent / it.list_available_assets``.
"""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


_LEAVE_ID_RE = re.compile(r"\b(?:LV-\d+|LR\d+|LR-\d+)\b", re.IGNORECASE)
_ASSET_ID_RE = re.compile(r"\b[A-Z]{2,4}-[A-Z0-9]+-\d+\b", re.IGNORECASE)
_LEAVE_TYPE_RE = re.compile(r"\b(Annual Leave|Sick Leave|Personal Leave)\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
# "to <name>" — captures the next non-space token after "to" or "for".
_RECIPIENT_RE = re.compile(r"\b(?:to|for)\s+(\S+)", re.IGNORECASE)
# Sprint 4 S4.1 (UC-11): cubicle ID (C-027) + floor number.
_CUBICLE_ID_RE = re.compile(r"\bC-\d{3}\b", re.IGNORECASE)
_FLOOR_NUM_RE = re.compile(r"\bfloor\s+(\d+)\b", re.IGNORECASE)
# Sprint 5: "seat 27" / "seat C-27" / "cubicle 27" → normalised to "C-0NN" so
# the "HR picks a seat" step accepts the bare number the floor list shows.
_SEAT_NUM_RE = re.compile(r"\b(?:seat|cubicle)\s+(?:C-)?0*(\d{1,3})\b", re.IGNORECASE)
# Sprint 5: detect "pending" intent in a list-leaves message. Matches
# "pending", "need to approve", "to approve", "awaiting approval" phrasing.
_PENDING_INTENT_RE = re.compile(
    r"\b(pending|need\s+to\s+approve|to\s+approve|awaiting\s+approval)\b",
    re.IGNORECASE,
)


def _extract_inline_args(tool_id: str, message: str) -> dict:
    """Extract simple inline arguments from the user message.

    - ``hr.approve_leave``: pulls ``leave_id`` from a token like ``LV-004``.
    - ``it.issue_asset``: pulls ``asset_id`` (canonical ``MBP-14-001`` shape)
      and ``employee_id`` (the token following ``to`` or ``for``).

    Returns an empty dict when nothing parses; the dispatcher then surfaces
    ERR-AGENT-002 to the user instead of silently substituting defaults.
    """
    if tool_id == "hr.apply_leave":
        out: dict = {}
        m_type = _LEAVE_TYPE_RE.search(message)
        if m_type:
            out["leave_type"] = m_type.group(1).title()
            if out["leave_type"].lower() == "annual leave":
                out["leave_type"] = "Annual Leave"
            elif out["leave_type"].lower() == "sick leave":
                out["leave_type"] = "Sick Leave"
            elif out["leave_type"].lower() == "personal leave":
                out["leave_type"] = "Personal Leave"
        dates = _ISO_DATE_RE.findall(message)
        if len(dates) >= 1:
            out["start_date"] = dates[0]
        if len(dates) >= 2:
            out["end_date"] = dates[1]
        return out
    if tool_id == "hr.approve_leave":
        match = _LEAVE_ID_RE.search(message)
        if match:
            return {"leave_id": match.group(0).upper()}
    if tool_id == "it.issue_asset":
        out: dict = {}
        m_asset = _ASSET_ID_RE.search(message)
        if m_asset:
            out["asset_id"] = m_asset.group(0).upper()
        m_recip = _RECIPIENT_RE.search(message)
        if m_recip:
            out["employee_id"] = m_recip.group(1).rstrip(".,;:!?")
        return out
    if tool_id == "hr.cubicle_list_floor":
        m_floor = _FLOOR_NUM_RE.search(message)
        if m_floor:
            return {"floor": int(m_floor.group(1))}
        return {}
    if tool_id == "hr.cubicle_assign":
        out2: dict = {}
        m_cubicle = _CUBICLE_ID_RE.search(message)
        if m_cubicle:
            out2["cubicle_id"] = m_cubicle.group(0).upper()
        else:
            # Accept a bare number after "seat"/"cubicle"/"desk" (e.g. "seat 27")
            # and normalise to the canonical "C-0NN" form (1..100 only).
            m_seat = _SEAT_NUM_RE.search(message)
            if m_seat:
                n = int(m_seat.group(1))
                if 1 <= n <= 100:
                    out2["cubicle_id"] = f"C-{n:03d}"
        m_recip2 = _RECIPIENT_RE.search(message)
        if m_recip2:
            # Strip trailing punctuation; the remainder is the username.
            out2["employee_username"] = m_recip2.group(1).rstrip(".,;:!?")
        return out2
    if tool_id == "hr.read_all_leaves":
        # Extract optional status=Pending when the message signals a list of
        # items awaiting approval ("pending", "need to approve", etc.).
        if _PENDING_INTENT_RE.search(message):
            return {"status": "Pending"}
        return {}
    return {}


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

        For tools that need parameters (e.g. ``hr.approve_leave`` needs
        ``leave_id``), this method extracts simple inline patterns from the
        message and merges them with ``rule.args``.

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
                merged_args = dict(rule.args)
                merged_args.update(_extract_inline_args(rule.tool_id, user_message))
                result.append(
                    ToolCall(
                        agent_id=rule.agent_id,
                        tool_id=rule.tool_id,
                        args=merged_args,
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
