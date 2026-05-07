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
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .errors import ActorTokenError
from .models import OAuthToken
from .wso2_is_client import WSO2ISClient

logger = logging.getLogger(__name__)

REFRESH_BUFFER_SECONDS: int = 30


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
        authenticator_id: str | None = (
            authenticators[0].get("authenticatorId") if authenticators else None
        )

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

        # ── Step 2: /oauth2/authn ──────────────────────────────────────────────
        try:
            code = await self.is_client.post_authn(
                flow_id=flow_id,
                authenticator_id=authenticator_id,
                params={"username": creds.agent_id, "password": creds.agent_secret},
            )
        except ActorTokenError:
            raise
        except Exception as exc:
            raise ActorTokenError(
                f"App-Native Auth /authn failed: {exc}",
                details={"step": "authn", "upstream": str(exc)},
            ) from exc

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
        return token
