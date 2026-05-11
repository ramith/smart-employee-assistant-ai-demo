"""orchestrator/chat/routes.py — Chat and CIBA-cancel HTTP endpoints.

Implements two FastAPI routes:
    POST /api/chat      — kicks off serial fan-out; returns ChatAck immediately;
                          all downstream events delivered via SSE.
    POST /api/ciba/cancel — cancels a specific in-flight CIBA flow.

Two-phase A2A protocol (F-01):
    1. ``client.message_send()`` → ConsentRequiredPayload | ResultPayload | ErrorPayload
    2. If ConsentRequired: push CibaUrlEvent → push VERIFYING → ``client.await_completion()``
       → push DONE/DENIED/EXPIRED/ERROR → push ChatMessageEvent
    If Result on first call: push ChatMessageEvent immediately.

Serial fan-out (Q2):
    Tool calls returned by ``keyword_router.route()`` are processed one at a time,
    in order.  The second specialist only starts after the first has fully resolved
    (including user consent + MCP round-trip).

Final-answer composition (S1.4b / milestone-plan §3):
    Sprint 1 concatenates per-tool outputs with double-newline separators.
    LLM composition is deferred to Sprint 2.

Boundary rule (F-09):
    - ``ChatRouterDeps`` is a @dataclass (holds asyncio-typed deps indirectly).
    - ``ChatRequest``, ``ChatAck``, ``CibaCancelRequest``, ``CibaCancelResponse``
      are Pydantic v2 BaseModels (cross HTTP boundary).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from common.a2a.client import A2AClient, A2AError
from common.a2a.models import (
    ConsentRequiredPayload,
    ErrorPayload,
    ResultPayload,
)
from common.logging.correlation import get_request_id
from orchestrator.agent_registry.cards import AgentRegistry
from orchestrator.auth.session_store import (
    IssuedTokenRecord,
    PendingCIBA,
    Session,
    SessionStore,
)
from orchestrator.chat.keyword_fallback import KeywordRouter, ToolCall
from orchestrator.config import OrchestratorConfig
from orchestrator.events.sse import (
    ChatMessageEvent,
    CibaStateChangeEvent,
    CibaUrlEvent,
    RoutingEvent,
    SseChannel,
    SseErrorEvent,
)
# S5 — LLM routing + composition. These modules are stdlib-only (no langchain
# import); the concrete GeminiLLMClient is imported lazily by main.py only when
# LLM_FALLBACK_MODE=llm + a key is configured.
from orchestrator.llm.client import LLMClient, ToolOutcome
from orchestrator.llm.composer import compose_reply
from orchestrator.llm.router import resolve_tool_calls

__all__ = [
    "ChatRouterDeps",
    "ChatRequest",
    "ChatAck",
    "CibaCancelRequest",
    "CibaCancelResponse",
    "build_chat_router",
]

_logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Dependency container (dataclass — holds runtime objects)
# ---------------------------------------------------------------------------


@dataclass
class ChatRouterDeps:
    """Dependency bundle injected into the chat router factory.

    Attributes:
        config: Orchestrator service configuration.
        session_store: In-memory session store.
        keyword_router: Deterministic keyword-based message router (always wired —
            it is the fallback for LLM mode and the only router in keyword mode).
        agent_registry: Registry of loaded AgentCards.
        a2a_clients: Mapping of agent_id → A2AClient; wired up by Wave 8 main.py.
        llm_client: Optional Gemini-backed LLM client. Non-None only when
            ``config.llm_fallback_mode == "llm"`` and a key is configured;
            ``None`` ⇒ keyword-only behaviour (Sprint 1–4).
    """

    config: OrchestratorConfig
    session_store: SessionStore
    keyword_router: KeywordRouter
    agent_registry: AgentRegistry
    a2a_clients: dict[str, A2AClient]
    llm_client: LLMClient | None = None


# ---------------------------------------------------------------------------
# Pydantic models (HTTP boundary — F-09)
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Request body for POST /api/chat.

    Attributes:
        message: Raw user message text.
    """

    message: str


class ChatAck(BaseModel):
    """Immediate acknowledgment returned by POST /api/chat.

    The SPA should begin listening to the SSE stream for actual results.

    Attributes:
        ok: Always True on acknowledgment.
        request_id: Correlation ID for this request (echoes X-Request-ID).
    """

    ok: bool = True
    request_id: str


class CibaCancelRequest(BaseModel):
    """Request body for POST /api/ciba/cancel.

    Attributes:
        auth_req_id: IS-issued CIBA identifier to cancel.
    """

    auth_req_id: str


class CibaCancelResponse(BaseModel):
    """Response body for POST /api/ciba/cancel.

    Attributes:
        cancelled: True if a pending CIBA was found and cancelled.
        reason: Optional operator-facing note (e.g. "not_found").
    """

    cancelled: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# Error copy — friendly messages for the SPA (F-07 + error-catalog mappings)
# ---------------------------------------------------------------------------

# Maps ERR-CIBA-* codes to user-facing sentences shown in chat.
_ERROR_COPY: dict[str, str] = {
    "ERR-CIBA-001": "There was a problem initiating consent. Please try again.",
    "ERR-CIBA-002": "The agent app is not configured for CIBA. Contact admin.",
    "ERR-CIBA-003": "The notification channel is not enabled. Contact admin.",
    "ERR-CIBA-004": "Consent request was rejected by the identity provider.",
    "ERR-CIBA-005": "You declined the consent request.",
    "ERR-CIBA-006": "Consent was declined.",
    "ERR-CIBA-007": "Consent was denied.",
    "ERR-CIBA-008": "Consent was not granted.",
    "ERR-CIBA-009": "You took too long to approve. Please ask again.",
    "ERR-MCP-001": "The agent could not access the requested data. Contact admin.",
    "ERR-MCP-002": "Token was rejected by the resource server. Contact admin.",
    "ERR-MCP-003": "You don't have permission to perform this action. Your administrator can grant the required role.",
    "ERR-MCP-005": "The agent could not complete the request. Try again or contact admin.",
    "ERR-AGENT-001": "The agent service is temporarily unavailable. Try again in a moment.",
    "ERR-AGENT-002": "I couldn't tell which item you meant. Try formats like 'approve LV-004' or 'issue MBP-14-001 to alice'.",
}

_DEFAULT_ERROR_COPY = "An error occurred while processing your request. Please try again."

# Error ids that represent the user (or IS-on-behalf-of-the-user) declining
# the CIBA consent. These get agent-aware copy per UC-04 EX-1/EX-3 acceptance.
_DENIED_ERROR_IDS: frozenset[str] = frozenset({
    "ERR-CIBA-005", "ERR-CIBA-006", "ERR-CIBA-007", "ERR-CIBA-008",
})

# Error id that represents the auth_req_id timing out before user approved.
_EXPIRED_ERROR_ID = "ERR-CIBA-009"


def _friendly_error(error_id: str, reason: str, agent_label: str | None = None) -> str:
    """Return a user-facing sentence for an error_id.

    For consent-denied and consent-expired cases the copy is agent-aware so
    the user sees *which* specialist could not run (UC-04 EX-1/EX-3): e.g.
    "I couldn't access HR Agent (you declined the authorization). Ask again
    if you'd like to retry." This makes the multi-agent partial-result reply
    read naturally when one agent is approved and another is declined.

    Other error families fall back to the static ``_ERROR_COPY`` map; these
    are not user-controllable so they read fine without an agent name.

    Args:
        error_id: Machine-readable error code from ERR-CIBA-*, ERR-MCP-*, ERR-AGENT-*.
        reason: Technical description from the specialist (NOT shown to the user).
        agent_label: Display name of the specialist (e.g. "HR Agent"). When
            None, copy falls back to the agent-agnostic default.

    Returns:
        A one-sentence user-facing string suitable for the chat view.
    """
    _ = reason  # intentionally unused — reason is for ops logs only
    if agent_label:
        if error_id in _DENIED_ERROR_IDS:
            return (
                f"I couldn't access {agent_label} (you declined the authorization). "
                "Ask again if you'd like to retry."
            )
        if error_id == _EXPIRED_ERROR_ID:
            return (
                f"{agent_label} approval timed out. "
                "Ask again if you'd like to retry."
            )
    return _ERROR_COPY.get(error_id, _DEFAULT_ERROR_COPY)


def _state_from_error_id(error_id: str) -> str:
    """Map an error_id to the appropriate CibaStateChangeEvent state.

    Args:
        error_id: Machine-readable error code.

    Returns:
        One of "DENIED", "EXPIRED", or "ERROR".
    """
    if error_id in ("ERR-CIBA-005", "ERR-CIBA-006", "ERR-CIBA-007", "ERR-CIBA-008"):
        return "DENIED"
    if error_id == "ERR-CIBA-009":
        return "EXPIRED"
    return "ERROR"


# ---------------------------------------------------------------------------
# Serial fan-out coroutine
# ---------------------------------------------------------------------------


async def _run_serial_fan_out(
    session: Session,
    tool_calls: list[ToolCall],
    request_id: str,
    deps: ChatRouterDeps,
    *,
    user_message: str = "",
) -> None:
    """Execute tool calls one by one and push SSE events for each outcome.

    This coroutine is spawned as a background asyncio.Task by POST /api/chat.
    It never raises — all exceptions are caught and surfaced as SSE error events
    so the SPA always receives a final chat_message.

    Serial discipline (Q2): the second specialist starts only after the first
    has fully resolved (consent approved + MCP call complete, OR error).

    Final answer: in keyword mode the per-tool text fragments are concatenated
    with double-newline separators; in ``LLM_FALLBACK_MODE=llm`` the structured
    per-tool outcomes are handed to the LLM composer (with the concatenation as
    the fallback). Either way exactly one ``ChatMessageEvent`` is published.

    Args:
        session: The caller's authenticated session.
        tool_calls: Ordered list of ToolCall objects (LLM router or keyword router).
        request_id: Correlation ID for this request.
        deps: Router dependency bundle.
        user_message: The raw user message (fed to the LLM composer; ignored in
            keyword mode and by non-chat callers, e.g. the reports reject flow).
    """
    channel = SseChannel(session.sse_queue)
    per_tool_outputs: list[str] = []
    # S5: structured per-tool outcomes for the LLM composer (parallel to the
    # text fragments, which remain the composer's fallback).
    outcomes: list[ToolOutcome] = []
    total_tools = len(tool_calls)

    def _record(
        agent_id: str,
        tool_id: str,
        *,
        fragment: str,
        ok: bool,
        data: dict | None = None,
        error_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        per_tool_outputs.append(fragment)
        outcomes.append(
            ToolOutcome(
                agent_id=agent_id,
                tool_id=tool_id,
                ok=ok,
                data=data,
                error_id=error_id,
                reason=reason,
            )
        )

    for tool_index, tool_call in enumerate(tool_calls):
        agent_id = tool_call.agent_id

        # DEBUG: one line per tool iteration showing what the router decided
        # (LLM router in llm-mode, keyword router otherwise / on fallback).
        _logger.debug(
            "chat_fan_out | tool_iteration request_id=%s index=%d/%d "
            "tool_id=%s agent_id=%s args_keys=%s",
            request_id,
            tool_index + 1,
            total_tools,
            tool_call.tool_id,
            agent_id,
            list(tool_call.args.keys()),
        )

        # --- Resolve agent card ---
        card = deps.agent_registry.get(agent_id)
        if card is None:
            _logger.warning(
                "chat_fan_out | agent_not_in_registry agent_id=%s request_id=%s",
                agent_id,
                request_id,
            )
            await channel.publish(
                SseErrorEvent(
                    error_id="ERR-AGENT-002",
                    message=f"Agent '{agent_id}' is not registered.",
                    request_id=request_id,
                )
            )
            _record(
                agent_id,
                tool_call.tool_id,
                fragment=f"I could not reach the agent '{agent_id}' — it is not registered.",
                ok=False,
                error_id="ERR-AGENT-002",
                reason="agent_not_registered",
            )
            continue

        agent_label: str = card.label

        # --- Emit routing event ---
        await channel.publish(
            RoutingEvent(
                request_id=request_id,
                agent_id=agent_id,
                agent_label=agent_label,
                tool_index=tool_index,
                total_tools=total_tools,
            )
        )

        # --- Resolve A2A client ---
        client = deps.a2a_clients.get(agent_id)
        if client is None:
            _logger.error(
                "chat_fan_out | no_a2a_client agent_id=%s request_id=%s",
                agent_id,
                request_id,
            )
            await channel.publish(
                SseErrorEvent(
                    error_id="ERR-AGENT-002",
                    message=f"No A2A client configured for agent '{agent_id}'.",
                    request_id=request_id,
                )
            )
            _record(
                agent_id,
                tool_call.tool_id,
                fragment=f"I could not connect to the agent '{agent_id}'.",
                ok=False,
                error_id="ERR-AGENT-002",
                reason="no_a2a_client",
            )
            continue

        # --- Phase 1: message/send ---
        # 3B.2 FIX-17: forward last_logout_reason on the FIRST agent call
        # only, then null it on the Session. The reason is a one-shot
        # signal: once the user's first re-CIBA after a logout has
        # rendered its reason-aware binding message, subsequent CIBAs
        # in the same session should fall back to FRESH/REFRESH.
        last_logout_reason = session.last_logout_reason
        if last_logout_reason is not None:
            session.last_logout_reason = None
            _logger.info(
                "chat_fan_out | propagating_logout_reason rid=%s reason=%s agent_id=%s",
                request_id,
                last_logout_reason,
                agent_id,
            )

        first: Any
        try:
            first = await client.message_send(
                session.token_a.access_token,
                tool_call.tool_id,
                tool_call.args,
                request_id=request_id,
                last_logout_reason=last_logout_reason,
            )
        except (A2AError, Exception) as exc:  # noqa: BLE001
            _logger.error(
                "chat_fan_out | message_send_failed agent_id=%s request_id=%s error=%r",
                agent_id,
                request_id,
                exc,
            )
            fragment = f"I could not reach {agent_label}. Please try again in a moment."
            _record(
                agent_id, tool_call.tool_id, fragment=fragment, ok=False,
                error_id="ERR-A2A-SEND", reason=str(exc)[:200],
            )
            continue

        if isinstance(first, ResultPayload):
            # Tool ran synchronously — no consent needed.
            fragment = _render_result(agent_label, tool_call.tool_id, first)
            _record(agent_id, tool_call.tool_id, fragment=fragment, ok=True, data=first.data)
            continue

        if isinstance(first, ErrorPayload):
            _logger.warning(
                "chat_fan_out | message_send_error agent_id=%s error_id=%s reason=%s",
                agent_id,
                first.error_id,
                first.reason,
            )
            fragment = _friendly_error(first.error_id, first.reason, agent_label)
            _record(
                agent_id, tool_call.tool_id, fragment=fragment, ok=False,
                error_id=first.error_id, reason=first.reason,
            )
            continue

        # ConsentRequiredPayload — enter the two-phase CIBA path.
        if not isinstance(first, ConsentRequiredPayload):
            # Unexpected payload type — defensive guard.
            _record(
                agent_id, tool_call.tool_id,
                fragment=f"Received an unexpected response from {agent_label}.",
                ok=False, error_id="ERR-AGENT-UNEXPECTED", reason="unexpected payload type",
            )
            continue

        consent: ConsentRequiredPayload = first

        # 6a. Push CibaUrlEvent.
        await channel.publish(
            CibaUrlEvent(
                request_id=request_id,
                agent_id=agent_id,
                agent_label=agent_label,
                action=consent.action,
                auth_url=consent.auth_url,
                binding_code=request_id[:8],
                expires_in=consent.expires_in,
                scope=consent.scope,
                is_refresh=consent.is_refresh,
                prior_consent_at=consent.prior_consent_at,
                # 3B.2 FIX-17: forward the dispatcher's reason-aware
                # binding_message so the SPA can surface it directly
                # (WSO2 IS doesn't reliably show it on its consent UI).
                binding_message=consent.binding_message,
                # Sprint 4 S4.1: forward the server-rendered action_text
                # for parameterised admin-action copy.
                action_text=consent.action_text,
            )
        )

        # 6b. Register PendingCIBA in the session.
        pending = PendingCIBA(
            auth_req_id=consent.auth_req_id,
            agent_id=agent_id,
            request_id=request_id,
            started_at=_utc_now(),
        )
        session.pending_ciba[consent.auth_req_id] = pending

        # 6c. (intentionally no eager VERIFYING publish — that race-replaced the
        # AWAITING_APPROVAL UI before the user could click the auth_url. The
        # widget stays in AWAITING_APPROVAL until await_completion resolves.)

        # 6d. Phase 2: await_completion (long-poll until user approves or flow expires).
        # BLOCK-A (mid-sprint review): set pending.cancelled_ack in finally so a
        # concurrent UC-09 logout cascade's cancel barrier (logout_handler.py
        # _cancel_pending_ciba) can confirm the poll task has exited and will
        # not mint a new token-B between cancel_event.set() and fan-out.
        second: Any
        try:
            try:
                second = await client.await_completion(
                    session.token_a.access_token,
                    consent.auth_req_id,
                    request_id=request_id,
                )
            except (A2AError, Exception) as exc:  # noqa: BLE001
                _logger.error(
                    "chat_fan_out | await_completion_failed agent_id=%s auth_req_id=%s error=%r",
                    agent_id,
                    consent.auth_req_id,
                    exc,
                )
                await channel.publish(
                    CibaStateChangeEvent(
                        request_id=request_id,
                        state="ERROR",
                        message=str(exc),
                    )
                )
                fragment = f"There was a problem completing consent for {agent_label}. Please try again."
                _record(
                    agent_id, tool_call.tool_id, fragment=fragment, ok=False,
                    error_id="ERR-CIBA-COMPLETE", reason=str(exc)[:200],
                )
                # Clean up pending entry.
                session.pending_ciba.pop(consent.auth_req_id, None)
                # BLOCK-A: signal the cancel barrier even on error.
                pending.cancelled_ack.set()
                continue
        finally:
            # BLOCK-A: ack regardless of branch — the poll has exited so any
            # concurrent logout cascade can stop waiting.
            pending.cancelled_ack.set()

        # 6e. Push terminal CIBA state.
        if isinstance(second, ResultPayload):
            await channel.publish(
                CibaStateChangeEvent(
                    request_id=request_id,
                    state="DONE",
                )
            )
            # 6f. Record the issued token in the session log (S1.11a Sprint 3 hook).
            session.completed_ciba_log.append(
                IssuedTokenRecord(
                    session_id=session.session_id,
                    agent_id=agent_id,
                    jti=second.token_jti,
                    exp=second.token_exp,
                    iat=second.token_iat,
                    auth_req_id=consent.auth_req_id,
                )
            )
            fragment = _render_result(agent_label, tool_call.tool_id, second)
            _record(agent_id, tool_call.tool_id, fragment=fragment, ok=True, data=second.data)

        elif isinstance(second, ErrorPayload):
            terminal_state = _state_from_error_id(second.error_id)
            await channel.publish(
                CibaStateChangeEvent(
                    request_id=request_id,
                    state=terminal_state,  # type: ignore[arg-type]
                )
            )
            # 6g. Graceful degradation — continue to next tool.
            _logger.warning(
                "chat_fan_out | await_completion_error agent_id=%s error_id=%s reason=%s",
                agent_id,
                second.error_id,
                second.reason,
            )
            fragment = _friendly_error(second.error_id, second.reason, agent_label)
            _record(
                agent_id, tool_call.tool_id, fragment=fragment, ok=False,
                error_id=second.error_id, reason=second.reason,
            )

        else:
            # Should not happen — await_completion only returns Result or Error.
            _record(
                agent_id, tool_call.tool_id,
                fragment=f"Received an unexpected response from {agent_label}.",
                ok=False, error_id="ERR-AGENT-UNEXPECTED", reason="unexpected payload type",
            )

        # Clean up PendingCIBA entry once resolved.
        session.pending_ciba.pop(consent.auth_req_id, None)

    # --- Compose final answer ---
    # Keyword-mode fallback = the per-tool fragments concatenated. In
    # LLM_FALLBACK_MODE=llm this is replaced by an LLM-composed reply (with
    # this string as the composer's own fallback). The whole compose + publish
    # is wrapped so a terminal ChatMessageEvent is *structurally* guaranteed —
    # even if compose_reply / publish raises, the SPA still gets a reply.
    if per_tool_outputs:
        fallback_text = "\n\n".join(per_tool_outputs)
    else:
        fallback_text = "I was unable to retrieve any results. Please try again."

    try:
        final_content = await compose_reply(user_message, outcomes, fallback_text, deps)
        await channel.publish(ChatMessageEvent(content=final_content, request_id=request_id))
    except Exception as exc:  # noqa: BLE001 — never let the chat hang on a terminal failure
        _logger.error(
            "chat_fan_out | terminal_compose_or_publish_failed request_id=%s error=%r",
            request_id, exc,
        )
        try:
            await channel.publish(ChatMessageEvent(content=fallback_text, request_id=request_id))
        except Exception:  # noqa: BLE001 — best-effort; nothing more we can do
            pass

    _logger.info(
        "chat_fan_out | done request_id=%s tools=%d",
        request_id,
        len(tool_calls),
    )


def _render_result(agent_label: str, tool_id: str, result: ResultPayload) -> str:
    """Produce a human-readable fragment from a ResultPayload.

    Per-tool formatting switch keyed on tool_id; this is the keyword-mode reply
    (and the LLM composer's fallback). ``result.data`` is run through
    ``strip_sensitive`` first so an IS ``sub`` / token can never reach the chat
    reply (sprint-5.md §2.7).

    Args:
        agent_label: Display name of the specialist (e.g. "HR Agent").
        tool_id: Tool identifier (e.g. "hr.read_balance").
        result: The successful ResultPayload from the specialist.

    Returns:
        A plain-text string suitable for inclusion in the final chat message.
        The SPA renders it via ``textContent``, so no HTML is used here.
    """
    from orchestrator.llm.prompts import strip_sensitive

    data = strip_sensitive(result.data) if isinstance(result.data, dict) else (result.data or {})

    if tool_id == "hr.read_balance":
        balance = data.get("balance")
        if isinstance(balance, dict):
            return (
                f"Your leave balance: {balance.get('annual', '?')} annual, "
                f"{balance.get('sick', '?')} sick, "
                f"{balance.get('personal', '?')} personal day(s) remaining."
            )
        # Legacy / canned shape fallback.
        days = data.get("leave_days", "?")
        leave_type = data.get("leave_type", "annual")
        as_of = data.get("as_of_date", "")
        date_clause = f" (as of {as_of})" if as_of else ""
        return f"You have {days} days of {leave_type} leave remaining{date_clause}."

    if tool_id == "hr.read_policy":
        types = data.get("leave_types") or []
        if not types:
            return "No leave policy is configured."
        lines = ["You can apply for these leave types:"]
        for t in types:
            if isinstance(t, dict):
                name = t.get("leave_type", "?")
                maxd = t.get("max_days_per_year", "?")
                notice = t.get("min_notice_days", 0)
                suffix = f", {notice} day(s) notice required" if notice else ""
                lines.append(f"  • {name} — up to {maxd} day(s) per year{suffix}")
            else:
                lines.append(f"  • {t}")
        lines.append(
            'To apply, ask the HR agent with a type and dates — e.g. '
            '"annual leave from 2026-06-10 to 2026-06-14".'
        )
        return "\n".join(lines)

    if tool_id == "hr.apply_leave":
        if data.get("success"):
            ref = data.get("request_id", "?")
            return f"Your leave request has been submitted (ref {ref}) and is pending approval."
        # Business rejection: surface the service's message verbatim.
        return data.get("message") or "Your leave request could not be submitted."

    if tool_id == "hr.cubicle_lookup_self":
        if data.get("assigned"):
            return (
                f"Your cubicle is {data.get('cubicle_id', '?')} "
                f"on floor {data.get('floor', '?')}."
            )
        return "You don't have a cubicle assigned yet."

    if tool_id == "hr.cubicle_summary":
        floors = sorted(
            (k for k in data.keys() if k.startswith("floor_")),
            key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
        )
        if not floors:
            return "No cubicle data available."
        parts = []
        for fk in floors:
            stats = data.get(fk, {})
            parts.append(
                f"Floor {fk.split('_')[1]}: {stats.get('vacant', '?')} vacant "
                f"of {stats.get('total', '?')}"
            )
        return "Vacant cubicles by floor — " + "; ".join(parts) + "."

    if tool_id == "hr.cubicle_list_floor":
        if data.get("error"):
            return data.get("message") or "That floor is not valid (use 1–4)."
        vacant = data.get("vacant", [])
        floor = data.get("floor", "?")
        if not vacant:
            return f"Floor {floor} has no vacant cubicles."
        return f"Floor {floor} has {len(vacant)} vacant cubicle(s): " + ", ".join(vacant) + "."

    if tool_id == "hr.cubicle_assign":
        if data.get("error") == "cubicle_already_occupied":
            holder = data.get("current_holder", {})
            return (
                f"That cubicle is already assigned to "
                f"{holder.get('username', 'someone else')}."
            )
        if data.get("error"):
            return data.get("message") or "Could not assign that cubicle."
        assigned_to = data.get("assigned_to", {})
        return (
            f"Assigned {data.get('cubicle_id', '?')} (floor {data.get('floor', '?')}) "
            f"to {assigned_to.get('username', '?')}."
        )

    if tool_id == "hr.read_history":
        entries = data.get("entries", [])
        if not entries:
            return "You have no leave history on record."
        lines = ["Your recent leave:"]
        for e in entries:
            if isinstance(e, dict):
                lines.append(
                    f"  • {e.get('start_date', '?')} to {e.get('end_date', '?')}"
                    f" — {e.get('days', '?')} day(s), {e.get('type', '?')}"
                    f" ({e.get('status', '?')})"
                )
            else:
                lines.append(f"  • {e}")
        return "\n".join(lines)

    if tool_id == "hr.approve_leave":
        leave_id = data.get("leave_id", "?")
        approved_at = data.get("approved_at", "")
        date_clause = f" on {approved_at[:10]}" if approved_at else ""
        return f"Leave request {leave_id} has been approved{date_clause}."

    if tool_id == "it.list_available_assets":
        assets = data.get("assets", [])
        if not assets:
            return "No equipment is currently available."
        lines = ["Available equipment:"]
        for a in assets:
            if isinstance(a, dict):
                lines.append(
                    f"  • {a.get('model', '?')} ({a.get('asset_id', '?')})"
                    f" — {a.get('type', '?')}, {a.get('available_count', '?')} available"
                )
            else:
                lines.append(f"  • {a}")
        return "\n".join(lines)

    if tool_id == "it.get_my_assets":
        assets = data.get("assets", [])
        if not assets:
            return "You have no assets assigned."
        lines = ["Your assigned equipment:"]
        for a in assets:
            if isinstance(a, dict):
                lines.append(
                    f"  • {a.get('model', '?')} ({a.get('asset_id', '?')})"
                    f" — {a.get('type', '?')}, {a.get('status', '?')}"
                )
            else:
                lines.append(f"  • {a}")
        return "\n".join(lines)

    if tool_id == "it.issue_asset":
        asset_id = data.get("asset_id", "?")
        # `employee_id` is dropped by strip_sensitive when it's a UUID sub;
        # prefer a display username if the tool provides one.
        recipient = data.get("employee_username") or data.get("employee_id") or "the requested employee"
        issued_at = data.get("issued_at", "")
        date_clause = f" on {issued_at[:10]}" if issued_at else ""
        return f"Asset {asset_id} issued to {recipient}{date_clause}."

    # Generic fallback for any future tool not yet in the switch above.
    # `data` is already strip_sensitive'd at the top of this function.
    pairs = ", ".join(f"{k}: {v}" for k, v in data.items())
    return f"{agent_label} returned: {pairs}."


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_chat_router(deps: ChatRouterDeps) -> APIRouter:
    """Construct the FastAPI router for /api/chat and /api/ciba/cancel.

    All route handlers close over the *deps* bundle; no global state is used.

    Args:
        deps: Pre-wired dependency bundle (session_store, keyword_router,
              agent_registry, a2a_clients, config).

    Returns:
        A FastAPI ``APIRouter`` with two routes:
            - ``POST /api/chat``
            - ``POST /api/ciba/cancel``
    """
    router = APIRouter()

    # ------------------------------------------------------------------
    # POST /api/chat
    # ------------------------------------------------------------------

    @router.post("/api/chat", response_model=ChatAck)
    async def post_chat(body: ChatRequest, request: Request) -> ChatAck:
        """Accept a user message, spawn async fan-out, return ChatAck immediately.

        Flow:
        1. Authenticate via ``orch_sid`` cookie → 401 on miss.
        2. Resolve request_id from contextvar or generate a fresh UUID4.
        3. Route the message via ``resolve_tool_calls`` (one LLM call in
           ``LLM_FALLBACK_MODE=llm``, capped at ``LLM_TIMEOUT_S`` seconds, then
           keyword-router fallback; pure keyword routing otherwise).
        4. If no tool calls: push "I don't know" ChatMessageEvent; return ChatAck.
        5. Otherwise: spawn ``_run_serial_fan_out`` as a background Task; return
           ChatAck immediately so the SPA is unblocked.

        Note: step 3 may add a Gemini round-trip before ChatAck is returned —
        the SPA shows a transient "Thinking…" affordance to cover it.

        Args:
            body: Parsed ``ChatRequest`` with the user's message.
            request: Starlette request (used to read the ``orch_sid`` cookie).

        Returns:
            ``ChatAck`` with ``ok=True`` and the ``request_id``.

        Raises:
            HTTPException(401): No valid session cookie.
        """
        session_id: str | None = request.cookies.get(
            deps.config.session_cookie_name
        )
        if not session_id:
            raise HTTPException(status_code=401, detail="Missing session cookie")

        try:
            session: Session = await deps.session_store.get_or_404(session_id)
        except KeyError:
            raise HTTPException(status_code=401, detail="Invalid or expired session")

        # 3A.1 BLOCK-G: reject in-flight requests once a logout cascade has
        # set Session.terminating. The cookie may still authenticate but the
        # session is being torn down — accepting the chat would race the
        # fan-out and could mint a token-B that survives the cascade.
        if session.terminating:
            raise HTTPException(status_code=401, detail="Session terminating")

        # Resolve request_id.
        request_id: str = get_request_id() or str(uuid.uuid4())

        _logger.info(
            "chat_request | session_id=%s request_id=%s message_len=%d",
            session_id,
            request_id,
            len(body.message),
        )

        # Routing — LLM-primary (with keyword fallback) or keyword-only.
        tool_calls: list[ToolCall] = await resolve_tool_calls(body.message, deps)

        if not tool_calls:
            _logger.info(
                "chat_request | no_route request_id=%s", request_id
            )
            channel = SseChannel(session.sse_queue)
            await channel.publish(
                ChatMessageEvent(
                    content="I don't know how to help with that.",
                    request_id=request_id,
                )
            )
            return ChatAck(request_id=request_id)

        # Spawn fan-out task; return ack immediately.
        asyncio.create_task(
            _run_serial_fan_out(session, tool_calls, request_id, deps, user_message=body.message),
            name=f"fan_out:{request_id}",
        )

        return ChatAck(request_id=request_id)

    # ------------------------------------------------------------------
    # POST /api/ciba/cancel
    # ------------------------------------------------------------------

    @router.post("/api/ciba/cancel", response_model=CibaCancelResponse)
    async def post_ciba_cancel(
        body: CibaCancelRequest, request: Request
    ) -> CibaCancelResponse:  # noqa: D401
        """Cancel a specific in-flight CIBA flow by auth_req_id.

        Flow:
        1. Authenticate via ``orch_sid`` cookie → 401 on miss.
        2. Look up ``auth_req_id`` in the session's ``pending_ciba`` dict.
        3. If found: call ``a2a_client.cancel()``, set the local cancel_event,
           return ``CibaCancelResponse(cancelled=True)``.
        4. If not found: return ``CibaCancelResponse(cancelled=False, reason="not_found")``.

        Args:
            body: Parsed ``CibaCancelRequest`` with the ``auth_req_id``.
            request: Starlette request (used to read the ``orch_sid`` cookie).

        Returns:
            ``CibaCancelResponse`` indicating whether cancellation succeeded.

        Raises:
            HTTPException(401): No valid session cookie.
        """
        session_id: str | None = request.cookies.get(
            deps.config.session_cookie_name
        )
        if not session_id:
            raise HTTPException(status_code=401, detail="Missing session cookie")

        try:
            session: Session = await deps.session_store.get_or_404(session_id)
        except KeyError:
            raise HTTPException(status_code=401, detail="Invalid or expired session")

        # FIX-1 (mid-sprint review): mirror the BLOCK-G fence on this entry
        # point so a UC-09 cascade in flight rejects new ciba/cancel calls
        # cleanly (otherwise we race the cascade's own cancel_event.set()).
        if session.terminating:
            raise HTTPException(status_code=401, detail="Session terminating")

        pending = session.pending_ciba.get(body.auth_req_id)
        if pending is None:
            _logger.info(
                "ciba_cancel | not_found auth_req_id=%s session_id=%s",
                body.auth_req_id,
                session_id,
            )
            return CibaCancelResponse(cancelled=False, reason="not_found")

        agent_id = pending.agent_id
        client = deps.a2a_clients.get(agent_id)
        cancelled_on_specialist = False
        if client is not None:
            try:
                resp = await client.cancel(
                    session.token_a.access_token,
                    body.auth_req_id,
                )
                cancelled_on_specialist = resp.cancelled
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "ciba_cancel | specialist_cancel_failed agent_id=%s auth_req_id=%s error=%r",
                    agent_id,
                    body.auth_req_id,
                    exc,
                )

        # Always fire the local cancel_event so the awaiting fan-out coroutine
        # can detect the cancellation.
        pending.cancel_event.set()
        pending.status = "cancelled"

        _logger.info(
            "ciba_cancel | done agent_id=%s auth_req_id=%s specialist_cancelled=%s",
            agent_id,
            body.auth_req_id,
            cancelled_on_specialist,
        )

        return CibaCancelResponse(cancelled=True)

    return router
