"""IT-agent CIBA orchestrator — implements ``common.a2a.server.DispatchProtocol``.

Structural mirror of ``hr_agent/ciba/orchestrator.py`` with IT-specific tool
registry and MCP client.  All F-01 / F-05 / F-09 / F-10 rules are identical;
see the HR-agent module docstring for a full explanation.

F-10 compliance summary:
    - ``_run_to_completion`` catches CIBA-typed, MCP-typed, and generic
      ``Exception`` (never ``BaseException`` — ``asyncio.CancelledError`` always
      propagates).
    - ``add_done_callback`` nulls out ``state.poll_task`` on completion.
    - ``state.completion.set()`` is called unconditionally in ``finally``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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
from common.auth.binding_messages import FRESH, REFRESH, render
from common.auth.ciba_client import CIBAClient
from common.auth.errors import CIBADeniedError, CIBAExpiredError, CIBATimeoutError
from common.auth.models import OAuthToken

from ..mcp.client import ITMcpClient

logger = logging.getLogger(__name__)

__all__ = ["ITDispatcherDeps", "ITDispatcher"]


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

# Map tool name → (action_text, mcp_method_name, args_to_kwargs_fn, scope_override)
# ``scope_override`` (when non-None) selects a different CIBA scope than the
# agent's env-default ``deps.ciba_scope``. Required for write-tier tools per
# scope-policy.md §3 rule 2. ``it.issue_asset`` lands in Sprint 2A.2.
# Args that the dispatcher MUST receive. Tools not listed here accept empty
# args (read tools default to token.sub etc.). Write-tier tools name what they
# need to fail clearly with ERR-AGENT-002.
_REQUIRED_ARGS: dict[str, list[str]] = {
    "it.issue_asset": ["asset_id", "employee_id"],
}


_TOOL_REGISTRY: dict[str, tuple[str, str, Callable[[dict], dict], str | None]] = {
    "it.list_available_assets": (
        "List available IT assets",
        "list_available_assets",
        lambda args: {"asset_type": args.get("asset_type")},
        None,
    ),
    "it.get_my_assets": (
        "View your assigned IT assets",
        "get_my_assets",
        lambda args: {"employee_id": args.get("employee_id")},
        None,
    ),
    "it.issue_asset": (
        "Issue an IT asset to an employee",
        "issue_asset",
        lambda args: {
            "asset_id": args.get("asset_id"),
            "employee_id": args.get("employee_id"),
        },
        "openid it_assets_write_rest",
    ),
}


# ---------------------------------------------------------------------------
# Token cache (UC-06 / D2.5) — see hr_agent.ciba.orchestrator for full notes.
# ---------------------------------------------------------------------------

_TOKEN_EXPIRY_BUFFER = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class _CachedToken:
    """Per-(user_sub, ciba_scope) cached OBO token; mirrors hr_agent's helper."""

    token: OAuthToken
    iat: datetime
    expires_at: datetime

    def is_near_expiry(self, *, now: datetime, buffer: timedelta = _TOKEN_EXPIRY_BUFFER) -> bool:
        return now >= self.expires_at - buffer


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------


@dataclass
class ITDispatcherDeps:
    """Dependencies wired in by ``it_agent/main.py`` at startup.

    Attributes:
        ciba_client: CIBA HTTP client for ``/oauth2/ciba`` + ``/oauth2/token``.
        actor_token_provider: Cached I4 actor-token provider for this agent.
        mcp_client: IT-server MCP client used after token-B is obtained.
        oauth_client_id: IT Agent App's OAuth ``client_id`` (Basic-auth on CIBA).
        oauth_client_secret: Corresponding client secret.
        agent_id: IT agent UUID; used as ``agent_label`` fallback.
        agent_label: Human-readable display name for the Consent Widget.
        ciba_scope: Space-separated OAuth scopes to request on CIBA initiation.
        max_poll_seconds: Maximum seconds to poll ``/oauth2/token`` per request.
    """

    ciba_client: CIBAClient
    actor_token_provider: ActorTokenProvider
    mcp_client: ITMcpClient
    oauth_client_id: str
    oauth_client_secret: str
    agent_id: str
    agent_label: str = "IT Agent"
    ciba_scope: str = "openid it.read"
    max_poll_seconds: float = 300.0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class ITDispatcher:
    """Implements ``common.a2a.server.DispatchProtocol`` for it_agent.

    Structurally identical to :class:`~hr_agent.ciba.orchestrator.HRDispatcher`.
    The difference is in the tool registry (:data:`_TOOL_REGISTRY`), the MCP
    client type (:class:`ITMcpClient`), and the default scope / label.

    One instance is created at startup and injected into the A2A router.
    """

    def __init__(self, deps: ITDispatcherDeps) -> None:
        self._deps = deps
        # UC-06 / D2.5: per-(user_sub, ciba_scope) OBO token cache.
        self._token_cache: dict[tuple[str, str], _CachedToken] = {}
        # 3A.2 FIX-19: secondary jti -> cache_key index for O(1) revoke lookup.
        self._jti_to_cache_key: dict[str, tuple[str, str]] = {}
        # 3A.2: revocation state attached at startup by it_agent/main.py.
        # FIX-4 (mid-sprint review): proper Optional annotation.
        from common.revocation import RevocationState as _RS
        self._revocation: _RS | None = None

    # ── 3A.2: revocation hooks ────────────────────────────────────────────────

    def attach_revocation(self, state) -> None:
        """Wire ``common.revocation.RevocationState`` into the dispatcher."""
        self._revocation = state

    async def revoke_jti(self, jti: str, user_sub: str, exp: float, reason: str) -> None:
        """Drop the cached _CachedToken for *jti* (3A.2 fan-out receiver hook)."""
        cache_key = self._jti_to_cache_key.pop(jti, None)
        if cache_key is not None:
            popped = self._token_cache.pop(cache_key, None)
            if popped is not None:
                logger.info(
                    "it_dispatcher_revoke_jti | jti=%s user_sub=%s reason=%s cache_dropped=true",
                    jti[:8],
                    user_sub,
                    reason,
                )
                return
        logger.info(
            "it_dispatcher_revoke_jti | jti=%s user_sub=%s reason=%s cache_dropped=false (no entry)",
            jti[:8],
            user_sub,
            reason,
        )

    def _denylist_contains(self, jti: str) -> bool:
        if self._revocation is None or not jti:
            return False
        return jti in self._revocation.revoked_jtis

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
        """Orchestrate the CIBA flow for one IT tool call and return immediately.

        Identical flow to :meth:`~hr_agent.ciba.orchestrator.HRDispatcher.__call__`
        — see that docstring for full step-by-step description.

        Args:
            tool: MCP tool identifier, e.g. ``"it.list_available_assets"``.
            args: Tool-specific arguments dict.
            user_sub: ``sub`` claim from the validated inbound token-A.
            orchestrator_act_sub: ``act.sub`` from token-A.
            request_id: X-Request-ID correlation string.
            pending_register: Inserts the :class:`A2APendingState` into the
                specialist's shared ``pending`` dict.

        Returns:
            :class:`ConsentRequiredPayload` on successful CIBA initiation, or
            :class:`ErrorPayload` on hard failure.
        """
        deps = self._deps

        # ── 1. Tool lookup ────────────────────────────────────────────────────
        registry_entry = _TOOL_REGISTRY.get(tool)
        if registry_entry is None:
            logger.warning(
                "it_dispatcher_tool_not_found tool=%s request_id=%s", tool, request_id
            )
            return ErrorPayload(
                error_id="ERR-AGENT-001-tool-not-found",
                reason=f"Tool {tool!r} is not registered in the IT dispatcher",
            )

        action_text, mcp_method, kwargs_builder, tool_scope_override = registry_entry
        ciba_scope = tool_scope_override or deps.ciba_scope

        # ── 1b. Validate required args BEFORE wasting a CIBA round-trip ───────
        # Only write tools require args; read tools default downstream.
        mcp_kwargs = kwargs_builder(args)
        missing = sorted(k for k in _REQUIRED_ARGS.get(tool, []) if not args.get(k))
        if missing:
            logger.warning(
                "it_dispatcher_args_missing tool=%s missing=%s request_id=%s",
                tool,
                missing,
                request_id,
            )
            return ErrorPayload(
                error_id="ERR-AGENT-002",
                reason=f"Missing required arguments for {tool}: {missing}",
            )

        # ── 1c. Token cache lookup (UC-06 / D2.5) ─────────────────────────────
        cache_key = (user_sub, ciba_scope)
        now = datetime.now(tz=timezone.utc)
        cached = self._token_cache.get(cache_key)
        prior_iat: datetime | None = cached.iat if cached is not None else None

        # 3A.2: denylist check before serving a cached token.
        if cached is not None and self._denylist_contains(getattr(cached.token, "jti", "")):
            logger.info(
                "it_dispatcher_cache_denylist_hit | dropping cache jti=%s",
                (getattr(cached.token, "jti", "") or "")[:8],
            )
            self._jti_to_cache_key.pop(getattr(cached.token, "jti", ""), None)
            self._token_cache.pop(cache_key, None)
            cached = None

        if cached and not cached.is_near_expiry(now=now):
            logger.info(
                "it_dispatcher_cache_hit tool=%s request_id=%s user_sub=%s "
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
                logger.warning(
                    "it_dispatcher_cache_hit_mcp_rejected | dropping cache + "
                    "falling back to re-CIBA tool=%s status=%s",
                    tool,
                    exc.response.status_code if exc.response is not None else "?",
                )
                self._token_cache.pop(cache_key, None)
            else:
                return ResultPayload(
                    data=tool_result,
                    token_jti=getattr(cached.token, "jti", "") or "",
                    token_exp=int(cached.expires_at.timestamp()),
                    token_iat=int(cached.iat.timestamp()),
                )

        is_refresh = prior_iat is not None
        prior_consent_at = prior_iat

        # ── 2. Render binding message (F-05) ──────────────────────────────────
        binding_msg = render(
            REFRESH if is_refresh else FRESH,
            agent_label=deps.agent_label,
            action=action_text,
            request_id=request_id,
        )

        # ── 3. Obtain actor-token ─────────────────────────────────────────────
        try:
            actor_token_obj = await deps.actor_token_provider.ensure_valid_token()
        except Exception as exc:
            logger.error(
                "it_dispatcher_actor_token_error request_id=%s error=%s",
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
                "it_dispatcher_ciba_initiate_error request_id=%s error=%s",
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
                mcp_kwargs=mcp_kwargs,
                request_id=request_id,
                cache_key=cache_key,
            ),
            name=f"it_poll_{ciba_request.auth_req_id[:8]}",
        )
        state.poll_task = poll_task

        def _on_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
            """F-10 rule 3: null-out poll_task after the task finishes."""
            state.poll_task = None

        poll_task.add_done_callback(_on_done)

        # ── 8. Register state + return ConsentRequiredPayload ─────────────────
        pending_register(state)

        logger.info(
            "it_dispatcher_consent_required "
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
        cache_key: tuple[str, str] | None = None,
    ) -> None:
        """Background task: poll for token-B, call MCP, write result into state.

        Exception handling matrix (F-10):
            - :class:`CIBADeniedError`   → ``ERR-CIBA-005``
            - :class:`CIBAExpiredError`  → ``ERR-CIBA-009``
            - :class:`CIBATimeoutError`  → ``ERR-CIBA-010``;
              ``reason="cancelled"`` when ``cancel_event`` was set.
            - :class:`httpx.HTTPStatusError` from MCP → ``ERR-MCP-005``
            - Any other ``Exception``   → ``ERR-AGENT-INTERNAL``

        ``asyncio.CancelledError`` (``BaseException``) is **never** caught;
        it propagates so the task is properly cancelled (F-10 rule 1).
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

            # ── b'. Cache the freshly issued token (UC-06 / D2.5) ─────────────
            if cache_key is not None and hasattr(token_b, "expires_at"):
                token_iat_dt = token_b.expires_at - timedelta(seconds=token_b.expires_in)
                self._token_cache[cache_key] = _CachedToken(
                    token=token_b,
                    iat=token_iat_dt,
                    expires_at=token_b.expires_at,
                )
                # 3A.2 FIX-19: maintain jti -> cache_key index for O(1) revoke.
                jti = getattr(token_b, "jti", "")
                if jti:
                    self._jti_to_cache_key[jti] = cache_key
                logger.info(
                    "it_dispatcher_token_cached request_id=%s exp_in_s=%d",
                    request_id,
                    token_b.expires_in,
                )

            # ── c. Write ResultPayload ────────────────────────────────────────
            state.result = ResultPayload(
                data=tool_result,
                token_jti=token_b.jti if hasattr(token_b, "jti") and token_b.jti else "",
                token_exp=int(token_b.expires_at.timestamp()) if hasattr(token_b, "expires_at") else 0,
                token_iat=int(token_b.expires_at.timestamp() - token_b.expires_in) if hasattr(token_b, "expires_at") else 0,
            )
            logger.info(
                "it_dispatcher_result_ready request_id=%s method=%s",
                request_id,
                mcp_method,
            )

        # ── d. CIBA denied ────────────────────────────────────────────────────
        except CIBADeniedError:
            logger.info("it_dispatcher_ciba_denied request_id=%s", request_id)
            state.error = ErrorPayload(error_id="ERR-CIBA-005", reason="user_denied")

        # ── e. CIBA expired ───────────────────────────────────────────────────
        except CIBAExpiredError:
            logger.info("it_dispatcher_ciba_expired request_id=%s", request_id)
            state.error = ErrorPayload(
                error_id="ERR-CIBA-009", reason="auth_req_id_expired"
            )

        # ── f. CIBA timeout (includes cancel) ─────────────────────────────────
        except CIBATimeoutError:
            reason = (
                "cancelled" if state.cancel_event.is_set() else "polling_timeout"
            )
            logger.info(
                "it_dispatcher_ciba_timeout request_id=%s reason=%s",
                request_id,
                reason,
            )
            state.error = ErrorPayload(error_id="ERR-CIBA-010", reason=reason)

        # ── g. MCP HTTP error ─────────────────────────────────────────────────
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
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
                "it_dispatcher_mcp_http_error request_id=%s status=%s upstream_id=%s",
                request_id,
                status,
                upstream_id,
            )
            state.error = ErrorPayload(
                error_id=error_id,
                reason=f"MCP HTTP {status}: {exc}",
            )

        # ── h. Unexpected exception ───────────────────────────────────────────
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "it_dispatcher_unexpected_error request_id=%s error=%r",
                request_id,
                exc,
            )
            state.error = ErrorPayload(
                error_id="ERR-AGENT-INTERNAL", reason=str(exc)
            )

        # ── i. Always set completion (F-01) ───────────────────────────────────
        finally:
            state.completion.set()
