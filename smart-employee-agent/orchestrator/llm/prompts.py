"""Router/composer prompt builders, router-output parsing, outcome rendering,
and the sensitive-key strip. Stdlib-only.

Security (sprint-5.md §2.7): ``strip_sensitive`` is applied to every tool
result before it reaches the composer prompt (and the keyword-mode
``_render_result`` generic fallback) — IS subject UUIDs and tokens never leave
the trust boundary.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from orchestrator.llm.client import (
    ToolOutcome,
)

__all__ = [
    "router_bind_system",
    "composer_system",
    "render_outcomes",
    "strip_sensitive",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sensitive-key strip (sprint-5.md §2.7)
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS_EXACT = frozenset(
    {"sub", "user_sub", "issued_by", "reviewed_by_sub", "reviewer_sub", "act"}
)
_SENSITIVE_KEY_SUFFIXES = ("_sub",)
_SENSITIVE_KEY_SUBSTRINGS = ("token", "secret", "password")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    if k in _SENSITIVE_KEYS_EXACT:
        return True
    if any(k.endswith(sfx) for sfx in _SENSITIVE_KEY_SUFFIXES):
        return True
    if any(sub in k for sub in _SENSITIVE_KEY_SUBSTRINGS):
        return True
    return False


def strip_sensitive(value: Any) -> Any:
    """Recursively drop sensitive keys and UUID-shaped string values.

    - Keys named ``sub`` / ``*_sub`` / ``issued_by`` / ``*token*`` / ``*secret*``
      / ``*password*`` / ``act`` are removed entirely.
    - Any *string value* that looks like a UUID is dropped (in this domain the
      only UUID-shaped strings in tool results are IS ``sub``s — cubicle ids are
      ``C-027``, asset ids ``AST-12345``, leave-request ids ``LRxxx``). This
      catches ambiguous keys like ``employee_id`` that some tools use for a sub.
    Lists/dicts are walked recursively. Scalars pass through unchanged.
    """
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if _is_sensitive_key(str(k)):
                continue
            if isinstance(v, str) and _UUID_RE.match(v):
                continue
            out[k] = strip_sensitive(v)
        return out
    if isinstance(value, list):
        return [strip_sensitive(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Router prompt (bind_tools / function-calling mode)
# ---------------------------------------------------------------------------


def router_bind_system(*, today: str) -> str:
    """Concise system prompt for the bind_tools router.

    Tool schemas are injected by ChatOpenAI.bind_tools(), so no tool listing
    or JSON format instructions are needed here.
    """
    return "\n".join([
        "You are the routing layer of an internal HR/IT employee assistant.",
        "Select the tool(s) needed to fully satisfy the user's request and extract each tool's arguments.",
        "Rules:",
        f"- Dates must be ISO format YYYY-MM-DD. Today is {today}.",
        "- leave_type must be exactly one of: Annual Leave, Sick Leave, Personal Leave.",
        "- If the message maps to more than one tool, list them in the order they "
        "should run.",
        "- If the message doesn't map to any tool (chit-chat, off-topic, or a "
        "question you can't answer with a tool), return [].",
        "- CRITICAL leave routing: any message that expresses intent to submit, "
        "apply for, request, book, or take leave — even partially (e.g. only dates "
        "given, or only type given, or a follow-up like 'go ahead') — MUST use "
        "hr.apply_leave. NEVER use hr.read_policy when the user wants to create a "
        "leave request. hr.read_policy is ONLY for purely informational questions "
        "about what the leave policy is, with no intent to apply.",
        "Available tools:",
    ]
    for e in catalogue:
        args = ", ".join(e.args) if e.args else "(none)"
        lines.append(f'- agent_id="{e.agent_id}" tool_id="{e.tool_id}": {e.description}')
        lines.append(f"    args: {args}")
    return "\n".join(lines)


def _coerce_text(content: Any) -> str:
    """Coerce a langchain response ``content`` (str or list-of-blocks or other) to str."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Composer prompt + outcome rendering
# ---------------------------------------------------------------------------

_MAX_DATA_CHARS = 2000


def composer_system() -> str:
    """Build the composer system prompt."""
    return "\n".join(
        [
            "You are the reply layer of an internal HR/IT employee assistant.",
            "Given the user's message and the outcome of each tool that ran, write "
            "ONE short, friendly reply in plain text — no markdown, no HTML, no "
            "code fences, no headings. Address the user as \"you\"; first person "
            "for the assistant.",
            "Rules:",
            "- Mention the outcome of every tool listed. Don't drop any.",
            "- If a tool failed with error_id ERR-CIBA-005, say plainly that the "
            "user declined that action and it wasn't done.",
            "- If a tool failed with error_id ERR-AGENT-002, ask the user for the "
            "specific missing information (the reason text names it). Don't say "
            '"something went wrong".',
            "- For any other failure, give a brief, non-technical apology and (if "
            "useful) what the user could try.",
            "- NEVER quote raw error text, HTTP status codes, JSON, identifier "
            "strings, or internal/IdP wording from a failure reason — translate "
            'the problem into plain language ("I couldn\'t start the approval '
            'step just now — please try again"), not the verbatim message.',
            "- State only facts present in the tool outputs. Never invent request "
            "ids, balances, cubicle numbers, asset ids, or dates.",
            "- If a leave-policy result is present, list each leave type with its "
            "minimum notice period, and — if the result includes a 'to_apply' "
            "list — tell the user exactly what they need to provide to apply for "
            "leave (the leave type, the start and end dates, and an optional reason).",
            "- If a tool result has a 'leave_requests' array (the HR-admin list of "
            "leave requests), show EACH request on its own line — even if there's "
            "only one — with: the request id, the employee who applied (use the "
            "'employee' field — do NOT write \"you\" / \"for you\" unless that "
            "employee is literally the person asking), the leave type, the start "
            "and end dates, the number of days, and the status. Don't compress it "
            "into a single sentence. If the array is empty, say there are no "
            "matching leave requests.",
            "- If an approve or reject of a leave request succeeded, confirm the "
            "action you just took: \"You approved leave request <request_id> for "
            "<employee>.\" (or \"You rejected …\") — use the 'request_id' and "
            "'employee' fields from the result; include the rejection reason if "
            "one is present.",
            "- 'Cubicle', 'seat' and 'seating (arrangement)' all mean the same "
            "thing — mirror whichever word the user used.",
            "- Cubicle/seat allocation is a guided flow — keep it moving: after a "
            "per-floor cubicle vacancy summary, end by asking which floor they "
            "want to allocate on. After a floor's vacant-seat list, end by asking "
            "which seat (and for which employee). After a successful assignment, "
            "confirm '<cubicle_id> on floor <floor> is now assigned to "
            "<employee>.' If the message already named a floor or a seat, skip "
            "straight ahead.",
            "- Issuing an IT device is a guided flow too: if the admin asked to "
            "issue a device but only named a TYPE ('a laptop', 'a phone') so all "
            "you have is the available-device catalogue, list the matching "
            "devices (model + catalogue id + how many in stock) and end by "
            "asking which one (the catalogue id) to issue and to whom — do NOT "
            "claim anything has been issued yet, and never invent a catalogue "
            "id. Only after a specific id has been issued (a successful "
            "it.issue_asset result) say 'Issued <asset_id> to <employee>.' If "
            "the message already named a specific catalogue id and a recipient, "
            "skip straight to the issuance.",
            "- Onboarding a new hire (a seat AND a laptop/phone/monitor in one "
            "request) is just the cubicle flow and the device flow at the same "
            "time — handle them together in ONE reply: present the seat options "
            "and the device options side by side and ask for all the picks at "
            "once (which floor for the seat, which laptop/phone), or — if "
            "everything was named specifically — confirm all the assignments in "
            "one reply.",
            '- Use a bullet list ("- ") only when listing 3 or more items '
            "(EXCEPT the leave-requests list above, which is always one line per "
            "request); otherwise prose.",
            "- Keep it under about 6 sentences for simple cases.",
        ]
    )


def render_outcomes(outcomes: list[ToolOutcome]) -> str:
    """Render the tool outcomes as the composer prompt body.

    Each ``ToolOutcome.data`` is run through ``strip_sensitive`` first (no
    ``sub``s / tokens reach the prompt) and JSON-dumped (truncated past
    ``_MAX_DATA_CHARS``).
    """
    if not outcomes:
        return "(no tools ran)"
    lines: list[str] = []
    for o in outcomes:
        if o.ok:
            safe = strip_sensitive(o.data or {})
            dumped = json.dumps(safe, default=str, ensure_ascii=False)
            if len(dumped) > _MAX_DATA_CHARS:
                dumped = dumped[:_MAX_DATA_CHARS] + "… (truncated)"
            lines.append(f"Tool {o.tool_id} (success): {dumped}")
        else:
            lines.append(
                f"Tool {o.tool_id} (failed): error_id={o.error_id} reason={(o.reason or '')!r}"
            )
    return "\n".join(lines)
