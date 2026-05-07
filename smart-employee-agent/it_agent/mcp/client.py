"""Async HTTP client for it_server's MCP-style tool endpoints.

Sprint 1: plain HTTP POST per-tool with Bearer token-B.
Sprint 2: switch to MCP protocol via langchain-mcp-adapters (out of scope here).

Design notes (sprint-1-fixes.md F-09, F-12):
    - No os.getenv calls here; all configuration is injected via ITMcpClientConfig.
    - This is a pure runtime dataclass + class; no Pydantic — it never crosses an
      HTTP boundary directly (the caller serialises the result dict).
    - X-Request-ID priority: explicit param > ContextVar (get_request_id()) > uuid4().

Token-B flow (UC-02 step 14, IT variant):
    After the CIBA polling loop delivers token-B (OAuthToken), the ciba/orchestrator
    calls this client with that token. The client attaches it as a Bearer header and
    posts to the it_server endpoint. it_server performs its own JWT validation
    (aud + act.sub + scope) before executing the tool — see api-contracts.md §4.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx

from common.auth.models import OAuthToken
from common.logging.correlation import get_request_id

__all__ = ["ITMcpClientConfig", "ITMcpClient"]


# ── Config ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ITMcpClientConfig:
    """Injected configuration for ITMcpClient.

    Attributes:
        base_url: Base URL of the it_server process,
                  e.g. ``http://it_server:8004``. No trailing slash.
        timeout_seconds: Per-request HTTP timeout. Defaults to 30 s.
    """

    base_url: str
    timeout_seconds: float = 30.0


# ── Client ────────────────────────────────────────────────────────────────────


class ITMcpClient:
    """Async HTTP client for it_server's MCP-style tool endpoints.

    Sprint 1: plain HTTP POST per-tool with Bearer token-B.
    Sprint 2: switch to MCP protocol via langchain-mcp-adapters (out of scope here).

    Lifecycle::

        config = ITMcpClientConfig(base_url="http://it_server:8004")
        client = ITMcpClient(config)
        result = await client.list_available_assets(token_b=obo_token.raw)
        await client.aclose()

    Alternatively, pass your own ``httpx.AsyncClient`` (e.g., from a test fixture)
    to avoid creating an internal client — in that case ``aclose()`` is a no-op for
    the injected client.

    Header rules applied on every request:
        - ``Authorization: Bearer <token_b.access_token>``
        - ``Content-Type: application/json``  (POST body)
        - ``X-Request-ID``: uses explicit ``request_id`` arg first, then
          ``get_request_id()`` from the ContextVar, then a fresh UUID4.
    """

    def __init__(
        self,
        config: ITMcpClientConfig,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the client.

        Args:
            config: Injected URL and timeout configuration.
            http: Optional pre-built ``httpx.AsyncClient``. When *None*, an
                  internal client is created and owned by this instance (it is
                  closed by :meth:`aclose`). When provided, the caller retains
                  ownership and ``aclose()`` does NOT close it.
        """
        self._config = config
        self._owned: bool = http is None
        self._http: httpx.AsyncClient = http or httpx.AsyncClient(
            timeout=config.timeout_seconds
        )

    # ── Header helpers ────────────────────────────────────────────────────────

    def _build_headers(
        self,
        token_b: OAuthToken,
        request_id: str | None,
    ) -> dict[str, str]:
        """Build the standard request headers for a tool POST.

        Args:
            token_b: The user-OBO token whose ``access_token`` is placed in
                     the ``Authorization`` header.
            request_id: Explicit override. Falls back to the ContextVar, then
                        to a freshly generated UUID4.

        Returns:
            A dict ready for use as ``httpx`` ``headers=``.
        """
        rid = request_id or get_request_id() or str(uuid.uuid4())
        return {
            "Authorization": f"Bearer {token_b.access_token}",
            "Content-Type": "application/json",
            "X-Request-ID": rid,
        }

    async def _post(
        self,
        path: str,
        *,
        token_b: OAuthToken,
        request_id: str | None,
        body: dict | None = None,
    ) -> dict:
        """Execute a POST and return the parsed JSON body.

        Args:
            path: Endpoint path relative to ``base_url``, e.g.
                  ``/mcp/tools/list_available_assets``.
            token_b: Bearer token for the ``Authorization`` header.
            request_id: Explicit ``X-Request-ID`` override (may be *None*).
            body: Optional JSON-serialisable request body dict.

        Returns:
            The response body parsed as ``dict``.

        Raises:
            httpx.HTTPStatusError: On any non-2xx HTTP response.
        """
        url = f"{self._config.base_url}{path}"
        headers = self._build_headers(token_b, request_id)
        response = await self._http.post(url, headers=headers, json=body or {})
        response.raise_for_status()
        return response.json()

    # ── Public tool methods ───────────────────────────────────────────────────

    async def list_available_assets(
        self,
        *,
        token_b: OAuthToken,
        asset_type: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST {base_url}/mcp/tools/list_available_assets`` with Bearer token-B.

        Required scope on token-B: ``it.read`` (enforced by it_server).
        Returns asset catalogue (not user-specific).

        Args:
            token_b: The user-OBO token obtained after CIBA consent.
            asset_type: Optional filter string, e.g. ``"laptop"`` | ``"monitor"``
                        | ``"phone"``. When *None*, all asset types are returned.
            request_id: Optional ``X-Request-ID`` to propagate. Falls back to
                        the ContextVar, then to a fresh UUID4.

        Returns:
            Tool result body, e.g.
            ``{"assets": [{"asset_id": "MBP-14", "model": "MacBook Pro 14",
               "type": "laptop", "available_count": 3}, ...]}``.

        Raises:
            httpx.HTTPStatusError: On non-2xx (including 401 from it_server on
                                   token validation failure — ERR-MCP-NNN).
        """
        body: dict = {}
        if asset_type is not None:
            body["asset_type"] = asset_type
        return await self._post(
            "/mcp/tools/list_available_assets",
            token_b=token_b,
            request_id=request_id,
            body=body,
        )

    async def get_my_assets(
        self,
        *,
        token_b: OAuthToken,
        employee_id: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST {base_url}/mcp/tools/get_my_assets`` with Bearer token-B.

        Required scope on token-B: ``it.read`` (enforced by it_server).
        Returns assets assigned to the requesting user (``token_b.sub``) or to
        ``employee_id`` when the caller is a manager.

        Args:
            token_b: The user-OBO token.
            employee_id: Optional employee UUID override. When *None* the
                         it_server defaults to ``token_b.sub`` (self-service).
            request_id: Optional ``X-Request-ID`` propagation override.

        Returns:
            Tool result body, e.g.
            ``{"employee_id": "...", "assets":
               [{"asset_id": "MBP-14", "model": "MacBook Pro 14",
                 "type": "laptop", "assigned_since": "2025-01-15"}]}``.

        Raises:
            httpx.HTTPStatusError: On non-2xx response.
        """
        body: dict = {}
        if employee_id is not None:
            body["employee_id"] = employee_id
        return await self._post(
            "/mcp/tools/get_my_assets",
            token_b=token_b,
            request_id=request_id,
            body=body,
        )

    async def issue_asset(
        self,
        *,
        token_b: OAuthToken,
        asset_id: str,
        employee_id: str,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST {base_url}/mcp/tools/issue_asset`` with Bearer token-B.

        Required scope on token-B: ``it_assets_write_rest`` (HR Admin only;
        enforced by it_server). D2.8 / N33 acceptance.

        Args:
            token_b: The user-OBO token (HR Admin's, with `act.sub` = it_agent).
            asset_id: Catalogue asset_id to assign, e.g. ``"MBP-14-001"``.
            employee_id: Target employee sub.
            request_id: Optional ``X-Request-ID`` propagation override.

        Returns:
            Tool result body, e.g.
            ``{"asset_id": "MBP-14-001", "employee_id": "...",
               "issued_by": "<agent UUID>", "issued_at": "<ISO-8601>"}``.

        Raises:
            httpx.HTTPStatusError: On non-2xx response.
        """
        return await self._post(
            "/mcp/tools/issue_asset",
            token_b=token_b,
            request_id=request_id,
            body={"asset_id": asset_id, "employee_id": employee_id},
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created internally.

        If an external ``httpx.AsyncClient`` was injected at construction time,
        this method is a no-op — the caller retains ownership and must close it
        themselves.
        """
        if self._owned:
            await self._http.aclose()
