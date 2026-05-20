"""JSON-RPC 2.0 pure envelope helpers for A2A communication.

This module owns ONLY wire-format types and factory helpers — no transport
(no httpx, no FastAPI), no domain types (those live in common/a2a/models.py).

Design notes (per sprint-1-fixes.md F-09):
- All classes are Pydantic v2 BaseModel because they cross HTTP boundaries.
- No asyncio types are stored here; this module is safe to import anywhere.

JSON-RPC 2.0 spec: https://www.jsonrpc.org/specification
Error code namespace (per api-contracts.md §3):
  Standard:    -32700 .. -32600
  Application: -32001 .. -32005
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, model_validator

# ---------------------------------------------------------------------------
# Error code constants
# ---------------------------------------------------------------------------

# Standard JSON-RPC 2.0 codes
PARSE_ERROR: int = -32700
INVALID_REQUEST: int = -32600
METHOD_NOT_FOUND: int = -32601
INVALID_PARAMS: int = -32602
INTERNAL_ERROR: int = -32603

# Application-specific codes (api-contracts.md §3 JSON-RPC error code table)
ERR_PEER_NOT_TRUSTED: int = -32001   # token validation failure (bad sig, wrong iss, act.sub not in allowlist)
ERR_INVALID_TOKEN_A: int = -32002    # missing X-Request-ID header
ERR_AGENT_INTERNAL: int = -32003     # argument validation failure
ERR_TOOL_NOT_FOUND: int = -32004     # CIBA initiation hard failure
ERR_AGENT_BAD_REQUEST: int = -32005  # internal specialist error

# ---------------------------------------------------------------------------
# Envelope models
# ---------------------------------------------------------------------------


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request object.

    This profile does NOT support notifications (requests without an id).
    ``make_request`` always assigns an id; ``parse_request`` accepts any
    non-null id value.

    Args:
        jsonrpc: Must be the literal string ``"2.0"``.
        id: Request identifier; echoed verbatim in the response.
        method: Method name, e.g. ``"message/send"``.
        params: Structured call parameters; dict (by-name) or list (by-position).
    """

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int
    method: str
    params: dict[str, Any] | list[Any] = {}


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 error object embedded inside a response.

    Args:
        code: Numeric error code. Use the module-level constants above.
        message: Short, human-readable error description.
        data: Optional structured detail payload; never raw exception strings.
    """

    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 response object.

    Exactly one of ``result`` or ``error`` must be set; both present or both
    absent is a protocol violation and will be rejected by the model validator.

    Args:
        jsonrpc: Must be the literal string ``"2.0"``.
        id: Echoes the originating request id; ``None`` when the id could not
            be determined (e.g. parse error on a malformed request).
        result: Present on success; absent on error.
        error: Present on error; absent on success.
    """

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None

    @model_validator(mode="after")
    def _exactly_one_of_result_or_error(self) -> "JsonRpcResponse":
        """Enforce the JSON-RPC 2.0 invariant: result XOR error must be set."""
        has_result = self.result is not None
        has_error = self.error is not None
        if has_result == has_error:
            raise ValueError(
                "JsonRpcResponse must have exactly one of 'result' or 'error' set; "
                f"got result={self.result!r}, error={self.error!r}"
            )
        return self


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def make_request(
    method: str,
    params: dict[str, Any] | list[Any] = {},
    *,
    request_id: str | None = None,
) -> JsonRpcRequest:
    """Build a JSON-RPC 2.0 request object.

    This profile does not support notifications; an ``id`` is always
    present. If ``request_id`` is ``None``, a fresh UUID4 string is
    generated automatically.

    Args:
        method: RPC method name (e.g. ``"message/send"``).
        params: By-name (dict) or by-position (list) parameters.
        request_id: Optional caller-supplied id. UUID4 generated if omitted.

    Returns:
        A validated :class:`JsonRpcRequest` instance.
    """
    return JsonRpcRequest(
        method=method,
        params=params,
        id=request_id if request_id is not None else str(uuid.uuid4()),
    )


def make_success(
    request_id: str | int | None,
    result: dict[str, Any],
) -> JsonRpcResponse:
    """Build a JSON-RPC 2.0 success response.

    Args:
        request_id: The id from the originating request; may be ``None``
            if the request id could not be parsed.
        result: The result payload dict.

    Returns:
        A validated :class:`JsonRpcResponse` with ``result`` set.
    """
    return JsonRpcResponse(id=request_id, result=result)


def make_error(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> JsonRpcResponse:
    """Build a JSON-RPC 2.0 error response.

    Args:
        request_id: The id from the originating request; may be ``None``.
        code: Numeric error code (use module-level constants).
        message: Short human-readable error description.
        data: Optional structured detail payload.

    Returns:
        A validated :class:`JsonRpcResponse` with ``error`` set.
    """
    return JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(code=code, message=message, data=data),
    )


def parse_request(body: dict[str, Any]) -> JsonRpcRequest:
    """Parse and validate a raw incoming JSON-RPC 2.0 request body.

    Rejects batch requests (lists) as unsupported in this profile.
    Raises :class:`pydantic.ValidationError` on any structural violation
    so callers can map it to a ``-32600 Invalid Request`` error response.

    Args:
        body: The decoded JSON object from the HTTP request body.

    Returns:
        A validated :class:`JsonRpcRequest`.

    Raises:
        pydantic.ValidationError: If the body does not conform to the
            JSON-RPC 2.0 request shape (missing/wrong fields).
        TypeError: If ``body`` is a list (batch requests are unsupported).
    """
    if isinstance(body, list):
        raise TypeError("Batch requests are not supported in this A2A profile")
    return JsonRpcRequest.model_validate(body)
