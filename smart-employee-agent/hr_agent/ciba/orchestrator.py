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
from datetime import datetime, timezone
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
from common.auth.binding_messages import FRESH, render
from common.auth.ciba_client import CIBAClient
from common.auth.errors import CIBADeniedError, CIBAExpiredError, CIBATimeoutError

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
_TOOL_REGISTRY: dict[str, tuple[str, str, Callable[[dict], dict], str | None]] = {
    "hr.read_balance": (
        "View your leave balance",
        "get_leave_balance",
        lambda args: {"employee_id": args.get("employee_id")},
        None,
    ),
    "hr.read_history": (
        "View your leave history",
        "get_leave_history",
        lambda args: {"employee_id": args.get("employee_id")},
        None,
    ),
    "hr.approve_leave": (
        "Approve a leave request on your behalf",
        "approve_leave",
        lambda args: {"leave_id": args.get("leave_id", "LV-004")},
        "openid hr_approve_rest",
    ),
}


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

    # ── DispatchProtocol entry point ──────────────────────────────────────────

    async def __call__(
        self,
        *,
        tool: str,
        args: dict,
        user_sub: str,
        orchestrator_act_sub: str,
        request_id: str,
        pending_register: Callable[[A2APendingState], None],
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

        # ── 2. Render binding message (F-05) ──────────────────────────────────
        binding_msg = render(
            FRESH,
            agent_label=deps.agent_label,
            action=action_text,
            request_id=request_id,
        )

        # ── 3. Obtain actor-token ─────────────────────────────────────────────
        try:
            actor_token_obj = await deps.actor_token_provider.ensure_valid_token()
        except Exception as exc:
            logger.error(
                "hr_dispatcher_actor_token_error request_id=%s error=%s",
                request_id,
                exc,
            )
            return ErrorPayload(
                error_id="ERR-AGENT-INTERNAL",
                reason=f"Failed to obtain actor token: {exc}",
            )

        # ── 4. Initiate CIBA ──────────────────────────────────────────────────
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
                "hr_dispatcher_ciba_initiate_error request_id=%s error=%s",
                request_id,
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
                mcp_kwargs=kwargs_builder(args),
                request_id=request_id,
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
        )

    # ── Background task ───────────────────────────────────────────────────────

    async def _run_to_completion(
        self,
        *,
        state: A2APendingState,
        ciba_request: object,  # CIBARequest — avoids circular typing issues
        mcp_method: str,
        mcp_kwargs: dict,
        request_id: str,
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

            # ── b. Call MCP tool with token-B ─────────────────────────────────
            mcp_callable = getattr(deps.mcp_client, mcp_method)
            tool_result: dict = await mcp_callable(
                token_b=token_b,
                request_id=request_id,
                **mcp_kwargs,
            )

            # ── c. Write ResultPayload ────────────────────────────────────────
            state.result = ResultPayload(
                data=tool_result,
                token_jti=token_b.jti if hasattr(token_b, "jti") and token_b.jti else "",
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
                "hr_dispatcher_ciba_expired request_id=%s",
                request_id,
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
                "hr_dispatcher_ciba_timeout request_id=%s reason=%s",
                request_id,
                reason,
            )
            state.error = ErrorPayload(error_id="ERR-CIBA-010", reason=reason)

        # ── g. MCP HTTP error ─────────────────────────────────────────────────
        except httpx.HTTPStatusError as exc:
            logger.error(
                "hr_dispatcher_mcp_http_error request_id=%s status=%s",
                request_id,
                exc.response.status_code if exc.response is not None else "?",
            )
            state.error = ErrorPayload(
                error_id="ERR-MCP-005",
                reason=f"MCP HTTP {exc.response.status_code if exc.response is not None else 'unknown'}: {exc}",
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
