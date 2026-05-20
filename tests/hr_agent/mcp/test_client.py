"""Tests for hr_agent/mcp/client.py — Wave 5, Sprint 1.

Test inventory (7 tests):
    1. get_leave_balance sends Bearer header with token_b.access_token
    2. X-Request-ID: explicit param wins over ContextVar wins over generated UUID
    3. get_leave_balance parses JSON body and returns dict
    4. Non-2xx response raises httpx.HTTPStatusError
    5. Sends Content-Type: application/json for POST
    6. aclose() closes owned client only (not injected client)
    7. Each tool call is independent (no shared state between calls)

Bootstrap strategy: load the module under test via importlib, bypassing package
__init__.py files that may not be fully implemented yet. This mirrors the pattern
used in tests/hr_agent/test_config.py.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

# ── Module bootstrap ──────────────────────────────────────────────────────────

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str, rel_dir: str) -> None:
    """Register a stub package namespace if not already in sys.modules."""
    if dotted_name not in sys.modules:
        stub = types.ModuleType(dotted_name)
        stub.__package__ = dotted_name
        stub.__path__ = [str(_ROOT / rel_dir)]  # type: ignore[assignment]
        sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules without executing package __init__."""
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]
    file_path = _ROOT / rel_path
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {file_path}"
    module = importlib.util.module_from_spec(spec)
    module.__package__ = dotted_name.rsplit(".", 1)[0] if "." in dotted_name else ""
    sys.modules[dotted_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# Package stubs needed for imports inside the modules under test.
for _pkg, _rel in [
    ("common", "common"),
    ("common.auth", "common/auth"),
    ("common.logging", "common/logging"),
    ("hr_agent", "hr_agent"),
    ("hr_agent.mcp", "hr_agent/mcp"),
]:
    _ensure_pkg(_pkg, _rel)

# Load leaves-and-logging dependencies first.
_models = _load_module("common.auth.models", "common/auth/models.py")
_correlation = _load_module("common.logging.correlation", "common/logging/correlation.py")

# Load the module under test.
_client_mod = _load_module("hr_agent.mcp.client", "hr_agent/mcp/client.py")

HRMcpClientConfig = _client_mod.HRMcpClientConfig
HRMcpClient = _client_mod.HRMcpClient
OAuthToken = _models.OAuthToken

# ── Fixtures ──────────────────────────────────────────────────────────────────

_BASE_URL = "http://hr_server:8000"


def _make_token(access_token: str = "token-b-value") -> Any:
    """Build a minimal OAuthToken fixture."""
    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=3600,
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        refresh_token=None,
        scope="openid hr.read",
        id_token=None,
    )


@pytest.fixture()
def config() -> Any:
    return HRMcpClientConfig(base_url=_BASE_URL)


# ── Helper: capture the last request sent through a mock client ───────────────

def _last_request(httpx_mock: HTTPXMock) -> httpx.Request:
    requests = httpx_mock.get_requests()
    assert requests, "No request was recorded by HTTPXMock"
    return requests[-1]


# ── Test 1: Bearer header contains token_b.access_token ──────────────────────

@pytest.mark.asyncio
async def test_get_leave_balance_sends_bearer_token(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """get_leave_balance must send Authorization: Bearer <token_b.access_token>."""
    token_b = _make_token("my-secret-obo-token")
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_balance",
        json={"leave_days": 12, "leave_type": "annual"},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        await client.get_leave_balance(token_b=token_b)

    req = _last_request(httpx_mock)
    assert req.headers["authorization"] == "Bearer my-secret-obo-token"


# ── Test 2: X-Request-ID precedence (explicit > contextvar > uuid4) ───────────

@pytest.mark.asyncio
async def test_explicit_request_id_wins(httpx_mock: HTTPXMock, config: Any) -> None:
    """Explicit request_id param must appear in X-Request-ID header."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_balance",
        json={"leave_days": 5},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        await client.get_leave_balance(
            token_b=token_b, request_id="explicit-rid-123"
        )

    req = _last_request(httpx_mock)
    assert req.headers["x-request-id"] == "explicit-rid-123"


@pytest.mark.asyncio
async def test_contextvar_request_id_used_when_no_explicit(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """When no explicit request_id, ContextVar value must be used."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_balance",
        json={"leave_days": 5},
    )

    # Inject a known value into the ContextVar.
    _correlation.set_request_id("contextvar-rid-456")
    try:
        async with httpx.AsyncClient() as http:
            client = HRMcpClient(config, http=http)
            await client.get_leave_balance(token_b=token_b)
    finally:
        _correlation.set_request_id("")

    req = _last_request(httpx_mock)
    assert req.headers["x-request-id"] == "contextvar-rid-456"


@pytest.mark.asyncio
async def test_uuid_generated_when_no_request_id(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """When no explicit request_id and ContextVar is empty, a UUID4 must be generated."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_balance",
        json={"leave_days": 5},
    )

    # Ensure ContextVar is empty.
    _correlation.set_request_id("")
    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        await client.get_leave_balance(token_b=token_b)

    req = _last_request(httpx_mock)
    rid = req.headers["x-request-id"]
    # Must be a valid UUID4-shaped string (36 chars with dashes).
    parsed = uuid.UUID(rid)
    assert str(parsed) == rid


# ── Test 3: JSON body is parsed and returned as dict ─────────────────────────

@pytest.mark.asyncio
async def test_get_leave_balance_returns_parsed_dict(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """get_leave_balance must return the JSON response body as a plain dict."""
    token_b = _make_token()
    expected = {
        "employee_id": "user-uuid-001",
        "leave_days": 12,
        "leave_type": "annual",
        "as_of_date": "2026-05-07",
    }
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_balance",
        json=expected,
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        result = await client.get_leave_balance(token_b=token_b)

    assert result == expected
    assert isinstance(result, dict)


# ── Test 4: Non-2xx raises HTTPStatusError ────────────────────────────────────

@pytest.mark.asyncio
async def test_non_2xx_raises_http_status_error(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """A 401 response from hr_server must raise httpx.HTTPStatusError."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_balance",
        status_code=401,
        json={"error_id": "ERR-MCP-001", "request_id": "test-rid"},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.get_leave_balance(token_b=token_b)

    assert exc_info.value.response.status_code == 401


@pytest.mark.asyncio
async def test_500_raises_http_status_error(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """A 500 response must also raise httpx.HTTPStatusError."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_balance",
        status_code=500,
        json={"error_id": "ERR-MCP-004", "request_id": "test-rid"},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.get_leave_balance(token_b=token_b)

    assert exc_info.value.response.status_code == 500


# ── Test 5: Content-Type: application/json on POST ───────────────────────────

@pytest.mark.asyncio
async def test_post_sends_content_type_json(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """Every POST must include Content-Type: application/json."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_history",
        json={"employee_id": "u1", "entries": []},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        await client.get_leave_history(token_b=token_b)

    req = _last_request(httpx_mock)
    assert req.headers["content-type"] == "application/json"


# ── Test 6: aclose() closes owned client only ─────────────────────────────────

@pytest.mark.asyncio
async def test_aclose_closes_owned_client(config: Any) -> None:
    """aclose() must close the internally created AsyncClient."""
    client = HRMcpClient(config)
    # The internal client should be open before aclose.
    assert not client._http.is_closed
    await client.aclose()
    assert client._http.is_closed


@pytest.mark.asyncio
async def test_aclose_does_not_close_injected_client(config: Any) -> None:
    """aclose() must NOT close an externally injected AsyncClient."""
    external = httpx.AsyncClient()
    client = HRMcpClient(config, http=external)
    await client.aclose()
    # External client must still be open.
    assert not external.is_closed
    await external.aclose()  # Clean up.


# ── Test 7: Each tool call is independent (no shared state) ───────────────────

@pytest.mark.asyncio
async def test_independent_calls_do_not_bleed_state(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """Consecutive calls must each send their own independent headers."""
    token_1 = _make_token("token-call-1")
    token_2 = _make_token("token-call-2")

    # Register two responses — one for each call (is_reusable=False by default).
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_balance",
        json={"leave_days": 10},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_balance",
        json={"leave_days": 5},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        await client.get_leave_balance(
            token_b=token_1, request_id="rid-first"
        )
        await client.get_leave_balance(
            token_b=token_2, request_id="rid-second"
        )

    requests = httpx_mock.get_requests()
    assert len(requests) == 2

    req1, req2 = requests[0], requests[1]
    assert req1.headers["authorization"] == "Bearer token-call-1"
    assert req1.headers["x-request-id"] == "rid-first"
    assert req2.headers["authorization"] == "Bearer token-call-2"
    assert req2.headers["x-request-id"] == "rid-second"


# ── Additional: approve_leave passes leave_id in body ─────────────────────────

@pytest.mark.asyncio
async def test_approve_leave_sends_leave_id_in_body(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """approve_leave must include leave_id in the POST JSON body."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/approve_leave",
        json={
            "leave_id": "leave-42",
            "status": "approved",
            "approved_by": "agent-uuid",
            "approved_at": "2026-05-07T10:00:00Z",
        },
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        result = await client.approve_leave(
            token_b=token_b, leave_id="leave-42", request_id="test-rid"
        )

    req = _last_request(httpx_mock)
    body = json.loads(req.content)
    assert body["leave_id"] == "leave-42"
    assert result["status"] == "approved"


# ── Additional: get_leave_history passes employee_id when provided ─────────────

@pytest.mark.asyncio
async def test_get_leave_history_sends_employee_id(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """get_leave_history must include employee_id in body when provided."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_leave_history",
        json={"employee_id": "emp-99", "entries": []},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        result = await client.get_leave_history(
            token_b=token_b, employee_id="emp-99", request_id="test-rid"
        )

    req = _last_request(httpx_mock)
    body = json.loads(req.content)
    assert body["employee_id"] == "emp-99"
    assert result["employee_id"] == "emp-99"


# ── Test 8: apply_leave (S5.1) posts the right body to the right path ────────

@pytest.mark.asyncio
async def test_apply_leave_posts_expected_body(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """apply_leave POSTs {leave_type, start_date, end_date, reason} with the
    Bearer token-B header to /mcp/tools/apply_leave, and returns the parsed body."""
    token_b = _make_token("obo-apply")
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/apply_leave",
        json={"success": True, "request_id": "LR007"},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        result = await client.apply_leave(
            token_b=token_b,
            leave_type="Annual Leave",
            start_date="2026-06-10",
            end_date="2026-06-14",
            reason="family trip",
        )

    req = _last_request(httpx_mock)
    assert str(req.url) == f"{_BASE_URL}/mcp/tools/apply_leave"
    assert req.headers["authorization"] == "Bearer obo-apply"
    body = json.loads(req.content)
    assert body == {
        "leave_type": "Annual Leave",
        "start_date": "2026-06-10",
        "end_date": "2026-06-14",
        "reason": "family trip",
    }
    assert result == {"success": True, "request_id": "LR007"}


@pytest.mark.asyncio
async def test_apply_leave_default_reason_empty(httpx_mock: HTTPXMock, config: Any) -> None:
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/apply_leave",
        json={"success": True, "request_id": "LR008"},
    )
    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        await client.apply_leave(
            token_b=token_b, leave_type="Sick Leave",
            start_date="2026-06-10", end_date="2026-06-10",
        )
    body = json.loads(_last_request(httpx_mock).content)
    assert body["reason"] == ""


# ── Test: get_all_leaves (Sprint 5 hr.read_all_leaves) ───────────────────────


@pytest.mark.asyncio
async def test_get_all_leaves_posts_to_correct_url_with_bearer(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """get_all_leaves must POST to /mcp/tools/get_all_leave_requests with Bearer token."""
    token_b = _make_token("admin-obo-token")
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_all_leave_requests",
        json={"leave_requests": []},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        result = await client.get_all_leaves(token_b, request_id="rid-all-leaves")

    req = _last_request(httpx_mock)
    assert str(req.url) == f"{_BASE_URL}/mcp/tools/get_all_leave_requests"
    assert req.headers["authorization"] == "Bearer admin-obo-token"
    assert req.headers["x-request-id"] == "rid-all-leaves"
    assert result == {"leave_requests": []}


@pytest.mark.asyncio
async def test_get_all_leaves_omits_none_filters_from_body(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """When status and employee_name are None, the body must be empty (no null keys)."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_all_leave_requests",
        json={"leave_requests": []},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        await client.get_all_leaves(token_b, status=None, employee_name=None)

    body = json.loads(_last_request(httpx_mock).content)
    assert "status" not in body
    assert "employee_name" not in body


@pytest.mark.asyncio
async def test_get_all_leaves_includes_status_filter_when_provided(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """When status='Pending', the body must include status='Pending'."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_all_leave_requests",
        json={"leave_requests": [{"request_id": "LR001", "employee": "Alice",
                                   "type": "Annual Leave", "start_date": "2026-06-10",
                                   "end_date": "2026-06-14", "days_requested": 5,
                                   "status": "Pending"}]},
    )

    async with httpx.AsyncClient() as http:
        client = HRMcpClient(config, http=http)
        result = await client.get_all_leaves(token_b, status="Pending")

    body = json.loads(_last_request(httpx_mock).content)
    assert body["status"] == "Pending"
    assert "employee_name" not in body
    assert len(result["leave_requests"]) == 1
