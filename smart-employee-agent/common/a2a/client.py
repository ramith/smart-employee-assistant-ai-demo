"""Async A2A client for the two-phase dispatch protocol (F-01).

This module implements the orchestrator-side HTTP client that talks to specialist
agents over JSON-RPC 2.0.  The **two-call pattern** is:

1. ``message_send`` → POST /a2a/message/send
   Returns immediately with ``ConsentRequiredPayload`` (CIBA initiated),
   ``ResultPayload`` (tool ran without consent), or ``ErrorPayload`` (tool
   rejected by the specialist).  On a JSON-RPC *error envelope* the method
   raises ``A2AError``.

2. ``await_completion`` → POST /a2a/await
   Long-polls the specialist until the CIBA flow resolves (approved, denied,
   expired) or the connection times out.  Returns ``ResultPayload`` on success
   or ``ErrorPayload`` on denial/expiry/MCP failure.  Raises ``A2AError`` only
   on transport-level JSON-RPC failures, NOT on payload-level errors.

3. ``cancel`` → POST /a2a/cancel
   Aborts the background CIBA polling task for an in-flight ``auth_req_id``.

Header rules (per api-contracts.md §3 + F-13):
    Authorization: Bearer <bearer_token>
    X-Request-ID:  <request_id parameter>  (falls back to correlation contextvar,
                    then generates a fresh UUID4)
    Content-Type:  application/json
    Accept:        application/json

Design notes (F-09 boundary rule):
    - ``A2AClientConfig`` is a frozen dataclass (holds no asyncio types).
    - ``A2AError`` is a plain Python Exception — NOT a Pydantic model.
    - Transport is ``httpx.AsyncClient``; caller can inject a pre-built client
      for test-harness mocking.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import TypeAdapter

from common.a2a.jsonrpc import JsonRpcResponse, make_request
from common.a2a.models import (
    A2AMessageResponse,
    CancelResponse,
    MessageSendParams,
)
from common.logging.correlation import get_request_id

__all__ = [
    "A2AClientConfig",
    "A2AError",
    "A2AClient",
]

_logger = logging.getLogger(__name__)

# TypeAdapter for the discriminated union — constructed once at import time.
_A2A_RESPONSE_ADAPTER: TypeAdapter[A2AMessageResponse] = TypeAdapter(
    A2AMessageResponse
)

# JSON-RPC method names for the three A2A endpoints.
_METHOD_MESSAGE_SEND: str = "message/send"
_METHOD_AWAIT: str = "a2a/await"
_METHOD_CANCEL: str = "a2a/cancel"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class A2AClientConfig:
    """Immutable configuration for one specialist target.

    Attributes:
        base_url: Specialist's base URL, e.g. ``"http://hr_agent:8001"``.
            The client appends endpoint paths; do NOT include a trailing slash.
        timeout_seconds: HTTP timeout for ``/a2a/message/send`` (returns
            immediately after CIBA initiation, so a short timeout is correct).
        await_timeout_seconds: HTTP timeout for ``/a2a/await``.  Must exceed
            the CIBA ``expires_in`` default (300 s) plus a small buffer.
            Default 330 s.
    """

    base_url: str
    timeout_seconds: float = 30.0
    await_timeout_seconds: float = 330.0


# ---------------------------------------------------------------------------
# A2AError
# ---------------------------------------------------------------------------


class A2AError(Exception):
    """Wraps a JSON-RPC error response into a Python exception.

    Raised by client methods when the specialist returns a JSON-RPC *error
    envelope* (i.e. the ``error`` field is set in the response JSON).  It is
    NOT raised for application-level failures represented as ``ErrorPayload``
    inside the JSON-RPC ``result`` field — those are returned normally.

    Attributes:
        code: Numeric JSON-RPC error code (e.g. ``-32001`` for token failure).
        message: Short human-readable description from the specialist.
        data: Optional structured detail payload; ``{}`` if the specialist did
            not include one.
    """

    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"A2A JSON-RPC error {code}: {message}")
        self.code: int = code
        self.message: str = message
        self.data: dict[str, Any] = data or {}

    def __repr__(self) -> str:
        return (
            f"A2AError(code={self.code!r}, message={self.message!r}, "
            f"data={self.data!r})"
        )


# ---------------------------------------------------------------------------
# A2AClient
# ---------------------------------------------------------------------------


class A2AClient:
    """Async client for the two-phase A2A dispatch protocol (F-01).

    One instance should be created per specialist target per orchestrator
    process.  The client owns its ``httpx.AsyncClient`` lifecycle unless an
    external one is injected (test harness pattern).

    Usage::

        config = A2AClientConfig(base_url="http://hr_agent:8001")
        client = A2AClient(config)

        # Phase 1 — dispatch
        first = await client.message_send(token_a, "get_leave_balance", {})
        if isinstance(first, ConsentRequiredPayload):
            # Push first.auth_url to SPA via SSE, then wait for user approval.
            second = await client.await_completion(token_a, first.auth_req_id)
            # second is ResultPayload or ErrorPayload
        elif isinstance(first, ResultPayload):
            ...  # tool ran instantly (no CIBA needed)

        await client.aclose()
    """

    def __init__(
        self,
        config: A2AClientConfig,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialise the client.

        Args:
            config: Immutable target configuration.
            http: Optional pre-built ``httpx.AsyncClient``.  When provided the
                client will NOT be closed by ``aclose()``; lifetime management
                is the caller's responsibility.  Intended for test injection.
        """
        self._config = config
        self._owned: bool = http is None
        self._http: httpx.AsyncClient = http or httpx.AsyncClient()

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def message_send(
        self,
        bearer_token: str,
        tool: str,
        args: dict[str, Any],
        *,
        request_id: str | None = None,
        last_logout_reason: str | None = None,
    ) -> A2AMessageResponse:
        """POST /a2a/message/send — Phase 1 of the two-phase A2A protocol.

        Sends a JSON-RPC ``message/send`` request with the given tool name and
        arguments.  The specialist returns one of:

        - ``ConsentRequiredPayload`` — CIBA initiated; orchestrator must push
          ``auth_url`` to SPA and then call ``await_completion``.
        - ``ResultPayload`` — tool ran synchronously (no CIBA needed).
        - ``ErrorPayload`` — specialist rejected the tool call.

        Args:
            bearer_token: Token-A forwarded as the ``Authorization: Bearer``
                credential.
            tool: MCP tool name, e.g. ``"get_leave_balance"``.
            args: Tool-specific keyword arguments; may be empty.
            request_id: ``X-Request-ID`` value.  Falls back to the correlation
                contextvar (``get_request_id()``), then to a generated UUID4.

        Returns:
            A discriminated-union instance: ``ConsentRequiredPayload``,
            ``ResultPayload``, or ``ErrorPayload``.

        Raises:
            A2AError: The specialist returned a JSON-RPC *error envelope*.
            httpx.HTTPStatusError: The specialist returned a non-2xx HTTP
                status that did not carry a JSON-RPC body.
            httpx.TimeoutException: The request timed out (``timeout_seconds``).
        """
        rid = self._resolve_request_id(request_id)
        # 3B.2 FIX-17: forward the orchestrator's recorded logout reason so
        # the specialist's CIBA dispatcher can pick a reason-aware
        # binding-message template. ``None`` for routine flows; the
        # orchestrator clears its Session.last_logout_reason after
        # passing it once.
        params = MessageSendParams(
            tool=tool,
            args=args,
            last_logout_reason=last_logout_reason,
        ).model_dump()
        rpc_request = make_request(_METHOD_MESSAGE_SEND, params, request_id=rid)
        url = f"{self._config.base_url}/a2a/message/send"

        _logger.debug(
            "a2a_message_send tool=%s request_id=%s url=%s", tool, rid, url
        )

        response = await self._http.post(
            url,
            json=rpc_request.model_dump(),
            headers=self._build_headers(bearer_token, rid),
            timeout=self._config.timeout_seconds,
        )
        response.raise_for_status()
        return self._parse_a2a_response(response.json())

    async def await_completion(
        self,
        bearer_token: str,
        auth_req_id: str,
        *,
        request_id: str | None = None,
    ) -> A2AMessageResponse:
        """POST /a2a/await — Phase 2 of the two-phase A2A protocol.

        Long-polls the specialist's in-process ``asyncio.Event`` until the CIBA
        flow resolves.  The specialist holds this connection open for up to its
        own ``expires_in + buffer`` and then responds with the result.

        Returns ``ResultPayload`` on successful consent + MCP completion, or
        ``ErrorPayload`` on denial, expiry, or MCP failure.  Application-level
        errors are returned as ``ErrorPayload``, NOT raised.

        Args:
            bearer_token: Token-A forwarded as the ``Authorization: Bearer``
                credential.
            auth_req_id: The value returned in the preceding
                ``ConsentRequiredPayload``.
            request_id: ``X-Request-ID`` value.  Falls back to contextvar then
                UUID4.

        Returns:
            ``ResultPayload`` or ``ErrorPayload``.

        Raises:
            A2AError: The specialist returned a JSON-RPC *error envelope*
                (transport-level failure, not a payload-level denial).
            httpx.ReadTimeout: The connection held open longer than
                ``await_timeout_seconds``.  The caller (``chat/routes.py``) is
                responsible for surfacing this as ``ERR-CIBA-009``.
            httpx.HTTPStatusError: Non-2xx HTTP status without JSON-RPC body.
        """
        rid = self._resolve_request_id(request_id)
        params = {"auth_req_id": auth_req_id}
        rpc_request = make_request(_METHOD_AWAIT, params, request_id=rid)
        url = f"{self._config.base_url}/a2a/await"

        _logger.debug(
            "a2a_await auth_req_id=%s request_id=%s url=%s",
            auth_req_id,
            rid,
            url,
        )

        response = await self._http.post(
            url,
            json=rpc_request.model_dump(),
            headers=self._build_headers(bearer_token, rid),
            timeout=self._config.await_timeout_seconds,
        )
        response.raise_for_status()
        return self._parse_a2a_response(response.json())

    async def cancel(
        self,
        bearer_token: str,
        auth_req_id: str,
        *,
        request_id: str | None = None,
    ) -> CancelResponse:
        """POST /a2a/cancel — abort an in-flight CIBA polling task.

        The specialist aborts the background polling task keyed by
        ``auth_req_id``.  Returns ``CancelResponse(cancelled=True)`` when a
        pending task was found and aborted; ``CancelResponse(cancelled=False)``
        when the ``auth_req_id`` was not found or polling had already completed.

        Args:
            bearer_token: Token-A forwarded as the ``Authorization: Bearer``
                credential.
            auth_req_id: Identifier of the CIBA flow to cancel.
            request_id: ``X-Request-ID`` value.  Falls back to contextvar then
                UUID4.

        Returns:
            ``CancelResponse`` with ``cancelled`` and optional ``reason``.

        Raises:
            A2AError: The specialist returned a JSON-RPC *error envelope*.
            httpx.TimeoutException: The request timed out (``timeout_seconds``).
        """
        rid = self._resolve_request_id(request_id)
        params = {"auth_req_id": auth_req_id}
        rpc_request = make_request(_METHOD_CANCEL, params, request_id=rid)
        url = f"{self._config.base_url}/a2a/cancel"

        _logger.debug(
            "a2a_cancel auth_req_id=%s request_id=%s url=%s",
            auth_req_id,
            rid,
            url,
        )

        response = await self._http.post(
            url,
            json=rpc_request.model_dump(),
            headers=self._build_headers(bearer_token, rid),
            timeout=self._config.timeout_seconds,
        )
        response.raise_for_status()
        rpc_response = JsonRpcResponse.model_validate(response.json())
        if rpc_response.error is not None:
            raise A2AError(
                rpc_response.error.code,
                rpc_response.error.message,
                data=rpc_response.error.data,
            )
        # result is guaranteed non-None here (model validator enforces XOR)
        return CancelResponse.model_validate(rpc_response.result)

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created by this instance.

        Injected clients (``http=`` constructor argument) are NOT closed — their
        lifecycle is the caller's responsibility.
        """
        if self._owned:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "A2AClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_request_id(explicit: str | None) -> str:
        """Return the best available request correlation id.

        Priority:
        1. ``explicit`` parameter (caller-supplied).
        2. ``get_request_id()`` from the correlation ContextVar.
        3. Fresh UUID4 string (generated here).

        Args:
            explicit: Caller-supplied id, or ``None``.

        Returns:
            A non-empty string suitable for use as ``X-Request-ID``.
        """
        if explicit is not None:
            return explicit
        contextvar_id = get_request_id()
        if contextvar_id:
            return contextvar_id
        return str(uuid.uuid4())

    @staticmethod
    def _build_headers(bearer_token: str, request_id: str) -> dict[str, str]:
        """Construct the required HTTP headers for every A2A request.

        Args:
            bearer_token: Raw token string (without the ``Bearer`` prefix).
            request_id: Resolved ``X-Request-ID`` value.

        Returns:
            A dict with ``Authorization``, ``X-Request-ID``, ``Content-Type``,
            and ``Accept`` entries.
        """
        return {
            "Authorization": f"Bearer {bearer_token}",
            "X-Request-ID": request_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _parse_a2a_response(body: dict[str, Any]) -> A2AMessageResponse:
        """Parse a raw JSON-RPC response body into an ``A2AMessageResponse``.

        Validates the JSON-RPC envelope first; raises ``A2AError`` if the
        specialist returned an error envelope; then parses the ``result`` field
        through the discriminated union ``TypeAdapter``.

        Args:
            body: Decoded JSON dict from the HTTP response.

        Returns:
            One of ``ConsentRequiredPayload``, ``ResultPayload``, or
            ``ErrorPayload``.

        Raises:
            A2AError: Envelope has the ``error`` field set.
            pydantic.ValidationError: The ``result`` dict does not match any
                known payload type (indicates a protocol mismatch with the
                specialist).
        """
        rpc_response = JsonRpcResponse.model_validate(body)
        if rpc_response.error is not None:
            raise A2AError(
                rpc_response.error.code,
                rpc_response.error.message,
                data=rpc_response.error.data,
            )
        # result is guaranteed non-None (model validator enforces XOR)
        return _A2A_RESPONSE_ADAPTER.validate_python(rpc_response.result)
