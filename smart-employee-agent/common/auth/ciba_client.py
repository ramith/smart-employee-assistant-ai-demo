"""CIBA client: initiate, poll, and acquire OBO tokens via IS CIBA grant.

This is the central CIBA module for the Smart Employee Agent POC.  It owns
every HTTP interaction with ``/oauth2/ciba`` and the CIBA-grant path of
``/oauth2/token``.  Other modules (``wso2_is_client.py``) are NOT involved —
CIBA HTTP code lives here by design.

Polling state machine (ported exactly from
``idp_capability_test/c8_ciba.py:254-296``, empirically validated against IS):

    loop while deadline not reached:
        POST /oauth2/token with grant_type=urn:openid:params:grant-type:ciba
        parse JSON body
        if access_token present:
            return OAuthToken
        if error == "authorization_pending":
            sleep(interval); continue
        if error == "slow_down":
            interval += 5; sleep(interval); continue
        if error == "expired_token":
            raise CIBAExpiredError (ERR-CIBA-009)
        if error == "access_denied":
            raise CIBADeniedError (ERR-CIBA-005)
        else:
            raise CIBAPollError (ERR-CIBA-008)
    raise CIBATimeoutError (ERR-CIBA-010)

F-10 MANDATORY RULES FOR CALLERS (``hr_agent/ciba/orchestrator.py`` et al.):
    1. Catch ONLY ``httpx.NetworkError`` for retry-on-network-error inside the
       poll loop.  NEVER catch ``Exception`` or ``BaseException`` broadly.
       ``CIBADeniedError``, ``CIBAExpiredError``, ``CIBATimeoutError`` MUST
       propagate.
    2. Wire ``add_done_callback`` to surface results/errors to
       ``SpecialistState.completion``:

           def _on_done(task: asyncio.Task[OBOToken]) -> None:
               if task.cancelled():
                   state.error = asyncio.CancelledError()
               elif (exc := task.exception()) is not None:
                   state.error = exc
               else:
                   state.result = task.result()
               state.completion.set()
               state.poll_task = None  # rule 3: null out after done

           state.poll_task = asyncio.create_task(poll_for_token(...))
           state.poll_task.add_done_callback(_on_done)

    3. After done/cancelled, set ``state.poll_task = None`` (inside
       ``_on_done``) so retries do not see a stale handle.

    asyncio.CancelledError is BaseException — it MUST propagate; never catch it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from .errors import (
    CIBADeniedError,
    CIBAExpiredError,
    CIBAInitiationError,
    CIBAPollError,
    CIBATimeoutError,
)
from .models import OAuthToken

logger = logging.getLogger(__name__)

# RFC 9449-bis / OpenID CIBA grant type URN
_CIBA_GRANT_TYPE = "urn:openid:params:grant-type:ciba"


def _utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(tz=timezone.utc)


# ── CIBARequest ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CIBARequest:
    """State returned by a successful ``POST /oauth2/ciba`` call.

    Attributes:
        auth_req_id: IS-issued opaque identifier for this CIBA flow.
        auth_url: Consent URL returned when ``notification_channel=external``
            (may be empty string for email/SMS channels).
        interval_s: Polling interval in seconds (IS default: 2).
        expires_in_s: Lifetime of ``auth_req_id`` in seconds (IS default: 120–300).
        issued_at: UTC datetime at which this CIBARequest was created locally;
            used to compute remaining time.
    """

    auth_req_id: str
    auth_url: str
    interval_s: int
    expires_in_s: int
    issued_at: datetime


# ── CIBAClientConfig ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CIBAClientConfig:
    """Immutable configuration for a :class:`CIBAClient` instance.

    Attributes:
        is_base_url: Root URL of the WSO2 IS instance (no trailing slash),
            e.g. ``https://13.60.190.47:9443``.
        insecure_tls: Disable TLS certificate verification.  Dev only.
        notification_channel: CIBA notification channel to request.  Use
            ``"external"`` so that IS returns an ``auth_url`` for the SPA.
        default_max_wait_seconds: Default polling budget; callers may override
            per-call via ``max_wait_seconds`` in :meth:`CIBAClient.poll_for_token`.
    """

    is_base_url: str
    insecure_tls: bool = False
    notification_channel: str = "external"
    default_max_wait_seconds: float = 300.0


# ── CIBAClient ────────────────────────────────────────────────────────────────


@dataclass
class CIBAClient:
    """Async CIBA client: initiate consent, poll for token, high-level acquire_obo.

    Lifecycle:
        Use as an async context manager or call :meth:`aclose` explicitly.

        If *http* is ``None`` (default), an ``httpx.AsyncClient`` is created
        internally and owned by this instance — :meth:`aclose` will close it.
        If *http* is injected (e.g. from ``pytest-httpx``), the caller owns it
        and :meth:`aclose` is a no-op on that client.

    Args:
        config: Connection and behaviour parameters.
        http: Optional pre-built ``httpx.AsyncClient`` for testing injection.
    """

    config: CIBAClientConfig
    http: httpx.AsyncClient | None = field(default=None)
    _owns_http: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.http is None:
            self.http = httpx.AsyncClient(
                verify=not self.config.insecure_tls,
                headers={"Accept": "application/json"},
            )
            self._owns_http = True

    # ── URL helpers ───────────────────────────────────────────────────────────

    @property
    def _ciba_url(self) -> str:
        return f"{self.config.is_base_url}/oauth2/ciba"

    @property
    def _token_url(self) -> str:
        return f"{self.config.is_base_url}/oauth2/token"

    # ── initiate ──────────────────────────────────────────────────────────────

    async def initiate(
        self,
        *,
        oauth_client_id: str,
        oauth_client_secret: str,
        login_hint: str,
        binding_message: str,
        actor_token: str,
        scope: str = "openid",
        resource: str | None = None,
    ) -> CIBARequest:
        """POST ``/oauth2/ciba`` to start a CIBA consent flow.

        Sends Basic-auth with the agent's OAuth App credentials.  The
        ``actor_token`` is included in the request body per the IS CIBA spec
        (``docs/configuring-ciba-grant-type.md``).

        Args:
            oauth_client_id: The agent's OAuth Application client_id.
            oauth_client_secret: Corresponding client secret.
            login_hint: User's ``sub`` UUID extracted from the inbound token-A.
            binding_message: Pre-rendered consent string from
                ``common.auth.binding_messages.render()`` (F-05).
            actor_token: The agent's own I4 token from ``ActorTokenProvider``.
            scope: Space-separated scope string (default ``"openid"``).
            resource: Optional resource indicator (F-06 notes IS ignores this
                for CIBA; included for spec completeness).

        Returns:
            :class:`CIBARequest` populated from the IS response.

        Raises:
            CIBAInitiationError: IS returned a non-2xx status OR the 200 body
                does not contain ``auth_req_id``.
        """
        assert self.http is not None  # guaranteed by __post_init__

        form_data: dict[str, str] = {
            "scope": scope,
            "login_hint": login_hint,
            "binding_message": binding_message,
            "actor_token": actor_token,
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "notification_channel": self.config.notification_channel,
        }
        if resource is not None:
            form_data["resource"] = resource

        logger.debug(
            "ciba_initiate | login_hint=%s scope=%s channel=%s",
            login_hint,
            scope,
            self.config.notification_channel,
        )

        response = await self.http.post(
            self._ciba_url,
            auth=httpx.BasicAuth(oauth_client_id, oauth_client_secret),
            data=form_data,
        )

        if not response.is_success:
            body_text = response.text[:500]
            logger.error(
                "ciba_initiate_failed | status=%d body=%s",
                response.status_code,
                body_text,
            )
            raise CIBAInitiationError(
                f"POST /oauth2/ciba returned HTTP {response.status_code}: {body_text}",
                details={
                    "http_status": response.status_code,
                    "body": body_text,
                    "login_hint": login_hint,
                },
            )

        try:
            body: dict = response.json()
        except Exception as exc:
            raise CIBAInitiationError(
                f"POST /oauth2/ciba returned non-JSON body: {response.text[:200]}",
                details={"body": response.text[:200]},
            ) from exc

        auth_req_id: str | None = body.get("auth_req_id")
        if not auth_req_id:
            raise CIBAInitiationError(
                "POST /oauth2/ciba succeeded but auth_req_id absent in response",
                details={"body": str(body)[:500]},
            )

        auth_url: str = body.get("auth_url", "")
        interval_s: int = int(body.get("interval", 2))
        expires_in_s: int = int(body.get("expires_in", 120))
        issued_at: datetime = _utc_now()

        if self.config.notification_channel == "external" and not auth_url:
            logger.warning(
                "ciba_initiate_no_auth_url | auth_req_id=%s channel=external",
                auth_req_id,
            )

        logger.info(
            "ciba_initiated | auth_req_id=%s expires_in=%ds interval=%ds auth_url_present=%s",
            auth_req_id,
            expires_in_s,
            interval_s,
            bool(auth_url),
        )

        return CIBARequest(
            auth_req_id=auth_req_id,
            auth_url=auth_url,
            interval_s=interval_s,
            expires_in_s=expires_in_s,
            issued_at=issued_at,
        )

    # ── poll_for_token ────────────────────────────────────────────────────────

    async def poll_for_token(
        self,
        *,
        ciba_request: CIBARequest,
        oauth_client_id: str,
        oauth_client_secret: str,
        max_wait_seconds: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> OAuthToken:
        """Poll ``/oauth2/token`` until the user consents or a stop condition fires.

        Implements the RFC 7522-bis back-channel polling state machine, ported
        exactly from ``idp_capability_test/c8_ciba.py:254-296``.

        Back-off rules:
        - ``authorization_pending`` → sleep(interval), retry
        - ``slow_down`` → interval += 5, sleep(interval), retry
        - ``expired_token`` → raise :class:`~common.auth.errors.CIBAExpiredError`
        - ``access_denied`` → raise :class:`~common.auth.errors.CIBADeniedError`
        - any other error → raise :class:`~common.auth.errors.CIBAPollError`

        Stop conditions:
        - ``access_token`` in response body → return :class:`~common.auth.models.OAuthToken`
        - Wall-clock elapsed ≥ ``max_wait_seconds`` → raise
          :class:`~common.auth.errors.CIBATimeoutError` (ERR-CIBA-010)
        - ``cancel_event.is_set()`` checked before each sleep → raise
          :class:`~common.auth.errors.CIBATimeoutError` with ``reason='cancelled'``

        F-10 RULES (enforced here):
        - Only ``httpx.NetworkError`` is caught for retry; all other exceptions
          propagate.  ``asyncio.CancelledError`` (BaseException) is NEVER caught.
        - ``CIBADeniedError`` / ``CIBAExpiredError`` / ``CIBATimeoutError``
          propagate out of this method to the caller's ``add_done_callback``.

        Note: NO ``actor_token`` is sent on the poll request (per F-05 spec).

        Args:
            ciba_request: State returned by :meth:`initiate`.
            oauth_client_id: Agent's OAuth Application client_id.
            oauth_client_secret: Corresponding client secret.
            max_wait_seconds: Override the config default.  ``None`` uses
                ``config.default_max_wait_seconds``.
            cancel_event: When set, the next pre-sleep check raises
                :class:`~common.auth.errors.CIBATimeoutError` with ``reason='cancelled'``.

        Returns:
            :class:`~common.auth.models.OAuthToken` on successful consent.

        Raises:
            CIBADeniedError: User clicked Deny (``access_denied``).
            CIBAExpiredError: ``auth_req_id`` expired at IS (``expired_token``).
            CIBATimeoutError: Local budget exhausted or ``cancel_event`` fired.
            CIBAPollError: Unexpected IS error code.
        """
        assert self.http is not None  # guaranteed by __post_init__

        budget: float = (
            max_wait_seconds
            if max_wait_seconds is not None
            else self.config.default_max_wait_seconds
        )
        deadline: float = _utc_now().timestamp() + budget
        interval: int = ciba_request.interval_s
        poll_count: int = 0

        while _utc_now().timestamp() < deadline:
            # ── cancel_event check (before sleep, not before poll) ─────────
            # Checked here so a cancel that arrives while sleeping is caught on
            # the next loop iteration before we fire another HTTP request.
            if cancel_event is not None and cancel_event.is_set():
                logger.info(
                    "ciba_poll_cancelled | auth_req_id=%s poll_count=%d",
                    ciba_request.auth_req_id,
                    poll_count,
                )
                raise CIBATimeoutError(
                    f"CIBA poll cancelled by caller after {poll_count} attempts",
                    details={
                        "auth_req_id": ciba_request.auth_req_id,
                        "reason": "cancelled",
                        "poll_count": poll_count,
                    },
                )

            poll_count += 1

            # ── network call — only httpx.NetworkError is retried ──────────
            # F-10: NEVER catch Exception/BaseException here.
            # asyncio.CancelledError is BaseException and must propagate.
            try:
                response = await self.http.post(
                    self._token_url,
                    auth=httpx.BasicAuth(oauth_client_id, oauth_client_secret),
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={
                        "grant_type": _CIBA_GRANT_TYPE,
                        "auth_req_id": ciba_request.auth_req_id,
                    },
                )
            except httpx.NetworkError as exc:
                logger.warning(
                    "ciba_poll_network_error | auth_req_id=%s poll_count=%d error=%s",
                    ciba_request.auth_req_id,
                    poll_count,
                    exc,
                )
                await asyncio.sleep(interval)
                continue

            # ── parse response ─────────────────────────────────────────────
            try:
                body: dict = response.json()
            except Exception:
                body = {}

            err: str | None = body.get("error")

            # Happy path: token issued
            if body.get("access_token"):
                logger.info(
                    "ciba_poll_token_issued | auth_req_id=%s poll_count=%d",
                    ciba_request.auth_req_id,
                    poll_count,
                )
                return OAuthToken.from_response(body)

            # Pending — user has not yet acted
            if err == "authorization_pending":
                logger.debug(
                    "ciba_poll_pending | auth_req_id=%s poll_count=%d sleep=%ds",
                    ciba_request.auth_req_id,
                    poll_count,
                    interval,
                )
                await asyncio.sleep(interval)
                continue

            # Slow-down — IS asks us to back off
            if err == "slow_down":
                interval += 5
                logger.debug(
                    "ciba_poll_slow_down | auth_req_id=%s new_interval=%ds",
                    ciba_request.auth_req_id,
                    interval,
                )
                await asyncio.sleep(interval)
                continue

            # auth_req_id expired before user consented
            if err == "expired_token":
                logger.info(
                    "ciba_poll_expired | auth_req_id=%s poll_count=%d",
                    ciba_request.auth_req_id,
                    poll_count,
                )
                raise CIBAExpiredError(
                    f"auth_req_id expired before user consented (poll #{poll_count})",
                    details={
                        "auth_req_id": ciba_request.auth_req_id,
                        "poll_count": poll_count,
                    },
                )

            # User denied consent
            if err == "access_denied":
                logger.info(
                    "ciba_poll_denied | auth_req_id=%s poll_count=%d",
                    ciba_request.auth_req_id,
                    poll_count,
                )
                raise CIBADeniedError(
                    f"User denied consent (poll #{poll_count})",
                    details={
                        "auth_req_id": ciba_request.auth_req_id,
                        "poll_count": poll_count,
                    },
                )

            # Unexpected IS error
            err_desc: str = body.get("error_description", "")
            logger.error(
                "ciba_poll_unexpected_error | auth_req_id=%s error=%s description=%s",
                ciba_request.auth_req_id,
                err,
                err_desc,
            )
            raise CIBAPollError(
                f"Unexpected CIBA poll error: error={err!r} description={err_desc!r} "
                f"(poll #{poll_count})",
                details={
                    "auth_req_id": ciba_request.auth_req_id,
                    "error": err,
                    "error_description": err_desc,
                    "poll_count": poll_count,
                },
            )

        # Deadline reached without token
        logger.info(
            "ciba_poll_timeout | auth_req_id=%s poll_count=%d budget=%.1fs",
            ciba_request.auth_req_id,
            poll_count,
            budget,
        )
        raise CIBATimeoutError(
            f"CIBA polling timed out after {poll_count} attempts ({budget:.0f}s budget)",
            details={
                "auth_req_id": ciba_request.auth_req_id,
                "poll_count": poll_count,
                "budget_seconds": budget,
            },
        )

    # ── acquire_obo ───────────────────────────────────────────────────────────

    async def acquire_obo(
        self,
        *,
        oauth_client_id: str,
        oauth_client_secret: str,
        login_hint: str,
        binding_message: str,
        actor_token: str,
        scope: str = "openid",
        max_wait_seconds: float | None = None,
        on_consent_required: Callable[[CIBARequest], Awaitable[None]] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[CIBARequest, OAuthToken]:
        """High-level helper: initiate CIBA + optional hook + poll.

        Calls :meth:`initiate`, then (if provided) awaits
        ``on_consent_required(ciba_request)`` so the caller can push
        ``auth_url`` to the SPA before blocking on the poll.  Then calls
        :meth:`poll_for_token` and returns ``(ciba_request, token)`` on
        success.

        ``on_consent_required`` is NOT called if :meth:`initiate` raises.

        Args:
            oauth_client_id: Agent's OAuth Application client_id.
            oauth_client_secret: Corresponding client secret.
            login_hint: User's ``sub`` UUID.
            binding_message: Pre-rendered consent string (F-05).
            actor_token: Agent's I4 token.
            scope: Space-separated scope string.
            max_wait_seconds: Override polling budget.
            on_consent_required: Async callback invoked with the
                :class:`CIBARequest` after initiation succeeds, before polling
                begins.  Use this to push ``auth_url`` to the SPA.
            cancel_event: Forwarded to :meth:`poll_for_token`.

        Returns:
            ``(ciba_request, token)`` on successful consent.

        Raises:
            CIBAInitiationError: If :meth:`initiate` fails.
            CIBADeniedError: User denied consent.
            CIBAExpiredError: ``auth_req_id`` expired.
            CIBATimeoutError: Polling budget exhausted or cancelled.
            CIBAPollError: Unexpected IS error during polling.
        """
        ciba_request = await self.initiate(
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            login_hint=login_hint,
            binding_message=binding_message,
            actor_token=actor_token,
            scope=scope,
        )

        if on_consent_required is not None:
            await on_consent_required(ciba_request)

        token = await self.poll_for_token(
            ciba_request=ciba_request,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            max_wait_seconds=max_wait_seconds,
            cancel_event=cancel_event,
        )

        return ciba_request, token

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Close the owned ``httpx.AsyncClient``.

        No-op if the client was injected by the caller.
        """
        if self._owns_http and self.http is not None:
            await self.http.aclose()

    async def __aenter__(self) -> "CIBAClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
