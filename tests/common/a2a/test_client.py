"""Tests for common/a2a/client.py — Wave 3, Sprint 1.

Coverage targets
----------------
1.  message_send happy path → ConsentRequiredPayload
2.  message_send instant-result happy path → ResultPayload
3.  message_send returns ErrorPayload for tool-rejected
4.  message_send JSON-RPC error envelope → raises A2AError with the code
5.  message_send includes Authorization and X-Request-ID headers
6.  request_id=None falls back to correlation.get_request_id() contextvar
7.  request_id=None and contextvar empty → generates a UUID4 string
8.  await_completion returns ResultPayload after a long-poll
9.  await_completion returns ErrorPayload on denial
10. cancel returns CancelResponse(cancelled=True)
11. HTTP timeout on await_completion raises httpx.ReadTimeout (caller's
    responsibility to handle)
12. aclose closes owned client only (injected client is not closed)
13. message_send JSON-RPC error carries data dict
14. cancel JSON-RPC error envelope raises A2AError
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from common.a2a.client import A2AClient, A2AClientConfig, A2AError
from common.a2a.models import (
    CancelResponse,
    ConsentRequiredPayload,
    ErrorPayload,
    ResultPayload,
)
from common.logging.correlation import set_request_id

# ---------------------------------------------------------------------------
# Helpers — fixture payloads
# ---------------------------------------------------------------------------

_CONSENT_PAYLOAD: dict[str, Any] = {
    "type": "consent_required",
    "auth_req_id": "ari-abc-123",
    "auth_url": "https://is.example.com/authz/ciba?req=abc",
    "agent_label": "HR Agent",
    "action": "View your leave balance",
    "scope": "openid hr.read",
    "binding_message": "HR Agent wants to View your leave balance — request ari-abc",
    "expires_in": 300,
    "is_refresh": False,
    "prior_consent_at": None,
}

_RESULT_PAYLOAD: dict[str, Any] = {
    "type": "result",
    "data": {"leave_days": 12, "leave_type": "Annual"},
    "token_jti": "jti-xyz-789",
    "token_exp": 1_746_700_000,
    "token_iat": 1_746_696_400,
}

_ERROR_PAYLOAD: dict[str, Any] = {
    "type": "error",
    "error_id": "ERR-CIBA-005",
    "reason": "user denied consent",
}

_CANCEL_RESULT: dict[str, Any] = {"cancelled": True, "reason": "polling task aborted"}


def _jsonrpc_success(result: dict[str, Any], rpc_id: str = "test-id") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _jsonrpc_error(
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
    rpc_id: str = "test-id",
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


# ---------------------------------------------------------------------------
# Fixture — client with injectable transport
# ---------------------------------------------------------------------------


def _make_mock_response(body: dict[str, Any], status_code: int = 200) -> MagicMock:
    """Return a mock httpx.Response that returns *body* as JSON."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status_code
    mock_resp.json.return_value = body
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _make_client(mock_http: MagicMock) -> A2AClient:
    """Return an A2AClient backed by a mock AsyncClient."""
    cfg = A2AClientConfig(base_url="http://hr_agent:8001")
    return A2AClient(cfg, http=mock_http)


def _make_async_mock_http(response_body: dict[str, Any]) -> MagicMock:
    """Return a mock httpx.AsyncClient whose post() returns a success response."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(return_value=_make_mock_response(response_body))
    mock_http.aclose = AsyncMock()
    return mock_http


# ---------------------------------------------------------------------------
# 1. message_send happy path → ConsentRequiredPayload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_send_returns_consent_required_payload() -> None:
    mock_http = _make_async_mock_http(_jsonrpc_success(_CONSENT_PAYLOAD))
    client = _make_client(mock_http)

    result = await client.message_send("token-a", "get_leave_balance", {}, request_id="req-1")

    assert isinstance(result, ConsentRequiredPayload)
    assert result.auth_req_id == "ari-abc-123"
    assert result.auth_url == "https://is.example.com/authz/ciba?req=abc"
    assert result.agent_label == "HR Agent"
    assert result.expires_in == 300


# ---------------------------------------------------------------------------
# 2. message_send instant-result happy path → ResultPayload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_send_returns_result_payload_instantly() -> None:
    mock_http = _make_async_mock_http(_jsonrpc_success(_RESULT_PAYLOAD))
    client = _make_client(mock_http)

    result = await client.message_send("token-a", "get_leave_balance", {}, request_id="req-2")

    assert isinstance(result, ResultPayload)
    assert result.data == {"leave_days": 12, "leave_type": "Annual"}
    assert result.token_jti == "jti-xyz-789"
    assert result.token_exp == 1_746_700_000
    assert result.token_iat == 1_746_696_400


# ---------------------------------------------------------------------------
# 3. message_send returns ErrorPayload for tool-rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_send_returns_error_payload_for_tool_rejection() -> None:
    mock_http = _make_async_mock_http(_jsonrpc_success(_ERROR_PAYLOAD))
    client = _make_client(mock_http)

    result = await client.message_send("token-a", "get_leave_balance", {}, request_id="req-3")

    assert isinstance(result, ErrorPayload)
    assert result.error_id == "ERR-CIBA-005"
    assert result.reason == "user denied consent"


# ---------------------------------------------------------------------------
# 4. message_send JSON-RPC error envelope → raises A2AError with the code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_send_rpc_error_raises_a2a_error() -> None:
    error_body = _jsonrpc_error(-32001, "token validation failure")
    mock_http = _make_async_mock_http(error_body)
    client = _make_client(mock_http)

    with pytest.raises(A2AError) as exc_info:
        await client.message_send("bad-token", "get_leave_balance", {}, request_id="req-4")

    err = exc_info.value
    assert err.code == -32001
    assert "token validation failure" in err.message
    assert err.data == {}


# ---------------------------------------------------------------------------
# 5. message_send includes Authorization and X-Request-ID headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_send_includes_required_headers() -> None:
    mock_http = _make_async_mock_http(_jsonrpc_success(_RESULT_PAYLOAD))
    client = _make_client(mock_http)

    await client.message_send("my-bearer-token", "get_leave_balance", {}, request_id="req-hdr")

    call_kwargs = mock_http.post.call_args
    headers: dict[str, str] = call_kwargs.kwargs["headers"]

    assert headers["Authorization"] == "Bearer my-bearer-token"
    assert headers["X-Request-ID"] == "req-hdr"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"


# ---------------------------------------------------------------------------
# 6. request_id=None falls back to correlation.get_request_id() contextvar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_send_uses_correlation_contextvar_when_no_request_id() -> None:
    mock_http = _make_async_mock_http(_jsonrpc_success(_RESULT_PAYLOAD))
    client = _make_client(mock_http)

    # Seed the contextvar.
    set_request_id("contextvar-rid-001")
    try:
        await client.message_send("token-a", "get_leave_balance", {})
    finally:
        # Reset: set an empty string so the next test starts clean.
        set_request_id("")

    call_kwargs = mock_http.post.call_args
    headers: dict[str, str] = call_kwargs.kwargs["headers"]
    assert headers["X-Request-ID"] == "contextvar-rid-001"


# ---------------------------------------------------------------------------
# 7. request_id=None and contextvar empty → generates a UUID4 string
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_send_generates_uuid4_when_contextvar_empty() -> None:
    mock_http = _make_async_mock_http(_jsonrpc_success(_RESULT_PAYLOAD))
    client = _make_client(mock_http)

    # Ensure contextvar is empty.
    set_request_id("")
    await client.message_send("token-a", "get_leave_balance", {})

    call_kwargs = mock_http.post.call_args
    headers: dict[str, str] = call_kwargs.kwargs["headers"]
    rid = headers["X-Request-ID"]

    # Must be a valid UUID4 string.
    parsed = uuid.UUID(rid, version=4)
    assert str(parsed) == rid


# ---------------------------------------------------------------------------
# 8. await_completion returns ResultPayload after a long-poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_completion_returns_result_payload() -> None:
    mock_http = _make_async_mock_http(_jsonrpc_success(_RESULT_PAYLOAD))
    client = _make_client(mock_http)

    result = await client.await_completion("token-a", "ari-abc-123", request_id="req-8")

    assert isinstance(result, ResultPayload)
    assert result.data == {"leave_days": 12, "leave_type": "Annual"}
    assert result.token_jti == "jti-xyz-789"

    # Verify the correct endpoint was called.
    call_url: str = mock_http.post.call_args.args[0]
    assert call_url.endswith("/a2a/await")

    # Verify auth_req_id was sent in the JSON-RPC params.
    call_json: dict[str, Any] = mock_http.post.call_args.kwargs["json"]
    assert call_json["params"]["auth_req_id"] == "ari-abc-123"


# ---------------------------------------------------------------------------
# 9. await_completion returns ErrorPayload on denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_completion_returns_error_payload_on_denial() -> None:
    denial_payload: dict[str, Any] = {
        "type": "error",
        "error_id": "ERR-CIBA-009",
        "reason": "auth_req_id expired",
    }
    mock_http = _make_async_mock_http(_jsonrpc_success(denial_payload))
    client = _make_client(mock_http)

    result = await client.await_completion("token-a", "ari-expired", request_id="req-9")

    assert isinstance(result, ErrorPayload)
    assert result.error_id == "ERR-CIBA-009"
    assert result.reason == "auth_req_id expired"


# ---------------------------------------------------------------------------
# 10. cancel returns CancelResponse(cancelled=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_returns_cancel_response_cancelled_true() -> None:
    mock_http = _make_async_mock_http(_jsonrpc_success(_CANCEL_RESULT))
    client = _make_client(mock_http)

    response = await client.cancel("token-a", "ari-abc-123", request_id="req-10")

    assert isinstance(response, CancelResponse)
    assert response.cancelled is True
    assert response.reason == "polling task aborted"

    call_url: str = mock_http.post.call_args.args[0]
    assert call_url.endswith("/a2a/cancel")


# ---------------------------------------------------------------------------
# 11. HTTP timeout on await_completion raises httpx.ReadTimeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_completion_propagates_read_timeout() -> None:
    """Caller is responsible for handling httpx.ReadTimeout (not swallowed)."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.post = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
    mock_http.aclose = AsyncMock()

    client = _make_client(mock_http)

    with pytest.raises(httpx.ReadTimeout):
        await client.await_completion("token-a", "ari-timeout", request_id="req-11")


# ---------------------------------------------------------------------------
# 12. aclose closes owned client only — injected client is not closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_owned_client() -> None:
    """Client created without injection owns its httpx.AsyncClient."""
    cfg = A2AClientConfig(base_url="http://hr_agent:8001")
    client = A2AClient(cfg)

    # Patch the internal http client's aclose.
    mock_aclose = AsyncMock()
    client._http.aclose = mock_aclose  # type: ignore[method-assign]

    await client.aclose()

    mock_aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_does_not_close_injected_client() -> None:
    """When an external client is injected, aclose() must NOT call its close."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.aclose = AsyncMock()

    cfg = A2AClientConfig(base_url="http://hr_agent:8001")
    client = A2AClient(cfg, http=mock_http)

    await client.aclose()

    mock_http.aclose.assert_not_awaited()


# ---------------------------------------------------------------------------
# 13. message_send JSON-RPC error carries data dict when present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_send_rpc_error_carries_data_dict() -> None:
    error_detail: dict[str, Any] = {"token_error": "invalid_signature", "kid": "key-001"}
    error_body = _jsonrpc_error(-32001, "invalid token", data=error_detail)
    mock_http = _make_async_mock_http(error_body)
    client = _make_client(mock_http)

    with pytest.raises(A2AError) as exc_info:
        await client.message_send("bad-token", "get_leave_balance", {}, request_id="req-13")

    err = exc_info.value
    assert err.code == -32001
    assert err.data == error_detail


# ---------------------------------------------------------------------------
# 14. cancel JSON-RPC error envelope raises A2AError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_rpc_error_raises_a2a_error() -> None:
    error_body = _jsonrpc_error(-32005, "internal specialist error")
    mock_http = _make_async_mock_http(error_body)
    client = _make_client(mock_http)

    with pytest.raises(A2AError) as exc_info:
        await client.cancel("token-a", "ari-bad", request_id="req-14")

    err = exc_info.value
    assert err.code == -32005


# ---------------------------------------------------------------------------
# Additional: await_completion uses await_timeout_seconds (not timeout_seconds)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_completion_uses_await_timeout_seconds() -> None:
    """The await call must pass await_timeout_seconds, not timeout_seconds."""
    mock_http = _make_async_mock_http(_jsonrpc_success(_RESULT_PAYLOAD))

    cfg = A2AClientConfig(
        base_url="http://hr_agent:8001",
        timeout_seconds=5.0,
        await_timeout_seconds=330.0,
    )
    client = A2AClient(cfg, http=mock_http)

    await client.await_completion("token-a", "ari-xxx", request_id="req-to")

    call_kwargs = mock_http.post.call_args.kwargs
    assert call_kwargs["timeout"] == 330.0


@pytest.mark.asyncio
async def test_message_send_uses_short_timeout_seconds() -> None:
    """The message_send call must pass timeout_seconds, not await_timeout_seconds."""
    mock_http = _make_async_mock_http(_jsonrpc_success(_CONSENT_PAYLOAD))

    cfg = A2AClientConfig(
        base_url="http://hr_agent:8001",
        timeout_seconds=15.0,
        await_timeout_seconds=330.0,
    )
    client = A2AClient(cfg, http=mock_http)

    await client.message_send("token-a", "get_leave_balance", {}, request_id="req-to2")

    call_kwargs = mock_http.post.call_args.kwargs
    assert call_kwargs["timeout"] == 15.0


# ---------------------------------------------------------------------------
# Additional: cancel does NOT use await_timeout_seconds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_uses_short_timeout_seconds() -> None:
    mock_http = _make_async_mock_http(_jsonrpc_success(_CANCEL_RESULT))

    cfg = A2AClientConfig(
        base_url="http://hr_agent:8001",
        timeout_seconds=10.0,
        await_timeout_seconds=330.0,
    )
    client = A2AClient(cfg, http=mock_http)

    await client.cancel("token-a", "ari-x", request_id="req-cto")

    call_kwargs = mock_http.post.call_args.kwargs
    assert call_kwargs["timeout"] == 10.0
