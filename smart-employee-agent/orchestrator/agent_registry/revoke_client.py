"""orchestrator/agent_registry/revoke_client.py — outbound /internal/events RPC.

Sprint 3 3A.2 deliverable. Used by ``logout_handler`` to fan out
denylist updates to all 4 receivers (HR-AGENT, IT-AGENT, hr_server,
it_server) in parallel, with inline retry-once @ 200 ms per FIX-22.

Wire shape (sprint-3-tech-arch.md §3.2 / common/revocation/internal_events.py):

    POST {receiver_url}/internal/events
    X-Internal-Auth: <INTERNAL_REVOKE_SHARED_SECRET>
    X-Request-ID: <rid>
    Body: {type: "session-revoked", subject: {sub, jti}, exp, reason}

Boundary rules
--------------
- F-09: this is a stateful client (httpx.AsyncClient inside) — plain class.
- The fan-out is best-effort. Each leg is allowed to fail independently;
  ``execute()`` returns a ``FanOutReport`` describing what acked vs. what
  fell back to the receiver-side denylist's natural delivery (i.e. nothing
  in 3A.2; introspection backstop is only token-A-bound per F-21).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

__all__ = ["FanOutReport", "FanOutTarget", "InternalEventsClient"]


@dataclass(frozen=True, slots=True)
class FanOutTarget:
    """One receiver in the fan-out.

    Attributes:
        label: Short identifier used in logs / FanOutReport.
        url: Base URL (e.g. ``http://hr-agent:8001``) — ``/internal/events``
            is appended at call time.
    """

    label: str
    url: str


@dataclass
class FanOutReport:
    """Outcome of a single fan-out invocation across all targets.

    Used by ``logout_handler`` for log emission per Stage 4 FIX-6
    (SECURITY-DEGRADED label on all-legs failure).

    Attributes:
        successes: List of target labels that 200'd.
        failures: List of (target_label, error_message) tuples for legs
            that failed both first attempt and retry.
        total: Total targets attempted.
    """

    successes: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.successes) + len(self.failures)

    @property
    def all_failed(self) -> bool:
        return self.total > 0 and not self.successes


class InternalEventsClient:
    """Fan-out RPC to ``POST /internal/events`` on N receivers.

    One instance per orchestrator app. Holds a long-lived ``httpx.AsyncClient``.

    Attributes:
        targets: List of ``FanOutTarget`` to call in parallel.
        shared_secret: Value sent as ``X-Internal-Auth``.
        retry_once_after_ms: How long to wait before the inline retry
            (FIX-22 default = 200).
        timeout_seconds: Per-leg HTTP timeout.
    """

    def __init__(
        self,
        *,
        targets: list[FanOutTarget],
        shared_secret: str,
        retry_once_after_ms: float = 200.0,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not shared_secret:
            raise ValueError(
                "shared_secret must be non-empty; set INTERNAL_REVOKE_SHARED_SECRET in env."
            )
        self.targets = targets
        self.shared_secret = shared_secret
        self.retry_once_after_ms = retry_once_after_ms
        self.timeout_seconds = timeout_seconds
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def fan_out(
        self,
        *,
        jti: str,
        user_sub: str,
        exp: float,
        reason: str,
        request_id: str,
    ) -> FanOutReport:
        """Fan out a session-revoked event to all targets in parallel.

        Each leg has inline retry-once on the first failure (FIX-22). The
        overall ``asyncio.gather`` collects per-leg outcomes; nothing
        bubbles out as an unhandled exception.

        Args:
            jti: jti of the revoked OBO token (or token-A for the
                orchestrator-driven path).
            user_sub: User uuid; included for audit and for the agent's
                cache lookup.
            exp: JWT ``exp`` claim — receivers' sweepers need this.
            reason: ``"user_signed_out"`` (UC-09) or ``"admin_terminated"``
                (UC-10, 3B.1). Echoed in receiver logs.
            request_id: Caller rid; propagated as ``X-Request-ID``.

        Returns:
            ``FanOutReport`` summarising per-leg outcomes.
        """
        body = {
            "type": "session-revoked",
            "subject": {"sub": user_sub, "jti": jti},
            "exp": exp,
            "reason": reason,
        }
        headers = {
            "X-Internal-Auth": self.shared_secret,
            "X-Request-ID": request_id,
            "Content-Type": "application/json",
        }
        client = await self._client()

        async def _call_one(target: FanOutTarget) -> tuple[FanOutTarget, str | None]:
            url = target.url.rstrip("/") + "/internal/events"
            for attempt in (1, 2):
                try:
                    resp = await client.post(url, json=body, headers=headers)
                    if 200 <= resp.status_code < 300:
                        return target, None
                    err = f"HTTP {resp.status_code} body={resp.text[:120]!r}"
                except httpx.HTTPError as exc:
                    err = f"network error: {exc}"
                if attempt == 1:
                    await asyncio.sleep(self.retry_once_after_ms / 1000.0)
                    continue
                return target, err
            return target, "unreachable"  # not reachable; shape preservation

        results = await asyncio.gather(*(_call_one(t) for t in self.targets))
        report = FanOutReport()
        for target, err in results:
            if err is None:
                report.successes.append(target.label)
                logger.info(
                    "internal_event_sent | rid=%s target=%s jti=%s reason=%s",
                    request_id,
                    target.label,
                    jti[:8],
                    reason,
                )
            else:
                report.failures.append((target.label, err))
                logger.warning(
                    "logout_fanout_partial | rid=%s target=%s jti=%s err=%s",
                    request_id,
                    target.label,
                    jti[:8],
                    err,
                )

        if report.all_failed:
            # FIX-6: SECURITY_DEGRADED label is the operator grep target
            # (R-LOGOUT-7b acceptance asserts the literal string).
            logger.error(
                "logout_fanout_total_failure SECURITY_DEGRADED | rid=%s jti=%s reason=%s targets=%d",
                request_id,
                jti[:8],
                reason,
                report.total,
            )

        return report
