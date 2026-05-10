"""Pattern C — orchestrator-side PKCE + actor_token code exchange (Sprint 1 Wave 5).

Implements the orchestrator half of the user-login flow described in
``docs/use-cases/UC-01-user-login.md`` steps 1-9 and constrained by
``docs/architecture/sprint-1-fixes.md`` F-01 and F-09.

Responsibilities
----------------
1. ``make_pkce()``         — generate a fresh RFC 7636 S256 PKCE pair.
2. ``build_authorize_url()`` — assemble the SPA → IS redirect URL.
3. ``PatternCExchanger``   — perform the backend code-exchange POST, injecting the
                             orchestrator-agent's actor_token into the **request body**
                             (F-01: actor_token is a BODY parameter, not a header).

Design constraints (sprint-1-fixes.md)
---------------------------------------
- F-01: ``actor_token`` + ``actor_token_type`` go in the POST body of
  ``POST /oauth2/token``.  The ``is_client.exchange_code()`` method already
  honours this; ``PatternCExchanger`` simply passes ``actor_token=`` kwarg.
- F-09: ``PatternCResult`` is a ``@dataclass`` (not Pydantic), because it is a
  pure runtime value never serialised over HTTP directly.  Serialisation to a
  session record happens one layer up (``auth/routes.py``).

Error policy
------------
- IS 4xx → ``AuthError`` (raised by ``WSO2ISClient.exchange_code``; propagated as-is).
- JWT validation failure → ``JWTValidationError`` (raised by ``validate``; propagated).
- Actor-token mint failure → ``ActorTokenError`` (raised by ``ActorTokenProvider``; propagated).
- Callers MUST NOT catch bare ``Exception``; catch only the specific subclasses above.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import urllib.parse
from dataclasses import dataclass

from common.auth.actor_token_provider import ActorTokenProvider
from common.auth.jwt_validator import JWKSCache, ValidatorConfig, validate
from common.auth.models import JWTClaims, OAuthToken
from common.auth.wso2_is_client import WSO2ISClient

logger = logging.getLogger(__name__)

__all__ = [
    "make_pkce",
    "build_authorize_url",
    "PatternCResult",
    "PatternCExchanger",
]


# ── PKCE helpers ───────────────────────────────────────────────────────────────


def make_pkce() -> tuple[str, str]:
    """Generate a fresh RFC 7636 S256 PKCE ``(code_verifier, code_challenge)`` pair.

    The verifier is 43 characters (32 random bytes → base64url-encoded → padding
    stripped), which satisfies the RFC 7636 §4.1 requirement of 43–128 characters.
    The challenge is the URL-safe base64 encoding (no padding) of the SHA-256 digest
    of the verifier bytes.

    Matches the shape used in ``idp_capability_test/c1_pattern_c.py:pkce_pair``
    and ``common/auth/actor_token_provider.py:_pkce_pair``.

    Returns:
        A ``(code_verifier, code_challenge)`` tuple; both values are ASCII strings.

    Example::

        verifier, challenge = make_pkce()
        # verifier is 43 chars, challenge is 43 chars (S256 of verifier)
    """
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── authorize URL builder ──────────────────────────────────────────────────────


def build_authorize_url(
    *,
    is_authorize_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    requested_actor: str,
    state: str,
    code_verifier: str,
) -> tuple[str, str]:
    """Build the ``/oauth2/authorize`` redirect URL with PKCE (S256) + ``requested_actor``.

    The caller is responsible for generating *state* (CSRF token) and *code_verifier*
    (PKCE secret) before calling this function.  Use :func:`make_pkce` for the latter.

    This function computes the ``code_challenge`` from the supplied *code_verifier*
    so that the challenge in the URL and the verifier submitted in the token exchange
    are provably related.

    Sprint 3 3B.3: this kwarg was previously named ``spa_client_id`` in
    a v3 RFC 8693 dual-client world (orchestrator-app + orchestrator-mcp-client).
    The v4 CIBA pivot collapsed onto the confidential MCP client, so the
    misleading name was renamed to ``client_id``. See memory
    ``project_orchestrator_app_vestigial.md``.

    Args:
        is_authorize_endpoint: Full URL of the IS ``/oauth2/authorize`` endpoint.
        client_id: OAuth Client ID of the orchestrator's confidential MCP
            client app (``orchestrator-mcp-client``). Used for both
            ``/authorize`` and the subsequent ``/token`` exchange — IS rejects
            cross-client code redemption, so the same client_id MUST be on
            both calls.
        redirect_uri: The SPA callback URI registered on the IS application.
        scope: Space-separated scope string (e.g. ``"openid orchestrate"``).
        requested_actor: UUID of the orchestrator-agent; tells IS which agent to embed
            as the ``act.sub`` claim in the resulting token-A.
        state: Opaque CSRF value generated by the SPA / backend; echoed by IS on
            redirect so the backend can validate it.
        code_verifier: PKCE verifier (from :func:`make_pkce`).  The S256 challenge
            is derived here.

    Returns:
        A ``(url, code_challenge)`` tuple.  *url* is the full redirect URL ready for
        the browser.  *code_challenge* is returned for reference / logging — the
        caller does NOT need to store it separately (it is derived from *code_verifier*
        on demand).

    Example::

        verifier, _ = make_pkce()
        url, challenge = build_authorize_url(
            is_authorize_endpoint="https://is.example.com/oauth2/authorize",
            client_id="orchestrator-mcp-client-id",
            redirect_uri="http://localhost:8090/agent-callback",
            scope="openid orchestrate",
            requested_actor="<orchestrator-agent-uuid>",
            state=secrets.token_urlsafe(16),
            code_verifier=verifier,
        )
    """
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    params: dict[str, str] = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "requested_actor": requested_actor,
    }
    url = f"{is_authorize_endpoint}?{urllib.parse.urlencode(params)}"
    return url, code_challenge


# ── Result dataclass ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PatternCResult:
    """Successful outcome of the Pattern C orchestrator-side code exchange.

    Boundary rule (F-09): this is a ``@dataclass``, not a Pydantic model, because
    it is a pure runtime value.  The caller (``auth/routes.py``) is responsible for
    extracting the fields it needs and storing them in a session record.

    Attributes:
        token_a: The raw ``OAuthToken`` returned by IS for the user-delegated grant.
            ``token_a.access_token`` carries ``sub=<user>``, ``act.sub=<orchestrator-agent>``.
        claims: Decoded and cryptographically verified ``JWTClaims`` for ``token_a``.
            Provides typed access to ``sub``, ``iss``, ``aud``, ``act``, etc.
    """

    token_a: OAuthToken
    claims: JWTClaims


# ── Exchanger ──────────────────────────────────────────────────────────────────


class PatternCExchanger:
    """Perform the orchestrator-side Pattern C code exchange with IS.

    On each ``exchange()`` call this class:

    1. Calls ``actor_token_provider.ensure_valid_token()`` to obtain a fresh
       (or cached) agent I4 token.
    2. Calls ``is_client.exchange_code(...)`` with ``actor_token=<agent-token>``
       in the POST body (F-01 — actor_token is NEVER sent as an Authorization header
       at this step; it is a body parameter alongside ``code``, ``code_verifier``,
       and ``redirect_uri``).
    3. Validates the resulting token-A using ``validate()``.
    4. Returns a :class:`PatternCResult` with the raw token and verified claims.

    Caching note: ``PatternCExchanger`` does NOT cache actor-tokens itself.
    That is entirely the responsibility of ``ActorTokenProvider`` (single-flight
    double-checked cache).  Each ``exchange()`` call delegates to
    ``actor_token_provider.ensure_valid_token()``, which returns the cached token
    if it is still fresh.

    Args:
        is_client: Pre-built ``WSO2ISClient`` — the low-level HTTP client for IS.
        actor_token_provider: Cached agent I4 token provider; injected for testability.
        mcp_client_id: OAuth Client ID of ``orchestrator-mcp-client``; used as
            ``client_id`` on ``POST /oauth2/token`` (Basic auth + body field).
        mcp_client_secret: Corresponding client secret.
        validator: ``ValidatorConfig`` used to cryptographically verify the
            resulting token-A.
        jwks_cache: Optional pre-built ``JWKSCache``; if ``None`` the global registry
            in ``jwt_validator.py`` is used.  Inject in tests to avoid live JWKS fetches.
    """

    def __init__(
        self,
        *,
        is_client: WSO2ISClient,
        actor_token_provider: ActorTokenProvider,
        mcp_client_id: str,
        mcp_client_secret: str,
        validator: ValidatorConfig,
        jwks_cache: JWKSCache | None = None,
    ) -> None:
        self._is_client = is_client
        self._actor_token_provider = actor_token_provider
        self._mcp_client_id = mcp_client_id
        self._mcp_client_secret = mcp_client_secret
        self._validator = validator
        self._jwks_cache = jwks_cache

    async def exchange(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> PatternCResult:
        """Exchange an authorization code for a user-delegated token-A.

        Steps:

        1. ``actor_token_provider.ensure_valid_token()`` → agent I4 ``OAuthToken``.
        2. ``is_client.exchange_code(...)`` → raw token-A ``OAuthToken``; the
           agent's ``access_token`` is passed as ``actor_token`` **in the POST body**
           (F-01 wire shape; see ``wso2_is_client.py`` for the exact form fields).
        3. ``validate(token_a.access_token, config, jwks_cache=...)`` → ``JWTClaims``.
        4. Return ``PatternCResult(token_a=..., claims=...)``.

        Args:
            code: Authorization code received in the SPA callback from IS.
            code_verifier: PKCE verifier that was used to derive the challenge sent in
                the ``/oauth2/authorize`` request.
            redirect_uri: Must exactly match the ``redirect_uri`` used during the
                authorize step.

        Returns:
            A :class:`PatternCResult` with the raw token and verified claims.

        Raises:
            ActorTokenError: If the agent I4 token cannot be minted / refreshed.
            AuthError: If IS returns a non-2xx response on ``POST /oauth2/token``.
            JWTValidationError: If token-A fails cryptographic or claims validation.
        """
        # Step 1 — obtain actor_token (cached or fresh mint via ActorTokenProvider)
        actor_token = await self._actor_token_provider.ensure_valid_token()
        logger.debug(
            "pattern_c_exchange_start | mcp_client_id=%s code_len=%d",
            self._mcp_client_id,
            len(code),
        )

        # Step 2 — POST /oauth2/token; actor_token goes in the BODY (F-01)
        token_a = await self._is_client.exchange_code(
            client_id=self._mcp_client_id,
            client_secret=self._mcp_client_secret,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            actor_token=actor_token.access_token,
        )
        logger.info(
            "pattern_c_token_received | scope=%r expires_in=%d",
            token_a.scope,
            token_a.expires_in,
        )

        # Step 3 — validate token-A cryptographically + claims checks
        claims = await validate(
            token_a.access_token,
            self._validator,
            jwks_cache=self._jwks_cache,
        )
        logger.info(
            "pattern_c_token_validated | sub=%s act_sub=%s",
            claims.sub,
            (claims.act or {}).get("sub"),
        )

        return PatternCResult(token_a=token_a, claims=claims)
