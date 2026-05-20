"""orchestrator/auth/is_revoke.py — wrapper for WSO2 IS POST /oauth2/revoke.

Sprint 3 3A.1 (G-1 deliverable). Used by ``logout_handler`` to revoke token-A
when the user signs out.

F-21 implication (see ``docs/spikes/sprint-3-is-source-analysis.md`` §3):
revoking token-A at IS does NOT propagate to CIBA-issued OBO tokens. This
module only kills token-A's IS-side validity. Receiver-side denylist
fan-out (3A.2) is the only revocation primitive for token-B / token-C.

Boundary rules
--------------
- F-09: this module exposes a callable, not a Pydantic model. Internal HTTP
  client is a stateful object held by ``RevokeClient``.
- The orchestrator-mcp-client confidential client authenticates the call
  via HTTP Basic auth — same shape as ``PatternCExchanger``.
- The IS cert is self-signed in dev; ``DISABLE_SSL_VERIFY=1`` in env mirrors
  the existing pattern (see ``common/auth/wso2_is_client.py``).
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

__all__ = ["RevokeClient", "RevokeError"]


class RevokeError(Exception):
    """Raised when /oauth2/revoke returns a non-2xx response.

    The revocation cascade is best-effort — callers (``logout_handler``)
    log the error and proceed. F-21 means receiver-side denylist is the
    real defense; IS-side revoke is defense-in-depth.
    """


class RevokeClient:
    """Async client for WSO2 IS ``POST /oauth2/revoke``.

    One instance per orchestrator app (created in ``main.py`` lifespan).
    Uses ``httpx.AsyncClient`` with self-signed TLS allowed in dev.

    Attributes:
        is_base_url: WSO2 IS base URL, e.g. ``https://13.60.190.47:9443``.
        client_id: Confidential client_id (orchestrator-mcp-client) for basic auth.
        client_secret: Corresponding secret.
        verify_tls: Whether to verify the IS TLS cert. ``False`` in dev where
            IS uses a self-signed cert.
        _http: Lazily-initialised async HTTP client.
    """

    def __init__(
        self,
        *,
        is_base_url: str,
        client_id: str,
        client_secret: str,
        verify_tls: bool = False,
    ) -> None:
        self.is_base_url = is_base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.verify_tls = verify_tls
        self._http: httpx.AsyncClient | None = None

    @property
    def revoke_url(self) -> str:
        return f"{self.is_base_url}/oauth2/revoke"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(verify=self.verify_tls, timeout=10.0)
        return self._http

    async def revoke_access_token(self, access_token: str, *, request_id: str | None = None) -> None:
        """Revoke an access token at IS via /oauth2/revoke.

        Returns ``None`` on 2xx; raises ``RevokeError`` otherwise. Empty body on
        2xx is normal per RFC 7009.

        Args:
            access_token: The token to revoke.
            request_id: Optional rid for log correlation.

        Raises:
            RevokeError: When IS returns non-2xx. Caller should log and proceed.
        """
        client = await self._get_client()
        logger.debug(
            "is_revoke_call | rid=%s endpoint=%s",
            request_id,
            self.revoke_url,
        )
        try:
            resp = await client.post(
                self.revoke_url,
                auth=(self.client_id, self.client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"token": access_token, "token_type_hint": "access_token"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "is_revoke_network_error | rid=%s err=%s",
                request_id,
                exc,
            )
            raise RevokeError(f"Network error calling /oauth2/revoke: {exc}") from exc

        if resp.status_code in (200, 204):
            logger.info(
                "token_a_revoked | rid=%s status=%d",
                request_id,
                resp.status_code,
            )
            return

        body_preview = resp.text[:200] if resp.text else ""
        logger.warning(
            "is_revoke_non_2xx | rid=%s status=%d body=%r",
            request_id,
            resp.status_code,
            body_preview,
        )
        raise RevokeError(
            f"/oauth2/revoke returned HTTP {resp.status_code}: {body_preview}"
        )

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
