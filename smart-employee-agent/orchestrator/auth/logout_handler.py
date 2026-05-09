"""orchestrator/auth/logout_handler.py — orchestrates the locked Sprint 3 logout cascade.

Sprint 3 3A.1 deliverable. Implements the ordering invariants from
``docs/architecture/sprint-3-tech-arch.md`` §1.1 and §4.4:

  1. Acquire per-user_sub asyncio.Lock (FIX-12: serialises UC-09/UC-10).
  2. Set Session.terminating = True (BLOCK-G: snapshot fence;
     rejects new chat/CIBA-initiate requests with 401).
  3. Snapshot session state (token_a, pending_ciba list, completed_ciba_log).
  4. Cancel pending CIBAs FIRST (BLOCK-F): set cancel_event,
     await cancelled_ack barrier ≤100 ms.
  5. Revoke token-A at IS via /oauth2/revoke (best-effort; F-21 means
     this does NOT propagate to OBO tokens).
  6. Fan-out internal /internal/events to all 4 receivers in parallel
     with inline retry-once (3A.2 wires the actual RPC; this slice
     leaves a stub).
  7. Clear orch_sid cookie + remove Session entry (LAST mutation per
     BLOCK-H ordering invariant).
  8. Release lock.
  9. Caller (routes.py) returns JSON {redirect_url} for the SPA.

Q3 (Stage 1 lock): the SPA navigates to the IS RP-initiated logout URL,
which renders WSO2's "Yes, sign me out" consent page. After IS confirms,
it 302s to post_logout_redirect_uri.

Boundary rules
--------------
- F-09: ``LogoutCascadeContext`` is a dataclass (holds runtime references).
- This module does NOT call the SPA. It returns the redirect URL string;
  the route handler builds the JSON response.
- F-21 implication: best-effort everywhere. Failures are logged but do
  not block the cascade (the user is signing out — UX over correctness).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field

from orchestrator.agent_registry.revoke_client import InternalEventsClient
from orchestrator.auth.is_revoke import RevokeClient, RevokeError
from orchestrator.auth.session_store import IssuedTokenRecord, Session, SessionStore
from orchestrator.config import OrchestratorConfig

logger = logging.getLogger(__name__)

__all__ = ["LogoutHandler", "LogoutResult"]


# 3A.1 FIX-9 / NIT-5: short-lived map of state nonces issued on logout
# redirect. Validated when the user returns to /?reason=signed_out so a
# malicious page can't construct its own ?state=anything.
# 60-second TTL; pruned on access. Single-process per Q5.
_logout_state_ttl_seconds = 60
_logout_state_nonces: dict[str, float] = {}


def _issue_logout_state() -> str:
    """Mint a fresh state nonce; record its issue time. NIT-5."""
    nonce = secrets.token_urlsafe(16)
    _logout_state_nonces[nonce] = time.time()
    return nonce


def _validate_logout_state(nonce: str) -> bool:
    """Return True iff *nonce* was issued within the TTL and hasn't been used. NIT-5."""
    issued_at = _logout_state_nonces.pop(nonce, None)
    if issued_at is None:
        return False
    if time.time() - issued_at > _logout_state_ttl_seconds:
        return False
    return True


def _prune_logout_state() -> None:
    """Drop expired logout-state nonces. Called on each cascade entry."""
    cutoff = time.time() - _logout_state_ttl_seconds
    expired = [n for n, t in _logout_state_nonces.items() if t < cutoff]
    for n in expired:
        _logout_state_nonces.pop(n, None)


@dataclass
class LogoutResult:
    """Outcome of the logout cascade — used by the route handler.

    Attributes:
        had_session: ``True`` if a Session existed for the cookie. ``False``
            for idempotent "already signed out" calls.
        redirect_url: The IS RP-initiated logout URL the SPA should
            navigate to. ``None`` when ``had_session`` is False (SPA
            navigates to ``/`` directly).
        request_id: Caller-supplied rid (echoed for SSE logging).
        reason_label: ``"user_signed_out"`` always for UC-09. UC-10 will
            override to ``"admin_terminated"`` when 3B.1 wires this same
            cascade behind the BCL receiver.
    """

    had_session: bool
    redirect_url: str | None
    request_id: str
    reason_label: str = "user_signed_out"


@dataclass
class LogoutHandler:
    """Stateful handler for the Sprint 3 logout cascade.

    One instance per orchestrator app (held by AuthRouterDeps). Stitches
    together the session store, IS revoke client, and (3A.2) the internal
    fan-out client.

    Attributes:
        config: Orchestrator configuration (for is_base_url, client_id, cookie names).
        session_store: In-memory session store.
        revoke_client: IS /oauth2/revoke wrapper.
        cancel_barrier_seconds: Max time to wait for ``PendingCIBA.cancelled_ack``
            after ``cancel_event.set()``. BLOCK-F default = 0.1.
    """

    config: OrchestratorConfig
    session_store: SessionStore
    revoke_client: RevokeClient
    # 3A.2: fan-out client injected. ``None`` keeps the stub-only mode used
    # by tests that don't care about the receiver side.
    events_client: InternalEventsClient | None = None
    cancel_barrier_seconds: float = 0.1

    async def execute(
        self,
        *,
        session: Session,
        request_id: str,
        reason: str = "user_signed_out",
    ) -> LogoutResult:
        """Run the logout cascade for *session*.

        Caller (route handler) is responsible for:
          - reading the orch_sid cookie and resolving Session (or returning early),
          - setting the response cookie deletion header,
          - returning the JSON {redirect_url} body.

        Args:
            session: The Session to terminate.
            request_id: rid for log correlation.
            reason: ``"user_signed_out"`` (UC-09) or ``"admin_terminated"``
                (UC-10, future). Surfaces in the audit chain and (3B.2)
                drives the binding_message branch.

        Returns:
            ``LogoutResult`` describing what to send back to the SPA.
        """
        _prune_logout_state()

        # Step 1: acquire per-user_sub lock (FIX-12).
        user_lock = self.session_store.get_user_lock(session.user_sub)
        async with user_lock:
            return await self._execute_locked(
                session=session,
                request_id=request_id,
                reason=reason,
            )

    async def _execute_locked(
        self,
        *,
        session: Session,
        request_id: str,
        reason: str,
    ) -> LogoutResult:
        # If a concurrent UC-10 cascade ran while we waited for the lock,
        # the Session has already been removed. Idempotency: short-circuit
        # but still log the reason for the audit chain (FIX-12).
        if session.session_id not in self.session_store._sessions:  # noqa: SLF001
            logger.warning(
                "logout_cascade_already_run | rid=%s reason=%s user_sub=%s",
                request_id,
                reason,
                session.user_sub,
            )
            return LogoutResult(
                had_session=False,
                redirect_url=None,
                request_id=request_id,
                reason_label=reason,
            )

        # Step 2: BLOCK-G — Session.terminating is the first state mutation.
        # Chat/CIBA paths see this and 401 (orchestrator/chat/routes.py + ciba/cancel).
        session.terminating = True
        logger.info(
            "logout_cascade_start | rid=%s reason=%s session_id=%s user_sub=%s",
            request_id,
            reason,
            session.session_id[:8],
            session.user_sub,
        )

        # Step 3: snapshot state.
        token_a_access = session.token_a.access_token
        token_a_id_token = getattr(session.token_a, "id_token", None)
        pending_list = list(session.pending_ciba.values())
        completed_log: list[IssuedTokenRecord] = list(session.completed_ciba_log)

        # Step 4: BLOCK-F — cancel pending CIBAs FIRST, await ack barrier.
        await self._cancel_pending_ciba(pending_list, request_id)

        # Step 5: revoke token-A at IS (best-effort).
        try:
            await self.revoke_client.revoke_access_token(
                token_a_access, request_id=request_id
            )
        except RevokeError as exc:
            # F-21: revoke failure does not change the cascade outcome — denylist
            # at receivers (3A.2) is the security boundary. Log and proceed.
            logger.warning(
                "is_revoke_failed_proceeding | rid=%s err=%s",
                request_id,
                exc,
            )

        # Step 6: fan-out to internal /internal/events on 4 receivers.
        # 3A.2: real RPC client wired. Each completed CIBA contributes its
        # jti to the fan-out; the receivers (HR-AGENT, IT-AGENT, hr_server,
        # it_server) all get told about it.
        if completed_log and self.events_client is not None:
            for record in completed_log:
                # Each fan_out call is per-jti; if the user has multiple OBO
                # tokens (multi-CIBA, multi-agent) we issue one fan-out per
                # token. FanOutReport return is logged inside fan_out() —
                # we capture the structure here for future SSE event emission.
                await self.events_client.fan_out(
                    jti=record.jti,
                    user_sub=session.user_sub,
                    exp=float(record.exp),
                    reason=reason,
                    request_id=request_id,
                )
        elif completed_log:
            # Test mode: events_client is None. Still emit the per-record log
            # line so the audit chain is unbroken.
            for record in completed_log:
                logger.info(
                    "logout_fanout_stub | rid=%s agent_id=%s jti=%s exp=%s reason=%s",
                    request_id,
                    record.agent_id,
                    record.jti[:8],
                    record.exp,
                    reason,
                )

        # Step 7: clear cookie + Session (LAST mutation per BLOCK-H).
        await self.session_store.delete(session.session_id)

        # Step 9: build the IS RP-initiated logout URL.
        redirect_url = self._build_is_logout_url(token_a_id_token)
        return LogoutResult(
            had_session=True,
            redirect_url=redirect_url,
            request_id=request_id,
            reason_label=reason,
        )

    async def _cancel_pending_ciba(self, pending_list: list, request_id: str) -> None:
        """Set cancel_event on each pending CIBA, await cancelled_ack barriers.

        BLOCK-F: cancellation must complete BEFORE fan-out so the poll task
        cannot mint a new token-B while the denylist is propagating.

        Args:
            pending_list: snapshot of session.pending_ciba.values() (live
                references — setting cancel_event on them is what we want).
            request_id: rid for log correlation.
        """
        if not pending_list:
            return

        for pending in pending_list:
            pending.cancel_event.set()

        async def _wait(p):
            try:
                await asyncio.wait_for(
                    p.cancelled_ack.wait(), timeout=self.cancel_barrier_seconds
                )
                return True
            except asyncio.TimeoutError:
                logger.warning(
                    "cancel_barrier_timeout | rid=%s auth_req_id=%s agent_id=%s",
                    request_id,
                    p.auth_req_id[:8],
                    p.agent_id,
                )
                return False

        results = await asyncio.gather(*(_wait(p) for p in pending_list))
        acked = sum(1 for r in results if r)
        logger.info(
            "ciba_cancel_barrier | rid=%s pending=%d acked=%d",
            request_id,
            len(pending_list),
            acked,
        )

    def _build_is_logout_url(self, id_token: str | None) -> str:
        """Build the IS /oidc/logout URL with id_token_hint (F-19 corrected: required).

        Per the source-code analysis (sprint-3-is-source-analysis.md §2),
        WSO2 IS only fans out BCL when /oidc/logout receives id_token_hint
        (or client_id). Without it, IS hits the empty-cache branch and never
        walks session participants. Our locked Q3 design always uses
        id_token_hint, which means BCL fan-out from IS reaches the
        orchestrator-mcp-client app (and any other session participants
        that registered backchannel_logout_uri).

        Args:
            id_token: The id_token captured at code-exchange time. May be
                None on stale sessions; in that case we still hit
                /oidc/logout with client_id only — IS will fan BCL based
                on the OPBS cookie.

        Returns:
            Full URL string for SPA window.location.href.
        """
        post_logout = f"{_spa_base_url(self.config)}/?reason=signed_out"
        state = _issue_logout_state()
        params: list[tuple[str, str]] = [
            ("client_id", self.config.mcp_client_id),
            ("post_logout_redirect_uri", post_logout),
            ("state", state),
        ]
        if id_token:
            params.append(("id_token_hint", id_token))
        return f"{self.config.is_base_url}/oidc/logout?{urllib.parse.urlencode(params)}"


def _spa_base_url(config: OrchestratorConfig) -> str:
    """Mirror of orchestrator/auth/routes.py::_spa_base_url; duplicated to
    avoid an import cycle (routes imports this module)."""
    return sorted(config.allowed_origins)[0]
