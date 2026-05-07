"""Tests for orchestrator/agent_registry/discovery.py — Wave 4, Sprint 1.

Coverage targets
----------------
1.  fetch() happy path: mock GET returns valid JSON → returns AgentCard
2.  fetch() URL not in allowlist → AgentDiscoveryError; mock NOT called
3.  fetch() HTTP 500 → AgentDiscoveryError
4.  fetch() HTTP 200 but body fails AgentCard validation → AgentDiscoveryError
5.  fetch() HTTP 200 with text/plain Content-Type → still parsed via
    model_validate_json (Content-Type is ignored)
6.  fetch_all() with allowlist of 3 URLs → fetches 3, returns dict of all 3 by id
7.  fetch_all() one URL fails → logs WARN, returns dict of the 2 that succeeded
8.  aclose() only closes owned client; injected client is NOT closed
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from orchestrator.agent_registry.discovery import (
    AgentDiscovery,
    AgentDiscoveryError,
    DiscoveryConfig,
)

# ---------------------------------------------------------------------------
# Fixture data — embedded valid agent-card JSON (mirrors
# tests/fixtures/agent_cards/hr_agent_valid.json)
# ---------------------------------------------------------------------------

_HR_CARD_DICT = {
    "schema_version": "v3-custom",
    "id": "hr_agent",
    "name": "HR Agent",
    "description": "Handles HR queries: leave, time-off, employee info.",
    "url": "https://hr.smart-employee.local",
    "oauth_client_id": "hr_agent-oauth-client-id-abc123",
    "api_version": "1.0.0",
    "skills": [
        {
            "id": "hr.approve_leave",
            "name": "Approve leave request",
            "description": "Approve or reject a leave request by id.",
            "scope": "hr_approve_a2a",
            "required_scopes": ["hr_approve_a2a"],
        },
        {
            "id": "hr.get_leave_balance",
            "name": "Get leave balance",
            "description": "Return the remaining leave balance for the authenticated user.",
            "scope": "hr_read_a2a",
            "required_scopes": ["hr_read_a2a"],
        },
    ],
    "capabilities": {"streaming": False, "pushNotifications": False},
    "auth": {
        "scheme": "oauth2",
        "issuer": "https://api.asgardeo.io/t/ddademo/oauth2/token",
        "audience": "https://hr.smart-employee.local",
    },
}

_IT_CARD_DICT = {
    "schema_version": "v3-custom",
    "id": "it_agent",
    "name": "IT Agent",
    "description": "Handles IT asset queries.",
    "url": "https://it.smart-employee.local",
    "oauth_client_id": "it_agent-oauth-client-id-def456",
    "api_version": "1.0.0",
    "skills": [
        {
            "id": "it.list_assets",
            "name": "List available assets",
            "description": "List assets available for assignment.",
            "scope": "it_read_a2a",
            "required_scopes": ["it_read_a2a"],
        }
    ],
    "capabilities": {"streaming": False, "pushNotifications": False},
    "auth": {
        "scheme": "oauth2",
        "issuer": "https://api.asgardeo.io/t/ddademo/oauth2/token",
        "audience": "https://it.smart-employee.local",
    },
}

_FINANCE_CARD_DICT = {
    "schema_version": "v3-custom",
    "id": "finance-agent",
    "name": "Finance Agent",
    "description": "Handles finance queries.",
    "url": "https://finance.smart-employee.local",
    "oauth_client_id": "finance-agent-oauth-client-id-ghi789",
    "api_version": "1.0.0",
    "skills": [
        {
            "id": "finance.get_budget",
            "name": "Get budget",
            "description": "Return the current department budget.",
            "scope": "finance_read_a2a",
            "required_scopes": ["finance_read_a2a"],
        }
    ],
    "capabilities": {"streaming": False, "pushNotifications": False},
    "auth": {
        "scheme": "oauth2",
        "issuer": "https://api.asgardeo.io/t/ddademo/oauth2/token",
        "audience": "https://finance.smart-employee.local",
    },
}

_HR_CARD_JSON: str = json.dumps(_HR_CARD_DICT)
_IT_CARD_JSON: str = json.dumps(_IT_CARD_DICT)
_FINANCE_CARD_JSON: str = json.dumps(_FINANCE_CARD_DICT)

_HR_CARD_URL = "http://hr_agent:8001/.well-known/agent-card.json"
_IT_CARD_URL = "http://it_agent:8002/.well-known/agent-card.json"
_FINANCE_CARD_URL = "http://finance-agent:8003/.well-known/agent-card.json"
_UNKNOWN_URL = "http://evil.example.com/card.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(
    body: str,
    status_code: int = 200,
    content_type: str = "application/json",
) -> MagicMock:
    """Return a mock ``httpx.Response``."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = body
    resp.headers = {"content-type": content_type}
    return resp


def _make_mock_http(responses: dict[str, MagicMock]) -> MagicMock:
    """Return a mock ``httpx.AsyncClient`` whose ``get()`` dispatches by URL.

    Args:
        responses: Mapping from URL string to mock ``httpx.Response``.
    """
    mock_http = MagicMock(spec=httpx.AsyncClient)

    async def _get(url: str, **_kwargs: object) -> MagicMock:
        return responses[url]

    mock_http.get = AsyncMock(side_effect=_get)
    mock_http.aclose = AsyncMock()
    return mock_http


def _single_url_config(url: str) -> DiscoveryConfig:
    return DiscoveryConfig(allowed_card_urls=frozenset([url]))


# ---------------------------------------------------------------------------
# Test 1 — fetch() happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_happy_path_returns_agent_card() -> None:
    """fetch() returns a parsed AgentCard when the server responds with 200 + valid JSON."""
    mock_http = _make_mock_http(
        {_HR_CARD_URL: _make_mock_response(_HR_CARD_JSON)}
    )
    discovery = AgentDiscovery(_single_url_config(_HR_CARD_URL), http=mock_http)

    card = await discovery.fetch(_HR_CARD_URL)

    assert card.id == "hr_agent"
    assert card.label == "HR Agent"
    assert len(card.skills) == 2
    mock_http.get.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2 — fetch() URL not in allowlist → error; mock NOT called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_rejects_url_not_in_allowlist() -> None:
    """fetch() raises AgentDiscoveryError immediately for a non-allowlisted URL."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock()
    discovery = AgentDiscovery(_single_url_config(_HR_CARD_URL), http=mock_http)

    with pytest.raises(AgentDiscoveryError, match="not in allowlist"):
        await discovery.fetch(_UNKNOWN_URL)

    # Network must not have been called
    mock_http.get.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — fetch() HTTP 500 → AgentDiscoveryError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_http_500_raises_discovery_error() -> None:
    """fetch() raises AgentDiscoveryError when the server returns a 500 status."""
    mock_http = _make_mock_http(
        {_HR_CARD_URL: _make_mock_response("Internal Server Error", status_code=500)}
    )
    discovery = AgentDiscovery(_single_url_config(_HR_CARD_URL), http=mock_http)

    with pytest.raises(AgentDiscoveryError, match="Non-200"):
        await discovery.fetch(_HR_CARD_URL)


# ---------------------------------------------------------------------------
# Test 4 — fetch() HTTP 200 but invalid body → AgentDiscoveryError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_invalid_body_raises_discovery_error() -> None:
    """fetch() raises AgentDiscoveryError when the 200 body fails AgentCard validation."""
    invalid_json = json.dumps({"not": "a valid agent card"})
    mock_http = _make_mock_http(
        {_HR_CARD_URL: _make_mock_response(invalid_json)}
    )
    discovery = AgentDiscovery(_single_url_config(_HR_CARD_URL), http=mock_http)

    with pytest.raises(AgentDiscoveryError, match="failed validation"):
        await discovery.fetch(_HR_CARD_URL)


# ---------------------------------------------------------------------------
# Test 5 — fetch() text/plain Content-Type → still parsed (Content-Type ignored)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_ignores_content_type_and_parses_body() -> None:
    """fetch() successfully parses a valid card body even when Content-Type is text/plain."""
    mock_http = _make_mock_http(
        {
            _HR_CARD_URL: _make_mock_response(
                _HR_CARD_JSON,
                status_code=200,
                content_type="text/plain",
            )
        }
    )
    discovery = AgentDiscovery(_single_url_config(_HR_CARD_URL), http=mock_http)

    card = await discovery.fetch(_HR_CARD_URL)

    assert card.id == "hr_agent"


# ---------------------------------------------------------------------------
# Test 6 — fetch_all() with 3 allowlisted URLs → returns all 3 by id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_all_returns_all_cards_by_id() -> None:
    """fetch_all() fetches every URL in the allowlist and returns a dict keyed by card.id."""
    mock_http = _make_mock_http(
        {
            _HR_CARD_URL: _make_mock_response(_HR_CARD_JSON),
            _IT_CARD_URL: _make_mock_response(_IT_CARD_JSON),
            _FINANCE_CARD_URL: _make_mock_response(_FINANCE_CARD_JSON),
        }
    )
    config = DiscoveryConfig(
        allowed_card_urls=frozenset([_HR_CARD_URL, _IT_CARD_URL, _FINANCE_CARD_URL])
    )
    discovery = AgentDiscovery(config, http=mock_http)

    cards = await discovery.fetch_all()

    assert set(cards.keys()) == {"hr_agent", "it_agent", "finance-agent"}
    assert cards["hr_agent"].label == "HR Agent"
    assert cards["it_agent"].label == "IT Agent"
    assert cards["finance-agent"].label == "Finance Agent"
    assert mock_http.get.await_count == 3


# ---------------------------------------------------------------------------
# Test 7 — fetch_all() one URL fails → logs warning, returns remaining 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_all_skips_failed_url_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """fetch_all() skips URLs that fail and logs a WARNING; partial results are returned."""
    import logging

    mock_http = _make_mock_http(
        {
            _HR_CARD_URL: _make_mock_response(_HR_CARD_JSON),
            _IT_CARD_URL: _make_mock_response("bad body — not valid JSON card"),
            _FINANCE_CARD_URL: _make_mock_response(_FINANCE_CARD_JSON),
        }
    )
    config = DiscoveryConfig(
        allowed_card_urls=frozenset([_HR_CARD_URL, _IT_CARD_URL, _FINANCE_CARD_URL])
    )
    discovery = AgentDiscovery(config, http=mock_http)

    with caplog.at_level(logging.WARNING, logger="orchestrator.agent_registry.discovery"):
        cards = await discovery.fetch_all()

    # The failing URL is absent; the two good ones are present
    assert "hr_agent" in cards
    assert "finance-agent" in cards
    assert "it_agent" not in cards

    # At least one WARNING was emitted for the failing URL
    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(_IT_CARD_URL in m for m in warning_msgs)


# ---------------------------------------------------------------------------
# Test 8 — aclose() only closes owned client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_owned_client_only() -> None:
    """aclose() closes the client only when AgentDiscovery built it (owned=True)."""
    # --- Case A: owned client (http=None → internally built) ---
    # Build the stand-in *before* patching so we can use the real class as spec.
    owned_instance = MagicMock()
    owned_instance.aclose = AsyncMock()

    with patch(
        "orchestrator.agent_registry.discovery.httpx.AsyncClient",
        return_value=owned_instance,
    ):
        owned_discovery = AgentDiscovery(
            DiscoveryConfig(allowed_card_urls=frozenset([_HR_CARD_URL]))
        )
        await owned_discovery.aclose()

    owned_instance.aclose.assert_awaited_once()

    # --- Case B: injected client (http=mock_http → not owned) ---
    injected_http = MagicMock()
    injected_http.aclose = AsyncMock()

    injected_discovery = AgentDiscovery(
        DiscoveryConfig(allowed_card_urls=frozenset([_HR_CARD_URL])),
        http=injected_http,
    )
    await injected_discovery.aclose()

    injected_http.aclose.assert_not_called()
