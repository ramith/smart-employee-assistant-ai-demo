"""Cached agent actor-token provider using WSO2 IS App-Native Auth (3-step).

The agent's I4 token is minted once via the 3-step App-Native Auth flow and
cached in memory with a 30-second pre-expiry buffer.  Concurrent callers share
a single asyncio.Lock so only one mint is ever in-flight (single-flight pattern
ported from ``_archive/agent.before-v3/agent_auth.py:AgentAuth.ensure_valid_token``).

Boundary rules (F-09):
- ``AgentCredentials`` and ``ActorTokenProvider`` are ``@dataclass`` because
  ``ActorTokenProvider`` holds an ``asyncio.Lock`` (non-serialisable runtime
  object).  No Pydantic here.

PKCE generation matches ``idp_capability_test/c4_app_native_authn.py:pkce_pair``:
- Verifier  : ``secrets.token_urlsafe(32)`` (URL-safe base64, no padding)
- Challenge : SHA-256 digest of verifier bytes, base64url-encoded, no padding

Security notes:
- ``agent_secret`` and ``oauth_client_secret`` are high-value secrets.
  They MUST NOT appear in log output; the ``RedactionFilter`` strips JWT-shaped
  strings but callers should also avoid passing credentials as positional log args.
- ``access_token`` is redacted via ``[REDACTED]`` in WARNING-level logs.
- The cache is memory-only (T1 mitigation); no persistence or serialisation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .errors import ActorTokenError
from .models import OAuthToken
from .wso2_is_client import WSO2ISClient

logger = logging.getLogger(__name__)

REFRESH_BUFFER_SECONDS: int = 2

# Hard cap on how long the in-process cache trusts a freshly-minted actor token,
# regardless of the token's actual `exp` (IS issues 1-hour tokens). Keeps the
# effective lag between an IS-Console "Deactivate Agent" toggle and the agent
# losing access bounded to this many seconds — verified live 2026-05-19 to be
# the dominant gap (IS rejects fresh mints for deactivated agents but does not
# invalidate already-issued JWTs). See memory: project_introspection_deferred.
ACTOR_TOKEN_CACHE_MAX_TTL_SECONDS: int = 10


# ── Credentials ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgentCredentials:
    """The 4-value tuple that fully identifies one agent (F-03: 4-value-per-agent rule).

    All four values come from Asgardeo Console → Agents + its auto-created OAuth App.

    Attributes:
        agent_id: UUID assigned to the agent identity; used as ``username`` on
            ``POST /oauth2/authn``.
        agent_secret: Agent password (high-value secret — never log raw).
        oauth_client_id: Client ID of the agent's OAuth Application (the App that
            has App-Native Authentication enabled).
        oauth_client_secret: Corresponding OAuth client secret.
        redirect_uri: Registered redirect URI.  App-Native Auth uses
            ``response_mode=direct`` so the browser never visits this URL, but IS
            still validates it against the registered value.
    """

    agent_id: str
    agent_secret: str
    oauth_client_id: str
    oauth_client_secret: str
    redirect_uri: str = "http://localhost:9999/agent-callback"


# ── PKCE helpers ───────────────────────────────────────────────────────────────


def _pkce_pair() -> tuple[str, str]:
    """Generate a fresh PKCE (verifier, challenge_S256) pair.

    Matches ``idp_capability_test/c4_app_native_authn.py:pkce_pair`` exactly:
    - Verifier  : URL-safe base64 encoding of 32 random bytes, padding stripped.
    - Challenge : SHA-256 of the verifier bytes, base64url-encoded, padding stripped.

    Returns:
        A ``(verifier, challenge)`` tuple; both are ASCII strings.
    """
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── Authenticator selection ────────────────────────────────────────────────────


def _pick_local_basic_authenticator(authenticators: list) -> str | None:
    """Return the authenticatorId of the LOCAL username+password authenticator.

    When an IS app has multiple login options (e.g. UAE Pass + local Basic Auth),
    the agent must use the LOCAL authenticator — sending credentials to a federated
    IdP connector (UAE Pass) would loop forever on INCOMPLETE.

    Selection priority:
    1. idp == "LOCAL" and authenticator name contains "Basic" or "Username"
    2. Any idp == "LOCAL" authenticator
    3. First authenticator in the list (fallback for single-option flows)
    """
    if not authenticators:
        return None
    for a in authenticators:
        if a.get("idp") == "LOCAL" and any(
            kw in (a.get("authenticator") or "") for kw in ("Basic", "Username", "Password")
        ):
            return a.get("authenticatorId")
    for a in authenticators:
        if a.get("idp") == "LOCAL":
            return a.get("authenticatorId")
    return authenticators[0].get("authenticatorId")


# ── Provider ───────────────────────────────────────────────────────────────────


@dataclass
class ActorTokenProvider:
    """Mints and caches an agent's own I4 actor-token via the 3-step App-Native Auth flow.

    One instance is created per specialist process at startup and injected into
    the CIBA orchestrator and Pattern C exchange helpers.

    The provider is **single-flight**: if multiple coroutines call
    ``ensure_valid_token()`` concurrently while no valid cached token exists,
    only one IS round-trip is made.  All other callers wait on the
    ``asyncio.Lock`` and receive the freshly-minted token.

    Args:
        credentials: The agent's 4-value credential tuple.
        is_client: Pre-built ``WSO2ISClient`` (injected; allows mocking in tests).
        buffer_seconds: Re-mint when fewer than this many seconds remain before
            expiry.  Defaults to 30 s (aligned with the project-wide
            ``REFRESH_BUFFER_SECONDS`` constant).
        scope: OAuth scope string requested on ``POST /oauth2/authorize``.
    """

    credentials: AgentCredentials
    is_client: WSO2ISClient
    buffer_seconds: int = REFRESH_BUFFER_SECONDS
    scope: str = "openid internal_login"

    # Internal cache — NOT constructor arguments.
    _cached: OAuthToken | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def ensure_valid_token(self) -> OAuthToken:
        """Return a valid actor-token, re-minting if expired or within the buffer window.

        Uses a double-checked locking pattern so that, under concurrent load,
        only one coroutine executes the 3-step IS flow while the rest wait and
        then reuse the result.

        Returns:
            A valid :class:`~common.auth.models.OAuthToken`.

        Raises:
            ActorTokenError: If any step of the App-Native Auth flow fails.
        """
        # Fast path: cached token still fresh — no lock needed.
        if self._is_fresh(self._cached):
            remaining = int(
                (self._cached.expires_at - datetime.now(tz=timezone.utc)).total_seconds()  # type: ignore[union-attr]
            )
            logger.debug(
                "actor_token_cache_hit | expires_in=%ds | agent_id=%s",
                remaining,
                self.credentials.agent_id,
            )
            return self._cached  # type: ignore[return-value]

        # Slow path: acquire lock, re-check, then mint.
        async with self._lock:
            # Another coroutine may have minted while we waited for the lock.
            if self._is_fresh(self._cached):
                logger.debug(
                    "actor_token_cache_hit_after_lock | agent_id=%s",
                    self.credentials.agent_id,
                )
                return self._cached  # type: ignore[return-value]

            self._cached = await self._mint()
            return self._cached

    async def force_refresh(self) -> OAuthToken:
        """Bypass the cache and mint a fresh token unconditionally.

        Used by ``ciba_client`` retry logic after a 401 from the CIBA endpoint
        indicates the current actor-token is no longer accepted by IS.

        Returns:
            A freshly-minted :class:`~common.auth.models.OAuthToken`.

        Raises:
            ActorTokenError: If the mint flow fails.
        """
        async with self._lock:
            logger.info(
                "actor_token_force_refresh | agent_id=%s",
                self.credentials.agent_id,
            )
            self._cached = await self._mint()
            return self._cached

    def invalidate(self) -> None:
        """Drop the cached token.

        The next call to ``ensure_valid_token()`` will trigger a fresh mint.
        Call this after receiving a 401 from a downstream service to ensure
        the stale token is not reused.
        """
        logger.info(
            "actor_token_invalidated | agent_id=%s",
            self.credentials.agent_id,
        )
        self._cached = None

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _is_fresh(self, token: OAuthToken | None) -> bool:
        """Return True if *token* is non-None and not within ``buffer_seconds`` of expiry."""
        if token is None:
            return False
        from datetime import timedelta

        return datetime.now(tz=timezone.utc) < (
            token.expires_at - timedelta(seconds=self.buffer_seconds)
        )

    async def _mint(self) -> OAuthToken:
        """Execute the 3-step App-Native Auth flow and return a fresh OAuthToken.

        Steps:
            1. ``POST /oauth2/authorize`` with PKCE challenge → ``flowId`` +
               ``authenticatorId``.
            2. ``POST /oauth2/authn`` with ``agent_id``/``agent_secret`` → auth code.
            3. ``POST /oauth2/token`` with auth code + PKCE verifier → access token.

        Raises:
            ActorTokenError: Wraps any IS error with an ``ERR-CIBA-009`` error ID
                and a ``details`` dict for structured logging.
        """
        creds = self.credentials
        verifier, challenge = _pkce_pair()

        logger.info(
            "actor_token_mint_start | agent_id=%s scope=%r",
            creds.agent_id,
            self.scope,
        )

        # ── Step 1: /oauth2/authorize ──────────────────────────────────────────
        try:
            authorize_body = await self.is_client.post_authorize(
                client_id=creds.oauth_client_id,
                client_secret=creds.oauth_client_secret,
                redirect_uri=creds.redirect_uri,
                scope=self.scope,
                code_challenge=challenge,
                code_challenge_method="S256",
                response_mode="direct",
            )
        except Exception as exc:
            raise ActorTokenError(
                f"App-Native Auth /authorize failed: {exc}",
                details={"step": "authorize", "upstream": str(exc)},
            ) from exc

        flow_id: str | None = authorize_body.get("flowId")
        next_step: dict = authorize_body.get("nextStep") or {}
        authenticators: list = next_step.get("authenticators") or []
        # When the app has multiple login options (e.g. UAE Pass + local Basic Auth),
        # prefer the LOCAL username+password authenticator so agent credentials
        # are never sent to a federated IdP connector.
        authenticator_id: str | None = _pick_local_basic_authenticator(authenticators)

        code: str | None = None

        # ── Step 1.5: Short-circuit when IS already accepted the agent ────────
        # IS 7.3 may return flowStatus=SUCCESS_COMPLETED with `authData.code` on
        # /oauth2/authorize itself when a prior IS session for this OAuth client
        # is still valid (e.g. another recent mint by the same agent). No flowId
        # / authenticatorId is present in that case — there is no Step 2 to run.
        # Detected live on 2026-05-19 once the actor-token cache TTL was capped
        # at 10s and re-mints became frequent enough to hit this branch.
        short_circuit_status = authorize_body.get("flowStatus")
        short_circuit_code = (authorize_body.get("authData") or {}).get("code")
        if short_circuit_status == "SUCCESS_COMPLETED" and short_circuit_code:
            logger.debug(
                "actor_token_authorize_short_circuit | agent_id=%s — IS returned code "
                "directly, skipping /authn",
                creds.agent_id,
            )
            code = short_circuit_code
        else:
            if not flow_id or not authenticator_id:
                logger.warning(
                    "actor_token_authorize_missing_fields | agent_id=%s body=%r",
                    creds.agent_id,
                    {k: v for k, v in authorize_body.items() if k != "access_token"},
                )
                raise ActorTokenError(
                    "POST /oauth2/authorize returned 200 but flowId or authenticatorId is absent",
                    details={
                        "step": "authorize",
                        "flow_id": flow_id,
                        "authenticator_id": authenticator_id,
                    },
                )

            logger.debug(
                "actor_token_authorize_ok | flow_id=%s authenticator_id=%s",
                flow_id,
                authenticator_id,
            )

            # ── Step 2: /oauth2/authn — loop to handle multi-step login flows ─
            # IS 7.3.0 defaults to Identifier First (2 steps: identifier → password).
            # Each INCOMPLETE response carries the next authenticatorId; we loop
            # until we receive a code or a terminal failure.
            _MAX_AUTHN_STEPS = 4
            current_authenticator_id = authenticator_id

            for _step in range(_MAX_AUTHN_STEPS):
                try:
                    body = await self.is_client.post_authn_raw(
                        flow_id=flow_id,
                        authenticator_id=current_authenticator_id,
                        params={"username": creds.agent_id, "password": creds.agent_secret},
                    )
                except Exception as exc:
                    raise ActorTokenError(
                        f"App-Native Auth /authn failed: {exc}",
                        details={"step": "authn", "upstream": str(exc)},
                    ) from exc

                code = (body.get("authData") or {}).get("code") or body.get("code")
                if code:
                    break

                flow_status = body.get("flowStatus", "UNKNOWN")
                next_step: dict = body.get("nextStep") or {}
                messages: list = next_step.get("messages") or []
                err_msgs = [
                    f"{m.get('messageId', '?')}: {m.get('message', '')}"
                    for m in messages
                    if isinstance(m, dict) and m.get("type") == "ERROR"
                ]

                is_credential_failure = flow_status == "FAIL_INCOMPLETE" or any(
                    "ABA-60003" in m or "login.fail" in m for m in err_msgs
                )
                if is_credential_failure:
                    hint = (
                        " — agent authentication FAILED; the agent secret is likely "
                        "stale (rotated by 'Regenerate' in IS Console). Update the "
                        "agent's *_AGENT_SECRET in the service .env and recreate the "
                        "container."
                    )
                    logger.error(
                        "post_authn no-code | flowStatus=%s errors=%s%s",
                        flow_status, err_msgs, hint,
                    )
                    raise ActorTokenError(
                        f"POST /oauth2/authn returned flowStatus={flow_status}"
                        + (f" errors={err_msgs}" if err_msgs else "")
                        + hint,
                        details={"step": "authn", "flowStatus": flow_status, "errors": err_msgs},
                    )

                if flow_status == "INCOMPLETE":
                    # Non-error incomplete — advance to the next authenticator step.
                    next_authenticators: list = next_step.get("authenticators") or []
                    if not next_authenticators:
                        raise ActorTokenError(
                            f"POST /oauth2/authn INCOMPLETE but no next authenticator (step {_step + 1})",
                            details={"step": "authn", "flowStatus": flow_status, "body": str(body)[:500]},
                        )
                    next_id = _pick_local_basic_authenticator(next_authenticators)
                    if not next_id:
                        raise ActorTokenError(
                            f"POST /oauth2/authn INCOMPLETE but no LOCAL authenticator found (step {_step + 1})",
                            details={"step": "authn", "flowStatus": flow_status, "body": str(body)[:500]},
                        )
                    current_authenticator_id = next_id
                    logger.debug(
                        "actor_token_authn_step | step=%d next_authenticator=%s",
                        _step + 1, current_authenticator_id,
                    )
                    continue

                # Unknown terminal status with no code.
                raise ActorTokenError(
                    f"POST /oauth2/authn returned flowStatus={flow_status} with no code",
                    details={"step": "authn", "flowStatus": flow_status, "errors": err_msgs},
                )
            else:
                raise ActorTokenError(
                    f"POST /oauth2/authn did not complete after {_MAX_AUTHN_STEPS} steps",
                    details={"step": "authn"},
                )

        if not code:
            raise ActorTokenError(
                "POST /oauth2/authn returned no authorization code",
                details={"step": "authn"},
            )

        logger.debug("actor_token_authn_ok | code_len=%d", len(code))

        # ── Step 3: /oauth2/token ──────────────────────────────────────────────
        try:
            token = await self.is_client.exchange_code(
                client_id=creds.oauth_client_id,
                client_secret=creds.oauth_client_secret,
                code=code,
                code_verifier=verifier,
                redirect_uri=creds.redirect_uri,
                # No actor_token here — this IS the agent's own I4 token mint
            )
        except ActorTokenError:
            raise
        except Exception as exc:
            raise ActorTokenError(
                f"App-Native Auth /token exchange failed: {exc}",
                details={"step": "token_exchange", "upstream": str(exc)},
            ) from exc

        if not token.access_token:
            raise ActorTokenError(
                "POST /oauth2/token returned 200 but access_token is absent",
                details={"step": "token_exchange"},
            )

        logger.info(
            "actor_token_mint_ok | agent_id=%s expires_in=%ds scope=%r",
            creds.agent_id,
            token.expires_in,
            token.scope,
        )

        # Cap the cache's view of expiry so deactivation lag is bounded.
        # The underlying JWT still has its IS-issued exp claim (~1 hour) and
        # remains valid downstream; this only forces our cache to re-mint via
        # IS every ACTOR_TOKEN_CACHE_MAX_TTL_SECONDS, giving IS the chance to
        # refuse for deactivated agents.
        capped_expires_at = datetime.now(tz=timezone.utc) + timedelta(
            seconds=ACTOR_TOKEN_CACHE_MAX_TTL_SECONDS
        )
        if capped_expires_at < token.expires_at:
            token = dataclasses.replace(token, expires_at=capped_expires_at)
        return token
