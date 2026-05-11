"""Async HTTP client for hr_server's MCP-style tool endpoints.

Sprint 1: plain HTTP POST per-tool with Bearer token-B.
Sprint 2: switch to MCP protocol via langchain-mcp-adapters (out of scope here).

Design notes (sprint-1-fixes.md F-09, F-12):
    - No os.getenv calls here; all configuration is injected via HRMcpClientConfig.
    - This is a pure runtime dataclass + class; no Pydantic — it never crosses an
      HTTP boundary directly (the caller serialises the result dict).
    - X-Request-ID priority: explicit param > ContextVar (get_request_id()) > uuid4().

Token-B flow (UC-02 step 14):
    After the CIBA polling loop delivers token-B (OAuthToken), the ciba/orchestrator
    calls this client with that token. The client attaches it as a Bearer header and
    posts to the hr_server endpoint. hr_server performs its own JWT validation
    (aud + act.sub + scope) before executing the tool — see api-contracts.md §4.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import httpx

from common.auth.models import OAuthToken
from common.logging.correlation import get_request_id

__all__ = ["HRMcpClientConfig", "HRMcpClient"]


# ── Config ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HRMcpClientConfig:
    """Injected configuration for HRMcpClient.

    Attributes:
        base_url: Base URL of the hr_server process,
                  e.g. ``http://hr_server:8000``. No trailing slash.
        timeout_seconds: Per-request HTTP timeout. Defaults to 30 s.
    """

    base_url: str
    timeout_seconds: float = 30.0


# ── Client ────────────────────────────────────────────────────────────────────


class HRMcpClient:
    """Async HTTP client for hr_server's MCP-style tool endpoints.

    Sprint 1: plain HTTP POST per-tool with Bearer token-B.
    Sprint 2: switch to MCP protocol via langchain-mcp-adapters (out of scope here).

    Lifecycle::

        config = HRMcpClientConfig(base_url="http://hr_server:8000")
        client = HRMcpClient(config)
        result = await client.get_leave_balance(token_b=obo_token.raw)
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
        config: HRMcpClientConfig,
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
                  ``/mcp/tools/get_leave_balance``.
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

    async def get_leave_policy(
        self,
        *,
        token_b: OAuthToken,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST /mcp/tools/get_leave_policy``. Scope: ``hr_basic_rest``.

        Parameter-less read of the company leave policy (leave types + rules).
        Returns ``{"leave_types": [{leave_type, max_days_per_year,
        requires_approval, min_notice_days, description}, ...]}``.
        """
        return await self._post(
            "/mcp/tools/get_leave_policy",
            token_b=token_b,
            request_id=request_id,
            body={},
        )

    async def get_leave_balance(
        self,
        *,
        token_b: OAuthToken,
        employee_id: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST {base_url}/mcp/tools/get_leave_balance`` with Bearer token-B.

        Required scope on token-B: ``hr.read`` (enforced by hr_server).

        Args:
            token_b: The user-OBO token obtained after CIBA consent.
            employee_id: Optional employee UUID override. When *None* the
                         hr_server defaults to ``token_b.sub`` (self-service).
            request_id: Optional ``X-Request-ID`` to propagate. Falls back to
                        the ContextVar, then to a fresh UUID4.

        Returns:
            Tool result body, e.g.
            ``{"employee_id": "...", "leave_days": 12, "leave_type": "annual",
               "as_of_date": "2026-05-07"}``.

        Raises:
            httpx.HTTPStatusError: On non-2xx (including 401 from hr_server on
                                   token validation failure — ERR-MCP-NNN).
        """
        body: dict = {}
        if employee_id is not None:
            body["employee_id"] = employee_id
        return await self._post(
            "/mcp/tools/get_leave_balance",
            token_b=token_b,
            request_id=request_id,
            body=body,
        )

    async def get_leave_history(
        self,
        *,
        token_b: OAuthToken,
        employee_id: str | None = None,
        limit: int = 10,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST {base_url}/mcp/tools/get_leave_history`` with Bearer token-B.

        Required scope on token-B: ``hr.read`` (enforced by hr_server).

        Args:
            token_b: The user-OBO token.
            employee_id: Optional employee UUID override.
            limit: Maximum entries to return (server-side cap: 50).
            request_id: Optional ``X-Request-ID`` propagation override.

        Returns:
            Tool result body, e.g.
            ``{"employee_id": "...", "entries": [...]}``.

        Raises:
            httpx.HTTPStatusError: On non-2xx response.
        """
        body: dict = {"limit": limit}
        if employee_id is not None:
            body["employee_id"] = employee_id
        return await self._post(
            "/mcp/tools/get_leave_history",
            token_b=token_b,
            request_id=request_id,
            body=body,
        )

    async def approve_leave(
        self,
        *,
        token_b: OAuthToken,
        leave_id: str,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST {base_url}/mcp/tools/approve_leave`` with Bearer token-B.

        Required scope on token-B: ``hr.write`` (enforced by hr_server).
        Manager-only operation; Sprint 1 returns a canned response.

        Args:
            token_b: The user-OBO token (must carry ``hr.write``).
            leave_id: The identifier of the leave request to approve.
            request_id: Optional ``X-Request-ID`` propagation override.

        Returns:
            Tool result body, e.g.
            ``{"leave_id": "...", "status": "approved",
               "approved_by": "...", "approved_at": "..."}``.

        Raises:
            httpx.HTTPStatusError: On non-2xx response.
        """
        return await self._post(
            "/mcp/tools/approve_leave",
            token_b=token_b,
            request_id=request_id,
            body={"leave_id": leave_id},
        )

    async def reject_leave(
        self,
        *,
        token_b: OAuthToken,
        leave_id: str,
        reason: str,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST {base_url}/mcp/tools/reject_leave`` with Bearer token-B.

        Required scope on token-B: ``hr_approve_rest`` (enforced by hr_server).
        Sprint 4 S4.4 — UC-15 admin reject flow. The reason string is recorded
        on the leave request row for audit; F-08 sanitisation of any text that
        feeds the consent action is applied upstream by the dispatcher.

        Args:
            token_b: User-OBO token (must carry ``hr_approve_rest``).
            leave_id: Leave request identifier (e.g. ``LR007``).
            reason: Non-empty rejection reason captured at the SPA.
            request_id: Optional ``X-Request-ID`` propagation override.

        Returns:
            Tool result body. On success: ``{success, request_id, new_status,
            employee, notification, rejected_by}``. Service-layer rejection:
            ``{success: False, error, message, ...}``.

        Raises:
            httpx.HTTPStatusError: On non-2xx response.
        """
        return await self._post(
            "/mcp/tools/reject_leave",
            token_b=token_b,
            request_id=request_id,
            body={"leave_id": leave_id, "reason": reason},
        )

    # ── Cubicle methods (Sprint 4 S4.1, UC-11) ───────────────────────────────
    #
    # Mirror the same shape as the leave methods: take token_b + optional
    # request_id, post the JSON body, return the parsed dict. The hr_server
    # response_model handles the Pydantic shape; this client is a thin HTTP
    # adapter and does not re-validate.

    async def get_cubicle_summary(
        self,
        *,
        token_b: OAuthToken,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST /mcp/tools/get_cubicle_summary``. Scope: ``hr_read_rest``."""
        return await self._post(
            "/mcp/tools/get_cubicle_summary",
            token_b=token_b,
            request_id=request_id,
            body={},
        )

    async def get_vacant_cubicles_on_floor(
        self,
        *,
        token_b: OAuthToken,
        floor: int,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST /mcp/tools/get_vacant_cubicles_on_floor``. Scope: ``hr_read_rest``."""
        return await self._post(
            "/mcp/tools/get_vacant_cubicles_on_floor",
            token_b=token_b,
            request_id=request_id,
            body={"floor": floor},
        )

    async def get_my_cubicle(
        self,
        *,
        token_b: OAuthToken,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST /mcp/tools/get_my_cubicle``. Scope: ``hr_self_rest``."""
        return await self._post(
            "/mcp/tools/get_my_cubicle",
            token_b=token_b,
            request_id=request_id,
            body={},
        )

    async def assign_cubicle(
        self,
        *,
        token_b: OAuthToken,
        cubicle_id: str,
        employee_username: str,
        employee_email: str = "",
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST /mcp/tools/assign_cubicle``. Scope: ``hr_assets_write_rest`` (NEW)."""
        return await self._post(
            "/mcp/tools/assign_cubicle",
            token_b=token_b,
            request_id=request_id,
            body={
                "cubicle_id": cubicle_id,
                "employee_username": employee_username,
                "employee_email": employee_email,
            },
        )

    async def lookup_employee(
        self,
        *,
        token_b: OAuthToken,
        username_or_email: str,
        request_id: str | None = None,
    ) -> dict:
        """Call ``POST /mcp/tools/lookup_employee``. Scope: ``hr_read_rest``.

        F-12: caller (HR Agent) MUST NOT log the ``sub`` field of the
        response — it is internal join data only.
        """
        return await self._post(
            "/mcp/tools/lookup_employee",
            token_b=token_b,
            request_id=request_id,
            body={"username_or_email": username_or_email},
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
