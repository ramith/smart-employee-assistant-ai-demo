"""Tests for it_agent/mcp/client.py — Wave 5, Sprint 1.

Test inventory (7 tests):
    1. list_available_assets sends Bearer header with token_b.access_token
    2. X-Request-ID: explicit param wins over ContextVar wins over generated UUID
    3. list_available_assets parses JSON body and returns dict
    4. Non-2xx response raises httpx.HTTPStatusError
    5. Sends Content-Type: application/json for POST
    6. aclose() closes owned client only (not injected client)
    7. Each tool call is independent (no shared state between calls)

Bootstrap strategy: load the module under test via importlib, bypassing package
__init__.py files that may not be fully implemented yet. This mirrors the pattern
used in tests/it_agent/test_config.py.
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
    ("it_agent", "it_agent"),
    ("it_agent.mcp", "it_agent/mcp"),
]:
    _ensure_pkg(_pkg, _rel)

# Load dependencies first.
_models = _load_module("common.auth.models", "common/auth/models.py")
_correlation = _load_module("common.logging.correlation", "common/logging/correlation.py")

# Load the module under test.
_client_mod = _load_module("it_agent.mcp.client", "it_agent/mcp/client.py")

ITMcpClientConfig = _client_mod.ITMcpClientConfig
ITMcpClient = _client_mod.ITMcpClient
OAuthToken = _models.OAuthToken

# ── Fixtures ──────────────────────────────────────────────────────────────────

_BASE_URL = "http://it_server:8004"


def _make_token(access_token: str = "token-b-value") -> Any:
    """Build a minimal OAuthToken fixture."""
    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=3600,
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        refresh_token=None,
        scope="openid it.read",
        id_token=None,
    )


@pytest.fixture()
def config() -> Any:
    return ITMcpClientConfig(base_url=_BASE_URL)


# ── Helper: capture the last request sent through a mock client ───────────────

def _last_request(httpx_mock: HTTPXMock) -> httpx.Request:
    requests = httpx_mock.get_requests()
    assert requests, "No request was recorded by HTTPXMock"
    return requests[-1]


# ── Test 1: Bearer header contains token_b.access_token ──────────────────────

@pytest.mark.asyncio
async def test_list_available_assets_sends_bearer_token(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """list_available_assets must send Authorization: Bearer <token_b.access_token>."""
    token_b = _make_token("my-secret-it-obo-token")
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        json={"assets": []},
    )

    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        await client.list_available_assets(token_b=token_b)

    req = _last_request(httpx_mock)
    assert req.headers["authorization"] == "Bearer my-secret-it-obo-token"


# ── Test 2: X-Request-ID precedence (explicit > contextvar > uuid4) ───────────

@pytest.mark.asyncio
async def test_explicit_request_id_wins(httpx_mock: HTTPXMock, config: Any) -> None:
    """Explicit request_id param must appear in X-Request-ID header."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        json={"assets": []},
    )

    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        await client.list_available_assets(
            token_b=token_b, request_id="explicit-it-rid-123"
        )

    req = _last_request(httpx_mock)
    assert req.headers["x-request-id"] == "explicit-it-rid-123"


@pytest.mark.asyncio
async def test_contextvar_request_id_used_when_no_explicit(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """When no explicit request_id, ContextVar value must be used."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        json={"assets": []},
    )

    _correlation.set_request_id("contextvar-it-rid-789")
    try:
        async with httpx.AsyncClient() as http:
            client = ITMcpClient(config, http=http)
            await client.list_available_assets(token_b=token_b)
    finally:
        _correlation.set_request_id("")

    req = _last_request(httpx_mock)
    assert req.headers["x-request-id"] == "contextvar-it-rid-789"


@pytest.mark.asyncio
async def test_uuid_generated_when_no_request_id(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """When no explicit request_id and ContextVar is empty, a UUID4 must be generated."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        json={"assets": []},
    )

    _correlation.set_request_id("")
    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        await client.list_available_assets(token_b=token_b)

    req = _last_request(httpx_mock)
    rid = req.headers["x-request-id"]
    parsed = uuid.UUID(rid)
    assert str(parsed) == rid


# ── Test 3: JSON body is parsed and returned as dict ─────────────────────────

@pytest.mark.asyncio
async def test_list_available_assets_returns_parsed_dict(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """list_available_assets must return the JSON response body as a plain dict."""
    token_b = _make_token()
    expected = {
        "assets": [
            {
                "asset_id": "MBP-14",
                "model": "MacBook Pro 14",
                "type": "laptop",
                "available_count": 3,
            }
        ]
    }
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        json=expected,
    )

    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        result = await client.list_available_assets(token_b=token_b)

    assert result == expected
    assert isinstance(result, dict)


# ── Test 4: Non-2xx raises HTTPStatusError ────────────────────────────────────

@pytest.mark.asyncio
async def test_non_2xx_raises_http_status_error(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """A 401 response from it_server must raise httpx.HTTPStatusError."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        status_code=401,
        json={"error_id": "ERR-MCP-001", "request_id": "test-rid"},
    )

    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.list_available_assets(token_b=token_b)

    assert exc_info.value.response.status_code == 401


@pytest.mark.asyncio
async def test_403_raises_http_status_error(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """A 403 response (wrong act.sub) must also raise httpx.HTTPStatusError."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        status_code=403,
        json={"error_id": "ERR-MCP-002", "request_id": "test-rid"},
    )

    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.list_available_assets(token_b=token_b)

    assert exc_info.value.response.status_code == 403


# ── Test 5: Content-Type: application/json on POST ───────────────────────────

@pytest.mark.asyncio
async def test_post_sends_content_type_json(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """Every POST must include Content-Type: application/json."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_my_assets",
        json={"assets": [], "total": 0},
    )

    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        await client.get_my_assets(token_b=token_b)

    req = _last_request(httpx_mock)
    assert req.headers["content-type"] == "application/json"


# ── Test 6: aclose() closes owned client only ─────────────────────────────────

@pytest.mark.asyncio
async def test_aclose_closes_owned_client(config: Any) -> None:
    """aclose() must close the internally created AsyncClient."""
    client = ITMcpClient(config)
    assert not client._http.is_closed
    await client.aclose()
    assert client._http.is_closed


@pytest.mark.asyncio
async def test_aclose_does_not_close_injected_client(config: Any) -> None:
    """aclose() must NOT close an externally injected AsyncClient."""
    external = httpx.AsyncClient()
    client = ITMcpClient(config, http=external)
    await client.aclose()
    assert not external.is_closed
    await external.aclose()  # Clean up.


# ── Test 7: Each tool call is independent (no shared state) ───────────────────

@pytest.mark.asyncio
async def test_independent_calls_do_not_bleed_state(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """Consecutive calls must each send their own independent headers."""
    token_1 = _make_token("it-token-call-1")
    token_2 = _make_token("it-token-call-2")

    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        json={"assets": [{"asset_id": "A1", "model": "M1", "type": "laptop", "available_count": 1}]},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        json={"assets": []},
    )

    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        await client.list_available_assets(
            token_b=token_1, request_id="it-rid-first"
        )
        await client.list_available_assets(
            token_b=token_2, request_id="it-rid-second"
        )

    requests = httpx_mock.get_requests()
    assert len(requests) == 2

    req1, req2 = requests[0], requests[1]
    assert req1.headers["authorization"] == "Bearer it-token-call-1"
    assert req1.headers["x-request-id"] == "it-rid-first"
    assert req2.headers["authorization"] == "Bearer it-token-call-2"
    assert req2.headers["x-request-id"] == "it-rid-second"


# ── Sprint 4 S4.2 — get_my_assets posts empty body, parses {assets,total} ───

@pytest.mark.asyncio
async def test_get_my_assets_posts_empty_body(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """Sprint 4 S4.2: get_my_assets carries no employee_id arg — identity is
    derived server-side from the validated token's ``username`` claim. The
    body is an empty JSON object; the response is ``{assets, total}``.
    """
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/get_my_assets",
        json={"assets": [], "total": 0},
    )

    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        result = await client.get_my_assets(
            token_b=token_b, request_id="test-rid"
        )

    req = _last_request(httpx_mock)
    body = json.loads(req.content)
    assert body == {}
    assert result["total"] == 0
    assert result["assets"] == []


# ── Additional: list_available_assets passes asset_type filter ────────────────

@pytest.mark.asyncio
async def test_list_available_assets_sends_asset_type_filter(
    httpx_mock: HTTPXMock, config: Any
) -> None:
    """list_available_assets must include asset_type in body when provided."""
    token_b = _make_token()
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/mcp/tools/list_available_assets",
        json={"assets": [{"asset_id": "MBP-14", "model": "MacBook Pro 14", "type": "laptop", "available_count": 2}]},
    )

    async with httpx.AsyncClient() as http:
        client = ITMcpClient(config, http=http)
        await client.list_available_assets(
            token_b=token_b, asset_type="laptop", request_id="test-rid"
        )

    req = _last_request(httpx_mock)
    body = json.loads(req.content)
    assert body["asset_type"] == "laptop"
