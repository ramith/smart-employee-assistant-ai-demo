"""Low-level async HTTP client for WSO2 Identity Server OAuth 2.0 endpoints.

Covers:
- App-Native Auth 3-step flow  (/oauth2/authorize → /oauth2/authn → /oauth2/token)
- Pattern C code exchange       (/oauth2/token with actor_token in BODY, per F-01 / C1 finding)
- Client-credentials grant      (/oauth2/token)
- JWKS fetch                    (/oauth2/jwks)

Boundary rules (F-09):
- This module is pure HTTP + parsing — NO token caching (see actor_token_provider.py)
  and NO CIBA (see ciba_client.py).
- WSO2ISClientConfig is a frozen dataclass (not Pydantic) because it carries no HTTP
  boundary concern.

Wire shapes are verified against live WSO2 IS 7.2 probes C1 and C4; see
docs/architecture/api-contracts.md §5 for the full normative spec.

Exception policy:
- Non-2xx from /oauth2/authorize or /oauth2/authn raises ``CIBAInitiationError``
  (imported from ``common.auth.errors``; error_id ERR-CIBA-001 by default).
- Non-2xx from /oauth2/token raises ``AuthError`` directly.
- All exceptions are specific subtypes — callers MUST NOT catch bare ``Exception``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import AuthError, CIBAInitiationError
from .models import OAuthToken

logger = logging.getLogger(__name__)


# ── Config ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WSO2ISClientConfig:
    """Immutable configuration for a WSO2 IS instance.

    Attributes:
        base_url: Root URL of the WSO2 IS instance, e.g. ``https://13.60.190.47:9443``.
            No trailing slash.
        insecure_tls: When ``True``, TLS certificate verification is disabled.
            Use only in dev environments with self-signed certs.
    """

    base_url: str
    insecure_tls: bool = False


# ── Client ─────────────────────────────────────────────────────────────────────


class WSO2ISClient:
    """Async helpers for WSO2 IS ``/oauth2/{authorize,authn,token,jwks}`` endpoints.

    Stateless — no token or JWKS caching; those concerns live in higher layers
    (``actor_token_provider.py`` for token caching, ``jwt_validator.py`` for JWKS
    caching).

    Ownership model:
        If *http* is ``None`` (default), the client creates an ``httpx.AsyncClient``
        internally and takes ownership — ``aclose()`` will close it.  If a client is
        injected, the caller owns it and ``aclose()`` is a no-op on that client.

    Args:
        config: WSO2IS connection parameters.
        http: Optional pre-built ``httpx.AsyncClient``.  Injected in tests.
    """

    def __init__(
        self,
        config: WSO2ISClientConfig,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_http: bool = http is None
        self._http: httpx.AsyncClient = http or httpx.AsyncClient(
            verify=not config.insecure_tls,
            headers={"Accept": "application/json"},
        )

    # ── URL properties ─────────────────────────────────────────────────────────

    @property
    def authorize_url(self) -> str:
        """Full URL for ``POST /oauth2/authorize`` (App-Native Auth step 1)."""
        return f"{self._config.base_url}/oauth2/authorize"

    @property
    def authn_url(self) -> str:
        """Full URL for ``POST /oauth2/authn`` (App-Native Auth step 2)."""
        return f"{self._config.base_url}/oauth2/authn"

    @property
    def token_url(self) -> str:
        """Full URL for ``POST /oauth2/token`` (code exchange, client_credentials)."""
        return f"{self._config.base_url}/oauth2/token"

    @property
    def jwks_url(self) -> str:
        """Full URL for ``GET /oauth2/jwks``."""
        return f"{self._config.base_url}/oauth2/jwks"

    @property
    def issuer(self) -> str:
        """Issuer string.

        WSO2 IS sets ``iss`` equal to the token endpoint URL (verified in C4 probe).
        """
        return self.token_url

    # ── App-Native Auth — step 1 ───────────────────────────────────────────────

    async def post_authorize(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
        response_mode: str = "direct",
    ) -> dict[str, Any]:
        """POST ``/oauth2/authorize`` for App-Native Auth (``response_mode=direct``).

        Sends Basic-auth credentials and the PKCE challenge.  IS responds with a
        ``flowId`` and the ``nextStep.authenticators`` list so the caller can pick
        the ``authenticatorId`` for step 2.

        Args:
            client_id: Agent App OAuth Client ID.
            client_secret: Agent App OAuth Client Secret.
            redirect_uri: Registered redirect URI (e.g. ``http://localhost:9999/agent-callback``).
            scope: Space-separated scope string (e.g. ``openid internal_login``).
            code_challenge: S256-hashed PKCE challenge.
            code_challenge_method: Always ``"S256"`` in practice; kept as a parameter
                to allow override in tests.
            response_mode: Must be ``"direct"`` for App-Native Auth.

        Returns:
            Parsed JSON body from IS: ``{"flowId": "...", "nextStep": {...}}``.

        Raises:
            CIBAInitiationError: If IS returns a non-2xx status code.
        """
        response = await self._http.post(
            self.authorize_url,
            auth=httpx.BasicAuth(client_id, client_secret),
            data={
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": scope,
                "response_mode": response_mode,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
            },
        )
        if response.status_code != 200:
            logger.error(
                "post_authorize failed | status=%d body=%s",
                response.status_code,
                response.text[:500],
            )
            raise CIBAInitiationError(
                f"POST /oauth2/authorize returned HTTP {response.status_code}",
                details={"http_status": response.status_code, "body": response.text[:500]},
            )
        return response.json()

    # ── App-Native Auth — step 2 (raw) ────────────────────────────────────────

    async def post_authn_raw(
        self,
        *,
        flow_id: str,
        authenticator_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """POST ``/oauth2/authn`` and return the raw response body.

        Unlike :meth:`post_authn`, this method does **not** inspect ``flowStatus``
        or extract the authorization code — it simply returns the parsed JSON body
        so callers can handle multi-step login flows (e.g. Identifier First +
        Basic Auth) by looping until ``flowStatus`` reaches ``SUCCESS_COMPLETED``.

        Raises:
            CIBAInitiationError: If IS returns a non-2xx status code.
        """
        response = await self._http.post(
            self.authn_url,
            json={
                "flowId": flow_id,
                "selectedAuthenticator": {
                    "authenticatorId": authenticator_id,
                    "params": params,
                },
            },
        )
        if response.status_code != 200:
            logger.error(
                "post_authn_raw failed | status=%d body=%s",
                response.status_code,
                response.text[:500],
            )
            raise CIBAInitiationError(
                f"POST /oauth2/authn returned HTTP {response.status_code}",
                details={"http_status": response.status_code, "body": response.text[:500]},
            )
        return response.json()

    # ── App-Native Auth — step 2 ───────────────────────────────────────────────

    async def post_authn(
        self,
        *,
        flow_id: str,
        authenticator_id: str,
        params: dict[str, Any],
    ) -> str:
        """POST ``/oauth2/authn`` with agent credentials to complete App-Native Auth.

        Sends the ``flowId`` and ``selectedAuthenticator`` block as JSON (verified
        empirically in C4 — IS requires ``Content-Type: application/json`` here,
        not form-encoded).

        Handles two IS response shapes for the auth code:
        - ``{"authData": {"code": "<code>"}}``: standard WSO2 IS 7.2 shape (C4/C8).
        - ``{"code": "<code>"}``: top-level fallback seen on some IS versions.

        Args:
            flow_id: The ``flowId`` value from step 1.
            authenticator_id: The ``authenticatorId`` from ``nextStep.authenticators[0]``.
            params: Credential dict, e.g. ``{"username": "agent-uuid", "password": "..."}``

        Returns:
            The authorization code string.

        Raises:
            CIBAInitiationError: If IS returns a non-2xx response or the code is absent.
        """
        response = await self._http.post(
            self.authn_url,
            json={
                "flowId": flow_id,
                "selectedAuthenticator": {
                    "authenticatorId": authenticator_id,
                    "params": params,
                },
            },
        )
        if response.status_code != 200:
            logger.error(
                "post_authn failed | status=%d body=%s",
                response.status_code,
                response.text[:500],
            )
            raise CIBAInitiationError(
                f"POST /oauth2/authn returned HTTP {response.status_code}",
                details={"http_status": response.status_code, "body": response.text[:500]},
            )

        body: dict[str, Any] = response.json()
        # Handle both response shapes (api-contracts.md §5.2, C8 line 107)
        code: str | None = (
            (body.get("authData") or {}).get("code")
            or body.get("code")
        )
        if not code:
            # WSO2 IS App-Native Auth returns HTTP 200 even when the
            # credentials are rejected — the failure is in flowStatus +
            # nextStep.messages. Surface that instead of the cryptic
            # "no code in response body" so operators can immediately tell
            # "the agent secret is wrong / was regenerated" from a log line.
            flow_status = body.get("flowStatus", "UNKNOWN")
            messages = (body.get("nextStep") or {}).get("messages") or []
            err_msgs = [
                f"{m.get('messageId', '?')}: {m.get('message', '')}"
                for m in messages
                if isinstance(m, dict) and m.get("type") == "ERROR"
            ]
            is_credential_failure = flow_status == "FAIL_INCOMPLETE" or any(
                "ABA-60003" in m or "login.fail" in m for m in err_msgs
            )
            hint = (
                " — agent authentication FAILED; the agent secret is likely "
                "stale (rotated by 'Regenerate' in IS Console). Update the "
                "agent's *_AGENT_SECRET in the service .env and recreate the "
                "container."
                if is_credential_failure
                else ""
            )
            logger.error(
                "post_authn no-code | flowStatus=%s errors=%s%s",
                flow_status,
                err_msgs or "(none)",
                hint,
            )
            raise CIBAInitiationError(
                f"POST /oauth2/authn returned flowStatus={flow_status}"
                + (f" errors={err_msgs}" if err_msgs else "")
                + hint,
                details={
                    "http_status": response.status_code,
                    "flowStatus": flow_status,
                    "errors": err_msgs,
                    "body": response.text[:500],
                },
            )
        return code

    # ── /oauth2/token — authorization_code (with optional actor_token) ─────────

    async def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        actor_token: str | None = None,
    ) -> OAuthToken:
        """Exchange an authorization code for a token via ``grant_type=authorization_code``.

        Implements Pattern C: when *actor_token* is provided it is sent in the POST
        **body** as ``actor_token`` + ``actor_token_type``, NOT in the Authorization
        header.  This is the empirically verified shape from C1 probe (P10.B finding
        / F-01 in sprint-1-fixes.md).

        Args:
            client_id: OAuth Client ID of the exchanging app (e.g. orchestrator-mcp-client).
            client_secret: Corresponding client secret.
            code: Authorization code from the callback.
            code_verifier: Original PKCE verifier string.
            redirect_uri: Must match the redirect_uri used in the authorize step.
            actor_token: Agent I4 token.  When present, included in the POST body
                as ``actor_token`` with type
                ``urn:ietf:params:oauth:token-type:access_token``.

        Returns:
            An :class:`~common.auth.models.OAuthToken` parsed from the IS response.

        Raises:
            AuthError: If IS returns a non-2xx status (e.g. ``invalid_grant``).
        """
        form_data: dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        }
        if actor_token is not None:
            form_data["actor_token"] = actor_token
            form_data["actor_token_type"] = (
                "urn:ietf:params:oauth:token-type:access_token"
            )

        response = await self._http.post(
            self.token_url,
            auth=httpx.BasicAuth(client_id, client_secret),
            data=form_data,
        )
        if not response.is_success:
            logger.error(
                "exchange_code failed | status=%d body=%s",
                response.status_code,
                response.text[:500],
            )
            raise AuthError(
                f"POST /oauth2/token (auth_code) returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        return OAuthToken.from_response(response.json())

    # ── /oauth2/token — client_credentials ────────────────────────────────────

    async def client_credentials(
        self,
        *,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
    ) -> OAuthToken:
        """Obtain a token using ``grant_type=client_credentials``.

        Useful for health-checks and internal service bootstrapping that do not
        require a user context.

        Args:
            client_id: OAuth Client ID.
            client_secret: Corresponding client secret.
            scope: Optional space-separated scope string.  Omitted from the request
                if ``None`` (IS will use the app's default scopes).

        Returns:
            An :class:`~common.auth.models.OAuthToken` parsed from the IS response.

        Raises:
            AuthError: If IS returns a non-2xx status.
        """
        form_data: dict[str, str] = {"grant_type": "client_credentials"}
        if scope is not None:
            form_data["scope"] = scope

        response = await self._http.post(
            self.token_url,
            auth=httpx.BasicAuth(client_id, client_secret),
            data=form_data,
        )
        if not response.is_success:
            logger.error(
                "client_credentials failed | status=%d body=%s",
                response.status_code,
                response.text[:500],
            )
            raise AuthError(
                f"POST /oauth2/token (client_credentials) returned HTTP "
                f"{response.status_code}: {response.text[:200]}"
            )
        return OAuthToken.from_response(response.json())

    # ── JWKS fetch ─────────────────────────────────────────────────────────────

    async def fetch_jwks(self, jwks_url: str | None = None) -> dict[str, Any]:
        """Fetch the JWKS document from IS.

        Args:
            jwks_url: Override URL.  Defaults to :attr:`jwks_url` (i.e.
                ``{base_url}/oauth2/jwks``).

        Returns:
            Parsed JWKS JSON dict.

        Raises:
            AuthError: If IS returns a non-2xx status.
        """
        url = jwks_url or self.jwks_url
        response = await self._http.get(url)
        if not response.is_success:
            raise AuthError(
                f"GET {url} returned HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` if owned by this instance.

        If the client was injected (e.g. in tests), this is a no-op.
        """
        if self._owns_http:
            await self._http.aclose()
