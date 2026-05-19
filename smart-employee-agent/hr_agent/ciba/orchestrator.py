"""HR-agent CIBA orchestrator — implements ``common.a2a.server.DispatchProtocol``.

This module is the dispatcher that ``common/a2a/server.py`` calls for every
``POST /a2a/message/send`` arriving at the hr_agent process.  It owns the
complete CIBA→OBO→MCP cycle for HR tools:

    1. Validate the requested tool against :data:`_TOOL_REGISTRY`.
    2. Render the consent binding-message (F-05).
    3. Obtain a fresh actor-token via :class:`ActorTokenProvider`.
    4. Initiate CIBA at IS (``POST /oauth2/ciba``).
    5. Return :class:`ConsentRequiredPayload` **immediately** (F-01 two-phase).
    6. Register an :class:`A2APendingState` via ``pending_register``.
    7. Schedule a background :class:`asyncio.Task` that polls for token-B,
       calls the MCP client, and writes the result/error into the pending state.
    8. Wire ``add_done_callback`` to null-out ``poll_task`` (F-10 rule 3).

F-10 compliance (asyncio.Task defensive rules):
    - :func:`_run_to_completion` catches only CIBA-typed, MCP-typed, and then
      a broad ``Exception`` safety net (never ``BaseException``).
    - ``asyncio.CancelledError`` (a ``BaseException``) is NEVER caught — it
      propagates naturally to the event loop.
    - The ``add_done_callback`` zeros out ``state.poll_task`` after completion.
    - ``state.completion.set()`` is called unconditionally in ``finally``.

Boundary rule (F-09):
    :class:`HRDispatcherDeps` and :class:`HRDispatcher` are regular classes /
    dataclasses — they hold :class:`asyncio.Task` indirectly and must NOT be
    Pydantic models.  All HTTP-boundary shapes remain in ``common/a2a/models.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from traceloop.sdk.decorators import atask  # type: ignore[import-not-found]

import jwt as _pyjwt  # mid-sprint fix: decode jti from token-B JWT (no sig-verify;
                     # IS just minted it and hr_server will verify on the MCP call)
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from common.a2a.models import (
    A2AMessageResponse,
    ConsentRequiredPayload,
    ErrorPayload,
    ResultPayload,
)
from common.a2a.server import A2APendingState
from common.auth.actor_token_provider import ActorTokenProvider
from common.auth.binding_messages import render, select_template
from common.auth.ciba_client import CIBAClient
from common.auth.errors import CIBADeniedError, CIBAExpiredError, CIBATimeoutError
from common.auth.models import OAuthToken

from ..mcp.client import HRMcpClient

logger = logging.getLogger(__name__)

__all__ = ["HRDispatcherDeps", "HRDispatcher"]


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

# Map tool name → (action_text, mcp_method_name, args_to_kwargs_fn, scope_override)
# ``args_to_kwargs_fn`` converts the raw ``args`` dict from the A2A request
# into the keyword arguments expected by the MCP client method.
# ``scope_override`` (when non-None) selects a different CIBA scope than the
# agent's env-default ``deps.ciba_scope``. Required for write-tier tools per
# scope-policy.md §3 rule 2.
# Args that the dispatcher MUST receive (sourced from the message body or
# extracted by orchestrator's keyword router). Tools not listed here accept
# empty args (defaults applied downstream — e.g. employee_id falls back to
# token.sub). Write-tier tools name what they need to fail clearly.
_REQUIRED_ARGS: dict[str, list[str]] = {
    "hr.approve_leave": ["leave_id"],
    "hr.reject_leave": ["leave_id", "reason"],
    # S5.1: a partial LLM call (e.g. "I want leave" with no dates) fails here,
    # before any CIBA round-trip, with ERR-AGENT-002 — the composer then asks
    # the user for the missing dates.
    "hr.apply_leave": ["leave_type", "start_date", "end_date"],
}


def _jti_of(token: object) -> str:
    """Return the ``jti`` claim of an OAuth/OBO token as a string, or "".

    OAuthToken (Sprint 1 raw) doesn't carry jti as a field; we decode the
    JWT payload without signature verification to extract it. hr_server
    will validate the token on the actual MCP call, so for the agent's
    own audit-log purpose (Sprint 3 IssuedTokenRecord, denylist index)
    skipping verification here is safe.
    """
    access_token = getattr(token, "access_token", None) or getattr(token, "raw", None)
    if not isinstance(access_token, str):
        # Already an OBOToken with explicit jti? Use it.
        explicit = getattr(token, "jti", None)
        return str(explicit) if explicit else ""
    try:
        payload = _pyjwt.decode(access_token, options={"verify_signature": False})
    except _pyjwt.PyJWTError:
        return ""
    raw_jti = payload.get("jti")
    return str(raw_jti) if raw_jti else ""


_TOOL_REGISTRY: dict[str, tuple[str, str, Callable[[dict], dict], str | None]] = {
    # hr_basic tier — company leave policy (not user-specific). Scope-override
    # to hr_basic_rest so the CIBA token-C carries exactly what the tool needs.
    "hr.read_policy": (
        "View the company leave policy",
        "get_leave_policy",
        lambda args: {},
        "openid hr_basic_rest",
    ),
    "hr.read_balance": (
        "View your leave balance",
        "get_leave_balance",
        lambda args: {},  # self-service: hr_server keys on token.sub (legacy employee_id arg dropped)
        None,
    ),
    "hr.read_history": (
        "View your leave history",
        "get_leave_history",
        lambda args: {},  # self-service: hr_server keys on token.sub
        None,
    ),
    # S5.1: UC-13 chat path. Self-service write → explicit hr_self_rest scope
    # override (the same self scope the read tools use, per scope-policy.md).
    "hr.apply_leave": (
        "Apply for leave on your behalf",
        "apply_leave",
        lambda args: {
            "leave_type": args.get("leave_type"),
            "start_date": args.get("start_date"),
            "end_date": args.get("end_date"),
            "reason": args.get("reason", ""),
        },
        "openid hr_self_rest",
    ),
    "hr.approve_leave": (
        "Approve a leave request on your behalf",
        "approve_leave",
        lambda args: {"leave_id": args.get("leave_id")},
        "openid hr_approve_rest",
    ),
    # ── Sprint 4 S4.4 reject (UC-15) ─────────────────────────────────────────
    "hr.reject_leave": (
        "Reject a leave request on your behalf",
        "reject_leave",
        lambda args: {
            "leave_id": args.get("leave_id"),
            "reason": args.get("reason", ""),
        },
        "openid hr_approve_rest",
    ),
    # ── Sprint 4 S4.1 cubicle tools (UC-11) ──────────────────────────────────
    # The cubicle/seat *reads* are admin-grade (whole-org view): the hr_server
    # MCP tools require ``hr_read_rest``, so the CIBA token-C MUST carry it —
    # the env default (``hr_self_rest``) is NOT enough (→ ERR-MCP-003). Employee
    # role lacks ``hr_read_rest`` → IS denies the CIBA consent → "no permission".
    "hr.cubicle_summary": (
        "View vacant cubicles by floor",
        "get_cubicle_summary",
        lambda args: {},
        "openid hr_read_rest",
    ),
    "hr.cubicle_list_floor": (
        "View vacant cubicles on floor",
        "get_vacant_cubicles_on_floor",
        lambda args: {"floor": int(args.get("floor", 1))},
        "openid hr_read_rest",
    ),
    "hr.cubicle_assign": (
        "Assign cubicle to employee",
        "assign_cubicle",
        lambda args: {
            "cubicle_id": args.get("cubicle_id"),
            "employee_username": args.get("employee_username"),
            "employee_email": args.get("employee_email", ""),
        },
        "openid hr_assets_write_rest",
    ),
    "hr.lookup_employee": (
        "Look up an employee",
        "lookup_employee",
        lambda args: {"username_or_email": args.get("name", "")},
        "openid hr_read_rest",  # hr_server lookup_employee requires hr_read_rest
    ),
    # ── Sprint 4 S4.2 cubicle self-service (UC-12 HR leg) ─────────────────────
    "hr.cubicle_lookup_self": (
        "View your cubicle assignment",
        "get_my_cubicle",
        lambda args: {},
        None,
    ),
    # ── Sprint 5 — HR Admin list all leave requests (hr.read_all_leaves) ──────
    # Scope: hr_approve_rest (same as approve/reject — whoever can approve is
    # who would ask). Employee role lacks this scope → IS denies CIBA consent →
    # ERR-CIBA-005 / ERR-MCP-003 path surfaces "no permission" copy.
    # No required args (both status and employee_name are optional filters).
    "hr.read_all_leaves": (
        "View all leave requests",
        "get_all_leaves",
        lambda args: {
            "status": args.get("status"),
            "employee_name": args.get("employee_name"),
        },
        "openid hr_approve_rest",
    ),
}


# ── action_text sanitisation (security audit F-08) ──────────────────────────
#
# ``action_text`` for hr_assets_write_rest carries user-typed substrings
# (cubicle_id from regex extraction; employee_username from chat). It flows
# through A2A → orchestrator → SSE → SPA where the SPA renders it with
# textContent so DOM injection is blocked. But the audit log writes
# ``action_text`` verbatim, so we restrict the charset and cap the length
# server-side before propagation.
import re as _re

_ACTION_TEXT_ALLOWED_RE = _re.compile(r"[A-Za-z0-9 .\-_'@,]")
_ACTION_TEXT_MAX_LEN = 256


def _sanitise_action_text(value: str) -> str:
    """Restrict *value* to the F-08 allowed charset and cap length.

    Allowed characters: ``[A-Za-z0-9 .-_'@,]``. Anything else is dropped.
    Length capped at 256 chars. The sanitised value is suitable for both the
    SSE wire payload and structured log lines.
    """
    if not value:
        return ""
    out = "".join(ch for ch in value if _ACTION_TEXT_ALLOWED_RE.match(ch))
    if len(out) > _ACTION_TEXT_MAX_LEN:
        out = out[:_ACTION_TEXT_MAX_LEN]
    return out


# ---------------------------------------------------------------------------
# Token cache (UC-06 / D2.5)
# ---------------------------------------------------------------------------

# Buffer used to decide whether a cached token is "fresh enough" to reuse vs
# "near enough to expiry that we should pre-emptively re-CIBA". Mirrors
# common.auth.models.OBOToken.is_expired's default and matches the buffer in
# _archive/agent.before-v3/agent_auth.py per UC-06 §Architecture note.
_TOKEN_EXPIRY_BUFFER = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class _CachedToken:
    """One cached OBO token entry per (user_sub, ciba_scope) per dispatcher.

    Attributes:
        token: The raw OAuth token returned by the CIBA poll.
        iat: Issuance time (UTC); used to populate the SPA's
            ``prior_consent_at`` so the Session Refresh widget can render
            "you approved this 47 min ago" (copy-deck §6).
        expires_at: Mirror of ``token.expires_at`` for explicit comparisons.
    """

    token: OAuthToken
    iat: datetime
    expires_at: datetime

    def is_near_expiry(self, *, now: datetime, buffer: timedelta = _TOKEN_EXPIRY_BUFFER) -> bool:
        """Return True if the token is within *buffer* of expiry."""
        return now >= self.expires_at - buffer


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------


@dataclass
class HRDispatcherDeps:
    """Dependencies wired in by ``hr_agent/main.py`` at startup.

    Attributes:
        ciba_client: CIBA HTTP client for ``/oauth2/ciba`` + ``/oauth2/token``.
        actor_token_provider: Cached I4 actor-token provider for this agent.
        mcp_client: HR-server MCP client used after token-B is obtained.
        oauth_client_id: HR Agent App's OAuth ``client_id`` (Basic-auth on CIBA).
        oauth_client_secret: Corresponding client secret.
        agent_id: HR agent UUID; used as ``agent_label`` fallback.
        agent_label: Human-readable display name for the Consent Widget.
        ciba_scope: Space-separated OAuth scopes to request on CIBA initiation.
        max_poll_seconds: Maximum seconds to poll ``/oauth2/token`` per request.
    """

    ciba_client: CIBAClient
    actor_token_provider: ActorTokenProvider
    mcp_client: HRMcpClient
    oauth_client_id: str
    oauth_client_secret: str
    agent_id: str
    agent_label: str = "HR Agent"
    ciba_scope: str = "openid hr.read"
    max_poll_seconds: float = 300.0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class HRDispatcher:
    """Implements ``common.a2a.server.DispatchProtocol`` for hr_agent.

    One instance is created at startup and injected into the A2A router via
    :class:`~common.a2a.server.A2ARouterConfig`.  It is stateless across
    requests (all per-request state lives in :class:`A2APendingState`).

    Usage::

        deps = HRDispatcherDeps(...)
        dispatcher = HRDispatcher(deps)
        # injected into build_a2a_router(A2ARouterConfig(dispatch=dispatcher, ...))
    """

    def __init__(self, deps: HRDispatcherDeps) -> None:
        self._deps = deps
        # UC-06 / D2.5: per-(user_sub, ciba_scope) OBO token cache.
        # On a cache hit with a non-near-expiry token we skip the entire CIBA
        # round-trip and call MCP directly. On a cache hit near expiry we run
        # CIBA again but mark it ``is_refresh=True`` and surface the previous
        # iat as ``prior_consent_at`` so the SPA can render the Session
        # Refresh widget variant.
        self._token_cache: dict[tuple[str, str], _CachedToken] = {}
        # 3A.2 FIX-19: secondary jti -> cache_key index for O(1) revoke_jti
        # lookup. Updated on cache write/pop alongside _token_cache.
        self._jti_to_cache_key: dict[str, tuple[str, str]] = {}
        # 3A.2: revocation state (denylist). Set by hr_agent/main.py at
        # startup via attach_revocation(). Optional in tests; None means
        # the denylist check is a no-op.
        # FIX-4 (mid-sprint review): proper Optional annotation, no ignore.
        from common.revocation import RevocationState as _RS  # local import to avoid cycle
        self._revocation: _RS | None = None

    # ── 3A.2: revocation hooks ────────────────────────────────────────────────

    def attach_revocation(self, state) -> None:
        """Wire a ``common.revocation.RevocationState`` into the dispatcher.

        Called once at startup from ``hr_agent/main.py``. After this, the
        cache lookup checks the denylist before serving a cached token,
        and ``revoke_jti`` becomes a meaningful operation.
        """
        self._revocation = state

    async def revoke_jti(self, jti: str, user_sub: str, exp: float, reason: str) -> None:
        """Drop the cached _CachedToken for *jti* (3A.2 fan-out receiver hook).

        The denylist add itself is performed by the shared
        ``/internal/events`` router; this callback runs AFTER that, and is
        responsible for the agent-side cache eviction so a future call
        for ``(user_sub, scope)`` does not serve the revoked token.

        Args:
            jti: jti of the OBO token being revoked.
            user_sub: user_sub from the event payload (used for log context).
            exp: token exp epoch seconds (already recorded by the denylist).
            reason: ``"user_signed_out"`` | ``"admin_terminated"``.
        """
        cache_key = self._jti_to_cache_key.pop(jti, None)
        if cache_key is not None:
            popped = self._token_cache.pop(cache_key, None)
            if popped is not None:
                logger.info(
                    "hr_dispatcher_revoke_jti | jti=%s user_sub=%s reason=%s cache_dropped=true",
                    jti[:8],
                    user_sub,
                    reason,
                )
                return
        logger.info(
            "hr_dispatcher_revoke_jti | jti=%s user_sub=%s reason=%s cache_dropped=false (no entry)",
            jti[:8],
            user_sub,
            reason,
        )

    def _denylist_contains(self, jti: str) -> bool:
        """Helper: check whether *jti* is on the receiver's denylist."""
        if self._revocation is None or not jti:
            return False
        return jti in self._revocation.revoked_jtis

    # ── DispatchProtocol entry point ──────────────────────────────────────────

    @atask(name="hr_agent.dispatch")
    async def __call__(
        self,
        *,
        tool: str,
        args: dict,
        user_sub: str,
        orchestrator_act_sub: str,
        request_id: str,
        pending_register: Callable[[A2APendingState], None],
        last_logout_reason: str | None = None,
    ) -> A2AMessageResponse:
        """Orchestrate the CIBA flow for one tool call and return immediately.

        Steps 1–8 of the F-01 two-phase protocol:

        1. Look up *tool* in :data:`_TOOL_REGISTRY`; return ``ErrorPayload`` on miss.
        2. Render ``binding_message`` via :func:`~common.auth.binding_messages.render`.
        3. Obtain actor-token via ``ActorTokenProvider.ensure_valid_token()``.
        4. Initiate CIBA at IS.
        5. Build :class:`A2APendingState` with a fresh ``cancel_event``.
        6. Schedule :meth:`_run_to_completion` as a background ``asyncio.Task``.
        7. Wire ``add_done_callback`` to null-out ``poll_task`` (F-10).
        8. Register state via ``pending_register``; return ``ConsentRequiredPayload``.

        Args:
            tool: MCP tool identifier from the A2A message params.
            args: Tool-specific arguments dict.
            user_sub: ``sub`` claim from the validated inbound token-A.
            orchestrator_act_sub: ``act.sub`` from token-A (the orchestrator UUID).
            request_id: X-Request-ID correlation string.
            pending_register: Inserts the :class:`A2APendingState` into the
                specialist's shared ``pending`` dict.

        Returns:
            :class:`ConsentRequiredPayload` on successful CIBA initiation, or
            :class:`ErrorPayload` on hard failure (tool not found, CIBA error,
            actor-token mint failure).
        """
        deps = self._deps

        # ── 1. Tool lookup ────────────────────────────────────────────────────
        registry_entry = _TOOL_REGISTRY.get(tool)
        if registry_entry is None:
            logger.warning(
                "hr_dispatcher_tool_not_found tool=%s request_id=%s", tool, request_id
            )
            return ErrorPayload(
                error_id="ERR-AGENT-001-tool-not-found",
                reason=f"Tool {tool!r} is not registered in the HR dispatcher",
            )

        action_text, mcp_method, kwargs_builder, tool_scope_override = registry_entry
        ciba_scope = tool_scope_override or deps.ciba_scope

        # ── 1b. Validate required args BEFORE wasting a CIBA round-trip ───────
        # Only write tools require args; read tools can default to token.sub.
        mcp_kwargs = kwargs_builder(args)
        missing = sorted(k for k in _REQUIRED_ARGS.get(tool, []) if not args.get(k))
        if missing:
            logger.warning(
                "hr_dispatcher_args_missing tool=%s missing=%s request_id=%s",
                tool,
                missing,
                request_id,
            )
            return ErrorPayload(
                error_id="ERR-AGENT-002",
                reason=f"Missing required arguments for {tool}: {missing}",
            )

        # ── 1c. Token cache lookup (UC-06 / D2.5) ─────────────────────────────
        # Three outcomes:
        #   - Hit + valid → call MCP directly, return ResultPayload synchronously
        #   - Hit + near expiry → re-CIBA, is_refresh=True, prior_consent_at=iat
        #   - Miss → fresh CIBA (is_refresh=False)
        cache_key = (user_sub, ciba_scope)
        now = datetime.now(tz=timezone.utc)
        cached = self._token_cache.get(cache_key)
        # Captured separately so we can still mark is_refresh=True even after
        # we drop a hit-but-MCP-rejected cache entry.
        prior_iat: datetime | None = cached.iat if cached is not None else None

        # 3A.2: denylist check before serving a cached token. If the cached
        # token's jti is on the denylist (e.g. fan-out from a logout cascade),
        # treat as a cache miss + drop the entry. The user will see a fresh
        # consent widget rather than a stale-token reuse.
        # Mid-sprint fix: jti has to be decoded; OAuthToken has no jti attr.
        cached_jti = _jti_of(cached.token) if cached is not None else ""
        if cached is not None and self._denylist_contains(cached_jti):
            logger.info(
                "hr_dispatcher_cache_denylist_hit | dropping cache jti=%s",
                cached_jti[:8] if cached_jti else "(missing)",
            )
            if cached_jti:
                self._jti_to_cache_key.pop(cached_jti, None)
            self._token_cache.pop(cache_key, None)
            cached = None

        if cached and not cached.is_near_expiry(now=now):
            logger.info(
                "hr_dispatcher_cache_hit tool=%s request_id=%s user_sub=%s "
                "exp_in_s=%d",
                tool,
                request_id,
                user_sub,
                int((cached.expires_at - now).total_seconds()),
            )
            try:
                tool_result = await getattr(deps.mcp_client, mcp_method)(
                    token_b=cached.token,
                    request_id=request_id,
                    **mcp_kwargs,
                )
            except httpx.HTTPStatusError as exc:
                # Cached token unexpectedly rejected (revoked, scope changed).
                # Drop it and fall through to fresh CIBA so the user sees a
                # consent widget instead of a silent error. ``prior_iat`` is
                # preserved so this still surfaces as a Session Refresh.
                logger.warning(
                    "hr_dispatcher_cache_hit_mcp_rejected | dropping cache + "
                    "falling back to re-CIBA tool=%s status=%s",
                    tool,
                    exc.response.status_code if exc.response is not None else "?",
                )
                self._token_cache.pop(cache_key, None)
            else:
                return ResultPayload(
                    data=tool_result,
                    token_jti=_jti_of(cached.token),
                    token_exp=int(cached.expires_at.timestamp()),
                    token_iat=int(cached.iat.timestamp()),
                )

        # is_refresh is True when there *was* a cache entry (now expired,
        # near-expiry, or freshly dropped). prior_consent_at lets the SPA
        # show "approved 47m ago".
        is_refresh = prior_iat is not None
        prior_consent_at = prior_iat

        # ── 2. Sprint 4 S4.1 action_text + binding_message ───────────────────
        #
        # For most tools the SPA derives action copy from SCOPE_ACTION_MAP and
        # ``action_text`` stays None. For hr_assets_write_rest the dispatcher
        # constructs a parameterised action_text from the resolved tool args
        # (cubicle_id, employee_username) so the consent widget can render
        # "Assign cubicle C-027 to jane.doe" verbatim. The string is
        # sanitised against the F-08 charset whitelist + 256-char cap.
        sprint4_action_text: str | None = None
        custom_binding_msg: str | None = None
        if tool == "hr.cubicle_assign":
            cubicle_id = (args.get("cubicle_id") or "").strip()
            employee_username = (args.get("employee_username") or "").strip()
            sprint4_action_text = _sanitise_action_text(
                f"Assign cubicle {cubicle_id} to {employee_username}"
            )
            # UC-11 spec binding-message verbatim: includes the corr-id so the
            # consent screen lets the admin tie the request back to the chat
            # turn. Sanitised through the same whitelist for log hygiene.
            custom_binding_msg = _sanitise_action_text(
                f"{deps.agent_label} wants to assign cubicle {cubicle_id} "
                f"to {employee_username} corr-id {request_id}"
            )
        elif tool in ("hr.approve_leave", "hr.reject_leave"):
            # Sprint 4 S4.4 (UC-15): construct parameterised action_text
            # naming the employee + start_date so the consent widget reads
            # "Approve <username>'s leave from <start_date>". Look up the
            # leave request via hr_service.get_leave_request_details to
            # extract the employee name + dates. On lookup failure fall
            # back to the bare leave id; sanitised through F-08.
            verb = "Approve" if tool == "hr.approve_leave" else "Reject"
            leave_id = (args.get("leave_id") or "").strip()
            employee_label = ""
            start_date = ""
            try:
                # Lazy import — avoids importing hr_service in test paths
                # that stub the dispatcher's deps.
                from hr_server.service import hr_service as _hr_service
                details = await _hr_service.get_leave_request_details(leave_id)
            except Exception:  # noqa: BLE001
                details = None
            if details:
                employee_label = str(details.get("employee") or "")
                start_date = str(details.get("start_date") or "")
            if employee_label and start_date:
                sprint4_action_text = _sanitise_action_text(
                    f"{verb} {employee_label}'s leave from {start_date}"
                )
            else:
                sprint4_action_text = _sanitise_action_text(
                    f"{verb} leave request {leave_id}"
                )

        # ── 2b. Render binding message (F-05; 3B.2 FIX-17 reason-branched) ────
        if custom_binding_msg is not None:
            binding_msg = custom_binding_msg
        else:
            binding_msg = render(
                select_template(last_logout_reason, is_refresh=is_refresh),
                agent_label=deps.agent_label,
                action=action_text,
                request_id=request_id,
            )
        if last_logout_reason is not None:
            logger.info(
                "hr_dispatcher_binding_reason_applied request_id=%s reason=%s "
                "is_refresh=%s",
                request_id,
                last_logout_reason,
                is_refresh,
            )

        # ── 3. Obtain actor-token ─────────────────────────────────────────────
        try:
            actor_token_obj = await deps.actor_token_provider.ensure_valid_token()
        except Exception as exc:
            logger.error(
                "hr_dispatcher_actor_token_error request_id=%s exc_type=%s error=%r",
                request_id,
                type(exc).__name__,
                exc,
            )
            return ErrorPayload(
                error_id="ERR-AGENT-INTERNAL",
                reason=f"Failed to obtain actor token: {exc}",
            )

        # DEBUG: log actor-token len (not the token itself — redaction covers it
        # but we don't rely on that; length is sufficient to confirm non-empty).
        logger.debug(
            "hr_dispatcher_actor_token_ok request_id=%s actor_token_len=%d",
            request_id,
            len(actor_token_obj.access_token),
        )

        # ── 4. Initiate CIBA ──────────────────────────────────────────────────
        logger.debug(
            "hr_dispatcher_ciba_initiate request_id=%s scope=%r login_hint=%s",
            request_id,
            ciba_scope,
            user_sub,
        )

        try:
            ciba_request = await deps.ciba_client.initiate(
                oauth_client_id=deps.oauth_client_id,
                oauth_client_secret=deps.oauth_client_secret,
                login_hint=user_sub,
                binding_message=binding_msg,
                actor_token=actor_token_obj.access_token,
                scope=ciba_scope,
            )
        except Exception as exc:
            logger.error(
                "hr_dispatcher_ciba_initiate_error request_id=%s exc_type=%s error=%r",
                request_id,
                type(exc).__name__,
                exc,
            )
            return ErrorPayload(
                error_id="ERR-CIBA-001",
                reason=f"CIBA initiation failed: {exc}",
            )

        # ── 5. Build pending state ────────────────────────────────────────────
        cancel_event = asyncio.Event()
        state = A2APendingState(
            auth_req_id=ciba_request.auth_req_id,
            request_id=request_id,
            started_at=datetime.now(tz=timezone.utc),
            poll_task=None,
            completion=asyncio.Event(),
            cancel_event=cancel_event,
        )

        # ── 6 & 7. Schedule background task + add_done_callback (F-10) ───────
        poll_task = asyncio.create_task(
            self._run_to_completion(
                state=state,
                ciba_request=ciba_request,
                mcp_method=mcp_method,
                mcp_kwargs=mcp_kwargs,
                request_id=request_id,
                cache_key=cache_key,
            ),
            name=f"hr_poll_{ciba_request.auth_req_id[:8]}",
        )
        state.poll_task = poll_task

        def _on_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
            """F-10 rule 3: null-out poll_task after the task finishes."""
            state.poll_task = None

        poll_task.add_done_callback(_on_done)

        # ── 8. Register state + return ConsentRequiredPayload ─────────────────
        pending_register(state)

        logger.info(
            "hr_dispatcher_consent_required "
            "tool=%s auth_req_id=%s request_id=%s user_sub=%s",
            tool,
            ciba_request.auth_req_id,
            request_id,
            user_sub,
        )

        return ConsentRequiredPayload(
            auth_req_id=ciba_request.auth_req_id,
            auth_url=ciba_request.auth_url,
            agent_label=self._deps.agent_label,
            action=action_text,
            scope=ciba_scope,
            binding_message=binding_msg,
            expires_in=ciba_request.expires_in_s,
            is_refresh=is_refresh,
            prior_consent_at=prior_consent_at,
            action_text=sprint4_action_text,
        )

    # ── Background task ───────────────────────────────────────────────────────

    @atask(name="hr_agent.run_tool")
    async def _run_to_completion(
        self,
        *,
        state: A2APendingState,
        ciba_request: object,  # CIBARequest — avoids circular typing issues
        mcp_method: str,
        mcp_kwargs: dict,
        request_id: str,
        cache_key: tuple[str, str] | None = None,
    ) -> None:
        """Background task: poll for token-B, call MCP, write result into state.

        Exception handling matrix (F-10):
            - :class:`CIBADeniedError`   → ``ERR-CIBA-005``
            - :class:`CIBAExpiredError`  → ``ERR-CIBA-009``
            - :class:`CIBATimeoutError`  → ``ERR-CIBA-010``;
              ``reason="cancelled"`` when ``cancel_event`` was set, else
              ``reason="polling_timeout"``.
            - :class:`httpx.HTTPStatusError` from MCP → ``ERR-MCP-005``
            - Any other ``Exception``   → ``ERR-AGENT-INTERNAL``

        ``asyncio.CancelledError`` (a ``BaseException``) is **never** caught;
        it propagates naturally so the task is properly cancelled (F-10 rule 1).

        ``state.completion.set()`` is called unconditionally in ``finally`` so
        that ``/a2a/await`` never blocks forever (F-01).
        """
        deps = self._deps
        try:
            # ── a. Poll for token-B ───────────────────────────────────────────
            token_b = await deps.ciba_client.poll_for_token(
                ciba_request=ciba_request,  # type: ignore[arg-type]
                oauth_client_id=deps.oauth_client_id,
                oauth_client_secret=deps.oauth_client_secret,
                max_wait_seconds=deps.max_poll_seconds,
                cancel_event=state.cancel_event,
            )

            # DEBUG: token-B obtained — log aud/scope so MCP validator failures
            # can be correlated against what we handed to the resource server.
            logger.debug(
                "hr_dispatcher_token_b_obtained request_id=%s "
                "token_b_scope=%r token_b_aud=%r token_b_jti=%s",
                request_id,
                getattr(token_b, "scope", None),
                getattr(token_b, "aud", None),
                (getattr(token_b, "jti", "") or "")[:8],
            )

            # ── b. Call MCP tool with token-B ─────────────────────────────────
            logger.debug(
                "hr_dispatcher_mcp_call request_id=%s method=%s kwargs_keys=%s",
                request_id,
                mcp_method,
                list(mcp_kwargs.keys()),
            )

            mcp_callable = getattr(deps.mcp_client, mcp_method)
            tool_result: dict = await mcp_callable(
                token_b=token_b,
                request_id=request_id,
                **mcp_kwargs,
            )

            # ── b'. Cache the freshly issued token (UC-06 / D2.5) ─────────────
            # Only cache *after* the MCP call succeeded, so a token that the
            # resource server refuses (e.g. silent scope downgrade per F-18)
            # does not pollute the cache for the next request.
            if cache_key is not None and hasattr(token_b, "expires_at"):
                token_iat_dt = token_b.expires_at - timedelta(seconds=token_b.expires_in)
                self._token_cache[cache_key] = _CachedToken(
                    token=token_b,
                    iat=token_iat_dt,
                    expires_at=token_b.expires_at,
                )
                # 3A.2 FIX-19: maintain jti -> cache_key index for O(1) revoke.
                # Mid-sprint fix: OAuthToken has no jti attr; decode JWT instead.
                jti = _jti_of(token_b)
                if jti:
                    self._jti_to_cache_key[jti] = cache_key
                logger.info(
                    "hr_dispatcher_token_cached request_id=%s exp_in_s=%d jti=%s",
                    request_id,
                    token_b.expires_in,
                    jti[:8] if jti else "(missing)",
                )

            # ── c. Write ResultPayload ────────────────────────────────────────
            # Mid-sprint fix: jti has to be decoded from JWT (OAuthToken model
            # has no jti field). Empty jti here means Sprint 3 fan-out can't
            # target this token at receivers.
            state.result = ResultPayload(
                data=tool_result,
                token_jti=_jti_of(token_b),
                token_exp=int(token_b.expires_at.timestamp()) if hasattr(token_b, "expires_at") else 0,
                token_iat=int(token_b.expires_at.timestamp() - token_b.expires_in) if hasattr(token_b, "expires_at") else 0,
            )
            logger.info(
                "hr_dispatcher_result_ready request_id=%s method=%s",
                request_id,
                mcp_method,
            )

        # ── d. CIBA denied ────────────────────────────────────────────────────
        except CIBADeniedError as exc:
            logger.info(
                "hr_dispatcher_ciba_denied request_id=%s auth_req_id=%s",
                request_id,
                getattr(exc, "details", {}).get("auth_req_id", "?"),
            )
            state.error = ErrorPayload(error_id="ERR-CIBA-005", reason="user_denied")

        # ── e. CIBA expired ───────────────────────────────────────────────────
        except CIBAExpiredError as exc:
            logger.info(
                "hr_dispatcher_ciba_expired request_id=%s detail=%r",
                request_id,
                str(exc),
            )
            state.error = ErrorPayload(
                error_id="ERR-CIBA-009", reason="auth_req_id_expired"
            )

        # ── f. CIBA timeout (includes cancel) ─────────────────────────────────
        except CIBATimeoutError as exc:
            reason = (
                "cancelled" if state.cancel_event.is_set() else "polling_timeout"
            )
            logger.info(
                "hr_dispatcher_ciba_timeout request_id=%s reason=%s detail=%r",
                request_id,
                reason,
                str(exc),
            )
            state.error = ErrorPayload(error_id="ERR-CIBA-010", reason=reason)

        # ── g. MCP HTTP error ─────────────────────────────────────────────────
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            # Surface upstream error_id when MCP returns a structured 401
            # (ERR-MCP-001 aud, ERR-MCP-003 scope, etc.) so the orchestrator
            # can pick the right user-facing copy.
            upstream_id: str | None = None
            try:
                if exc.response is not None:
                    payload = exc.response.json()
                    detail = payload.get("detail", payload)
                    if isinstance(detail, dict):
                        upstream_id = detail.get("error_id")
            except Exception:  # noqa: BLE001
                upstream_id = None
            error_id = upstream_id or "ERR-MCP-005"
            logger.error(
                "hr_dispatcher_mcp_http_error request_id=%s status=%s upstream_id=%s reason=%r",
                request_id,
                status,
                upstream_id,
                str(exc),
            )
            state.error = ErrorPayload(
                error_id=error_id,
                reason=f"MCP HTTP {status}: {exc}",
            )

        # ── h. Unexpected exception ───────────────────────────────────────────
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "hr_dispatcher_unexpected_error request_id=%s error=%r",
                request_id,
                exc,
            )
            state.error = ErrorPayload(
                error_id="ERR-AGENT-INTERNAL", reason=str(exc)
            )

        # ── i. Always set completion (F-01) ───────────────────────────────────
        finally:
            state.completion.set()
