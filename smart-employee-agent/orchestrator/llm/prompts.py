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
    LLMError,
    RoutedToolCall,
    ToolCatalogueEntry,
    ToolOutcome,
)

__all__ = [
    "router_system",
    "parse_router_output",
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
# Router prompt + output parsing
# ---------------------------------------------------------------------------


def router_system(catalogue: list[ToolCatalogueEntry], *, today: str) -> str:
    """Build the router system prompt from the tool catalogue."""
    lines = [
        "You are the routing layer of an internal HR/IT employee assistant.",
        "Decide which tool(s) to call to fully satisfy the user's message, and "
        "extract each tool's arguments from the message.",
        "Respond with ONLY a JSON array — no prose, no markdown code fences.",
        'Each element: {"agent_id": "<exact agent_id>", "tool_id": "<exact tool_id>", "args": { ... }}',
        "Rules:",
        "- Use only the agent_id / tool_id strings listed below — never invent one.",
        "- Include only the argument names a tool accepts (listed below). Omit any "
        "argument you cannot determine from the message.",
        f"- Dates must be ISO format YYYY-MM-DD. Today is {today}.",
        "- leave_type must be exactly one of: Annual Leave, Sick Leave, Personal Leave.",
        "- If the message maps to more than one tool, list them in the order they "
        "should run.",
        "- If the message doesn't map to any tool (chit-chat, off-topic, or a "
        "question you can't answer with a tool), return [].",
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


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        # drop the first line (``` or ```json) and a trailing ```
        nl = s.find("\n")
        s = s[nl + 1 :] if nl != -1 else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def parse_router_output(content: Any) -> list[RoutedToolCall]:
    """Parse the router LLM's text into ``RoutedToolCall``s.

    An empty JSON array (``[]``) is valid and returns ``[]`` — the model
    legitimately found nothing. Anything that won't parse as a JSON array, or a
    non-empty array with zero well-formed items, raises ``LLMError`` (the caller
    then falls back to the keyword router).
    """
    text = _strip_code_fence(_coerce_text(content))
    if not text:
        raise LLMError("router returned empty content")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise LLMError(f"router output is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise LLMError(f"router output is not a JSON array (got {type(parsed).__name__})")
    out: list[RoutedToolCall] = []
    skipped = 0
    for item in parsed:
        if not isinstance(item, dict):
            skipped += 1
            continue
        agent_id = item.get("agent_id")
        tool_id = item.get("tool_id")
        if not isinstance(agent_id, str) or not agent_id:
            skipped += 1
            continue
        if not isinstance(tool_id, str) or not tool_id:
            skipped += 1
            continue
        args = item.get("args")
        out.append(RoutedToolCall(agent_id=agent_id, tool_id=tool_id, args=args if isinstance(args, dict) else {}))
    if not out and parsed:
        raise LLMError("router output had items but none were well-formed")
    if skipped:
        logger.debug("parse_router_output skipped %d malformed item(s)", skipped)
    return out


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
