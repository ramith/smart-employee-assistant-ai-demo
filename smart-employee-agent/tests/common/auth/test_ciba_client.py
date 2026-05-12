"""Tests for common/auth/ciba_client.py — Wave 3, Sprint 1.

Test inventory (18 tests):
  initiate:
  1.  Happy path: CIBARequest populated with all fields from IS response
  2.  IS returns 401 → CIBAInitiationError (ERR-CIBA-001)
  3.  IS returns 400 → CIBAInitiationError
  4.  200 body missing auth_req_id → CIBAInitiationError
  5.  external channel but no auth_url: logs warning, does not raise

  poll_for_token:
  6.  First poll returns access_token → OAuthToken
  7.  authorization_pending × 2, then success → returns OAuthToken (proves retry)
  8.  slow_down → interval bumps by 5, then success
  9.  expired_token → CIBAExpiredError
 10.  access_denied → CIBADeniedError
 11.  unknown error code → CIBAPollError
 12.  max_wait_seconds=0.5 with perpetual pending → CIBATimeoutError (budget)
 13.  cancel_event set before poll cycle → CIBATimeoutError(reason='cancelled')
 14.  asyncio.CancelledError propagates (not swallowed)
 15.  httpx.NetworkError on one poll, then success → returns OAuthToken

  acquire_obo:
 16.  on_consent_required called exactly once with the CIBARequest
 17.  on_consent_required NOT called when initiate raises
 18.  acquire_obo returns (CIBARequest, OAuthToken) on success
"""

from __future__ import annotations

# ── Bootstrap (mirrors project conftest pattern) ──────────────────────────────
# Load only the modules under test; bypass stale __init__.py files.

import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _load(dotted: str, rel: str) -> types.ModuleType:
    if dotted in sys.modules:
        return sys.modules[dotted]
    path = _ROOT / rel
    spec = importlib.util.spec_from_file_location(dotted, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


for _pkg in ("common", "common.auth"):
    if _pkg not in sys.modules:
        stub = types.ModuleType(_pkg)
        stub.__package__ = _pkg
        stub.__path__ = [str(_ROOT / _pkg.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_pkg] = stub

_load("common.auth.models", "common/auth/models.py")
_load("common.auth.errors", "common/auth/errors.py")
_load("common.auth.ciba_client", "common/auth/ciba_client.py")

# ── Imports ───────────────────────────────────────────────────────────────────

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_httpx

from common.auth.ciba_client import CIBAClient, CIBAClientConfig, CIBARequest
from common.auth.errors import (
    CIBADeniedError,
    CIBAExpiredError,
    CIBAInitiationError,
    CIBAPollError,
    CIBATimeoutError,
)
from common.auth.models import OAuthToken

# ── Constants ──────────────────────────────────────────────────────────────────

IS_BASE = "https://is.example.com:9443"
CIBA_URL = f"{IS_BASE}/oauth2/ciba"
TOKEN_URL = f"{IS_BASE}/oauth2/token"

OAUTH_CLIENT_ID = "agent-app-client-id"
OAUTH_CLIENT_SECRET = "agent-app-client-secret"
LOGIN_HINT = "user-uuid-0000-0000-0001"
BINDING_MSG = "HR Agent wants to view leave balance — request abcd1234"
ACTOR_TOKEN = "eyJhbGciOiJSUzI1NiJ9.actor.sig"
AUTH_REQ_ID = "015a2f21-6844-4e9c-80dd-a608544dcd8f"
AUTH_URL = f"{IS_BASE}/oauth2/ciba_authorize?authCodeKey=test-key"

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_client(
    httpx_mock: pytest_httpx.HTTPXMock,
    *,
    insecure_tls: bool = False,
    notification_channel: str = "external",
    default_max_wait_seconds: float = 300.0,
) -> CIBAClient:
    """Build a CIBAClient with an injected httpx client intercepted by pytest-httpx."""
    config = CIBAClientConfig(
        is_base_url=IS_BASE,
        insecure_tls=insecure_tls,
        notification_channel=notification_channel,
        default_max_wait_seconds=default_max_wait_seconds,
    )
    http = httpx.AsyncClient(
        verify=not insecure_tls,
        headers={"Accept": "application/json"},
    )
    return CIBAClient(config=config, http=http)


def _ciba_success_body() -> dict[str, Any]:
    """Minimal valid /oauth2/ciba success response."""
    return {
        "auth_req_id": AUTH_REQ_ID,
        "interval": 2,
        "auth_url": AUTH_URL,
        "expires_in": 120,
    }


def _token_success_body() -> dict[str, Any]:
    """Minimal valid /oauth2/token success response after CIBA consent."""
    return {
        "access_token": "obo-access-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid hr.read",
    }


def _make_ciba_request(
    *,
    auth_req_id: str = AUTH_REQ_ID,
    interval_s: int = 2,
    expires_in_s: int = 120,
) -> CIBARequest:
    return CIBARequest(
        auth_req_id=auth_req_id,
        auth_url=AUTH_URL,
        interval_s=interval_s,
        expires_in_s=expires_in_s,
        issued_at=datetime.now(tz=timezone.utc),
    )


# ── 1. initiate happy path ─────────────────────────────────────────────────────


class TestInitiateHappyPath:
    """CIBARequest is fully populated from the IS response."""

    @pytest.mark.asyncio
    async def test_all_fields_populated(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=CIBA_URL,
            json=_ciba_success_body(),
            status_code=200,
        )
        client = _make_client(httpx_mock)
        req = await client.initiate(
            oauth_client_id=OAUTH_CLIENT_ID,
            oauth_client_secret=OAUTH_CLIENT_SECRET,
            login_hint=LOGIN_HINT,
            binding_message=BINDING_MSG,
            actor_token=ACTOR_TOKEN,
        )
        await client.aclose()

        assert req.auth_req_id == AUTH_REQ_ID
        assert req.auth_url == AUTH_URL
        assert req.interval_s == 2
        assert req.expires_in_s == 120
        assert isinstance(req.issued_at, datetime)
        assert req.issued_at.tzinfo is not None  # timezone-aware

    @pytest.mark.asyncio
    async def test_request_uses_basic_auth(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """POST /oauth2/ciba must use Basic auth (client_id:secret)."""
        httpx_mock.add_response(method="POST", url=CIBA_URL, json=_ciba_success_body())
        client = _make_client(httpx_mock)
        await client.initiate(
            oauth_client_id=OAUTH_CLIENT_ID,
            oauth_client_secret=OAUTH_CLIENT_SECRET,
            login_hint=LOGIN_HINT,
            binding_message=BINDING_MSG,
            actor_token=ACTOR_TOKEN,
        )
        await client.aclose()

        [request] = httpx_mock.get_requests()
        assert request.headers.get("Authorization", "").startswith("Basic ")

    @pytest.mark.asyncio
    async def test_form_body_includes_actor_token(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """actor_token must appear in the POST body, not just the Authorization header."""
        httpx_mock.add_response(method="POST", url=CIBA_URL, json=_ciba_success_body())
        client = _make_client(httpx_mock)
        await client.initiate(
            oauth_client_id=OAUTH_CLIENT_ID,
            oauth_client_secret=OAUTH_CLIENT_SECRET,
            login_hint=LOGIN_HINT,
            binding_message=BINDING_MSG,
            actor_token=ACTOR_TOKEN,
        )
        await client.aclose()

        [request] = httpx_mock.get_requests()
        body = request.content.decode()
        assert "actor_token=" in body
        assert "login_hint=" in body
        assert "binding_message=" in body


# ── 2. initiate 401 → CIBAInitiationError ─────────────────────────────────────


class TestInitiate401:
    @pytest.mark.asyncio
    async def test_401_raises_ciba_initiation_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=CIBA_URL,
            status_code=401,
            json={"error": "unauthorized_client"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBAInitiationError) as exc_info:
            await client.initiate(
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
                login_hint=LOGIN_HINT,
                binding_message=BINDING_MSG,
                actor_token=ACTOR_TOKEN,
            )
        await client.aclose()
        assert exc_info.value.error_id == "ERR-CIBA-001"


# ── 3. initiate 400 → CIBAInitiationError ─────────────────────────────────────


class TestInitiate400:
    @pytest.mark.asyncio
    async def test_400_raises_ciba_initiation_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=CIBA_URL,
            status_code=400,
            json={"error": "invalid_request", "error_description": "missing login_hint"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBAInitiationError):
            await client.initiate(
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
                login_hint=LOGIN_HINT,
                binding_message=BINDING_MSG,
                actor_token=ACTOR_TOKEN,
            )
        await client.aclose()


# ── 4. initiate 200 but missing auth_req_id ────────────────────────────────────


class TestInitiateMissingAuthReqId:
    @pytest.mark.asyncio
    async def test_200_without_auth_req_id_raises(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=CIBA_URL,
            status_code=200,
            json={"interval": 2, "expires_in": 120},  # auth_req_id absent
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBAInitiationError) as exc_info:
            await client.initiate(
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
                login_hint=LOGIN_HINT,
                binding_message=BINDING_MSG,
                actor_token=ACTOR_TOKEN,
            )
        await client.aclose()
        assert "auth_req_id" in str(exc_info.value).lower() or \
               "auth_req_id" in str(exc_info.value.details).lower()


# ── 5. external channel without auth_url: warning, no raise ───────────────────


class TestInitiateNoAuthUrl:
    @pytest.mark.asyncio
    async def test_no_auth_url_does_not_raise(
        self, httpx_mock: pytest_httpx.HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing auth_url with external channel emits a warning but does NOT raise."""
        httpx_mock.add_response(
            method="POST",
            url=CIBA_URL,
            status_code=200,
            json={"auth_req_id": AUTH_REQ_ID, "interval": 2, "expires_in": 120},
        )
        import logging
        with caplog.at_level(logging.WARNING, logger="common.auth.ciba_client"):
            client = _make_client(httpx_mock, notification_channel="external")
            req = await client.initiate(
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
                login_hint=LOGIN_HINT,
                binding_message=BINDING_MSG,
                actor_token=ACTOR_TOKEN,
            )
            await client.aclose()

        assert req.auth_url == ""
        assert any("no_auth_url" in r.message or "auth_url" in r.message for r in caplog.records)


# ── 6. poll_for_token — first poll returns token ───────────────────────────────


class TestPollFirstSuccess:
    @pytest.mark.asyncio
    async def test_returns_oauth_token_on_first_success(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json=_token_success_body(),
        )
        client = _make_client(httpx_mock)
        token = await client.poll_for_token(
            ciba_request=_make_ciba_request(),
            oauth_client_id=OAUTH_CLIENT_ID,
            oauth_client_secret=OAUTH_CLIENT_SECRET,
        )
        await client.aclose()

        assert isinstance(token, OAuthToken)
        assert token.access_token == "obo-access-token"
        assert token.expires_in == 3600


# ── 7. poll_for_token — pending × 2, then success ─────────────────────────────


class TestPollPendingThenSuccess:
    @pytest.mark.asyncio
    async def test_retries_on_authorization_pending(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """Two 'authorization_pending' responses followed by success."""
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"error": "authorization_pending", "error_description": "not yet"},
        )
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"error": "authorization_pending", "error_description": "still waiting"},
        )
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json=_token_success_body(),
        )

        # Use a very short interval so the test is fast
        client = _make_client(httpx_mock)
        ciba_req = _make_ciba_request(interval_s=0)

        token = await client.poll_for_token(
            ciba_request=ciba_req,
            oauth_client_id=OAUTH_CLIENT_ID,
            oauth_client_secret=OAUTH_CLIENT_SECRET,
        )
        await client.aclose()

        assert isinstance(token, OAuthToken)
        assert token.access_token == "obo-access-token"
        # Three POST requests were made
        assert len(httpx_mock.get_requests()) == 3


# ── 8. poll_for_token — slow_down bumps interval ──────────────────────────────


class TestPollSlowDown:
    @pytest.mark.asyncio
    async def test_slow_down_increments_interval(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """slow_down must increment interval by 5 then sleep, then succeed."""
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"error": "slow_down"},
        )
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json=_token_success_body(),
        )

        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        import unittest.mock as mock
        with mock.patch("common.auth.ciba_client.asyncio.sleep", side_effect=_fake_sleep):
            client = _make_client(httpx_mock)
            ciba_req = _make_ciba_request(interval_s=2)
            token = await client.poll_for_token(
                ciba_request=ciba_req,
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
            )
            await client.aclose()

        assert isinstance(token, OAuthToken)
        # After slow_down: interval was 2, bumped to 7; sleep(7) must have been called
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 7


# ── 9. poll_for_token — expired_token → CIBAExpiredError ──────────────────────


class TestPollExpiredToken:
    @pytest.mark.asyncio
    async def test_expired_token_raises_ciba_expired_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"error": "expired_token", "error_description": "auth_req_id has expired"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBAExpiredError) as exc_info:
            await client.poll_for_token(
                ciba_request=_make_ciba_request(),
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
            )
        await client.aclose()
        assert exc_info.value.error_id == "ERR-CIBA-009"


# ── 10. poll_for_token — access_denied → CIBADeniedError ──────────────────────


class TestPollAccessDenied:
    @pytest.mark.asyncio
    async def test_access_denied_raises_ciba_denied_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"error": "access_denied", "error_description": "user denied"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBADeniedError) as exc_info:
            await client.poll_for_token(
                ciba_request=_make_ciba_request(),
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
            )
        await client.aclose()
        assert exc_info.value.error_id == "ERR-CIBA-005"


# ── 11. poll_for_token — unknown error → CIBAPollError ────────────────────────


class TestPollUnknownError:
    @pytest.mark.asyncio
    async def test_unknown_error_raises_ciba_poll_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"error": "server_error", "error_description": "unexpected failure"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBAPollError) as exc_info:
            await client.poll_for_token(
                ciba_request=_make_ciba_request(),
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
            )
        await client.aclose()
        assert exc_info.value.error_id == "ERR-CIBA-008"


# ── 12. poll_for_token — budget exhausted → CIBATimeoutError ──────────────────


class TestPollBudgetExhausted:
    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_timeout_when_budget_zero_and_pending(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """With max_wait_seconds=0 the deadline is in the past immediately.

        The loop condition ``_utc_now().timestamp() < deadline`` is False before
        the first iteration, so the body never executes and no HTTP call is made.
        We register a response but don't require it to be consumed.
        """
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"error": "authorization_pending"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBATimeoutError) as exc_info:
            await client.poll_for_token(
                ciba_request=_make_ciba_request(interval_s=0),
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
                max_wait_seconds=0.0,
            )
        await client.aclose()
        assert exc_info.value.error_id == "ERR-CIBA-010"


# ── 13. poll_for_token — cancel_event set → CIBATimeoutError(reason=cancelled) ─


class TestPollCancelEvent:
    @pytest.mark.asyncio
    async def test_cancel_event_raises_timeout_with_cancelled_reason(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """When cancel_event is set the loop raises CIBATimeoutError(reason=cancelled)."""
        # The event is set before the first poll cycle, so no HTTP call is made.
        cancel = asyncio.Event()
        cancel.set()

        client = _make_client(httpx_mock, default_max_wait_seconds=300.0)
        with pytest.raises(CIBATimeoutError) as exc_info:
            await client.poll_for_token(
                ciba_request=_make_ciba_request(),
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
                cancel_event=cancel,
            )
        await client.aclose()
        assert exc_info.value.error_id == "ERR-CIBA-010"
        assert exc_info.value.details.get("reason") == "cancelled"


# ── 14. poll_for_token does NOT swallow asyncio.CancelledError ────────────────


class TestPollCancelledErrorPropagates:
    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """asyncio.CancelledError must propagate; poll_for_token must not catch it.

        Strategy: use a reusable "authorization_pending" response so the mock
        never runs out.  Use a non-zero interval (0.05 s) so asyncio.sleep()
        actually yields to the event loop — that is when task.cancel() is
        processed and CancelledError is injected into the sleep await point.
        """
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"error": "authorization_pending"},
            is_reusable=True,
        )

        client = _make_client(httpx_mock, default_max_wait_seconds=60.0)
        # interval_s=1 is long enough that the task will always be sleeping
        # (not inside the HTTP call) when we cancel it.
        ciba_req = _make_ciba_request(interval_s=1)

        async def _run() -> OAuthToken:
            return await client.poll_for_token(
                ciba_request=ciba_req,
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
            )

        task = asyncio.create_task(_run())
        # Wait long enough for the task to enter asyncio.sleep(interval).
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        await client.aclose()


# ── 15. poll_for_token — network error retry ───────────────────────────────────


class TestPollNetworkErrorRetry:
    @pytest.mark.asyncio
    async def test_network_error_then_success(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """httpx.NetworkError on one poll is retried; success on the next poll."""
        httpx_mock.add_exception(httpx.ConnectError("simulated network error"))
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json=_token_success_body(),
        )

        import unittest.mock as mock
        with mock.patch("common.auth.ciba_client.asyncio.sleep"):
            client = _make_client(httpx_mock)
            token = await client.poll_for_token(
                ciba_request=_make_ciba_request(interval_s=0),
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
            )
            await client.aclose()

        assert isinstance(token, OAuthToken)
        assert token.access_token == "obo-access-token"


# ── 16. acquire_obo — on_consent_required called once ─────────────────────────


class TestAcquireOboConsentHook:
    @pytest.mark.asyncio
    async def test_on_consent_required_called_exactly_once(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="POST", url=CIBA_URL, json=_ciba_success_body())
        httpx_mock.add_response(method="POST", url=TOKEN_URL, json=_token_success_body())

        hook_calls: list[CIBARequest] = []

        async def _hook(req: CIBARequest) -> None:
            hook_calls.append(req)

        client = _make_client(httpx_mock)
        ciba_req, token = await client.acquire_obo(
            oauth_client_id=OAUTH_CLIENT_ID,
            oauth_client_secret=OAUTH_CLIENT_SECRET,
            login_hint=LOGIN_HINT,
            binding_message=BINDING_MSG,
            actor_token=ACTOR_TOKEN,
            on_consent_required=_hook,
        )
        await client.aclose()

        assert len(hook_calls) == 1
        assert hook_calls[0].auth_req_id == AUTH_REQ_ID
        assert isinstance(token, OAuthToken)


# ── 17. acquire_obo — hook NOT called when initiate raises ────────────────────


class TestAcquireOboHookNotCalledOnInitiateFailure:
    @pytest.mark.asyncio
    async def test_hook_not_called_when_initiate_raises(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=CIBA_URL,
            status_code=401,
            json={"error": "unauthorized_client"},
        )

        hook_calls: list[CIBARequest] = []

        async def _hook(req: CIBARequest) -> None:
            hook_calls.append(req)  # must never be reached

        client = _make_client(httpx_mock)
        with pytest.raises(CIBAInitiationError):
            await client.acquire_obo(
                oauth_client_id=OAUTH_CLIENT_ID,
                oauth_client_secret=OAUTH_CLIENT_SECRET,
                login_hint=LOGIN_HINT,
                binding_message=BINDING_MSG,
                actor_token=ACTOR_TOKEN,
                on_consent_required=_hook,
            )
        await client.aclose()

        assert len(hook_calls) == 0


# ── 18. acquire_obo — returns (CIBARequest, OAuthToken) ───────────────────────


class TestAcquireOboReturnShape:
    @pytest.mark.asyncio
    async def test_returns_tuple_of_ciba_request_and_token(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="POST", url=CIBA_URL, json=_ciba_success_body())
        httpx_mock.add_response(method="POST", url=TOKEN_URL, json=_token_success_body())

        client = _make_client(httpx_mock)
        result = await client.acquire_obo(
            oauth_client_id=OAUTH_CLIENT_ID,
            oauth_client_secret=OAUTH_CLIENT_SECRET,
            login_hint=LOGIN_HINT,
            binding_message=BINDING_MSG,
            actor_token=ACTOR_TOKEN,
        )
        await client.aclose()

        assert isinstance(result, tuple) and len(result) == 2
        ciba_req, token = result
        assert isinstance(ciba_req, CIBARequest)
        assert isinstance(token, OAuthToken)
        assert ciba_req.auth_req_id == AUTH_REQ_ID
        assert token.access_token == "obo-access-token"


# ── login_hint is sent verbatim (S5.18 — IS Multi-Attribute Login resolves the email) ──


class TestLoginHintVerbatim:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hint",
        [
            "employee_user@example.com",            # email sub — resolved by IS MAL
            "sivanoly@wso2.com",                    # email sub, username != local-part
            "Nesaratnam",                           # bare username
            "2048ad8c-16a6-4ec1-bb63-b38300118f28",  # user-id UUID
        ],
    )
    async def test_initiate_sends_login_hint_unchanged(
        self, httpx_mock: pytest_httpx.HTTPXMock, hint: str
    ) -> None:
        """initiate() POSTs the login_hint exactly as given — no @domain stripping.

        Since S5.18 IS resolves an email login_hint via Multi-Attribute Login
        (and a UUID via its userid branch), so we no longer mangle the value.
        """
        httpx_mock.add_response(method="POST", url=CIBA_URL, json=_ciba_success_body())
        client = _make_client(httpx_mock)
        await client.initiate(
            oauth_client_id=OAUTH_CLIENT_ID,
            oauth_client_secret=OAUTH_CLIENT_SECRET,
            login_hint=hint,
            binding_message=BINDING_MSG,
            actor_token=ACTOR_TOKEN,
        )
        await client.aclose()

        [request] = httpx_mock.get_requests()
        # urllib.parse.parse_qs decodes %40 → @ so we compare the real value.
        from urllib.parse import parse_qs
        sent = parse_qs(request.content.decode())["login_hint"][0]
        assert sent == hint

    def test_normalize_helper_removed(self) -> None:
        """The S5.9-era ``_normalize_login_hint`` workaround is gone (S5.18)."""
        import common.auth.ciba_client as _mod
        assert not hasattr(_mod, "_normalize_login_hint")

    @pytest.mark.asyncio
    async def test_initiate_passes_uuid_login_hint_through(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(method="POST", url=CIBA_URL, json=_ciba_success_body())
        client = _make_client(httpx_mock)
        await client.initiate(
            oauth_client_id=OAUTH_CLIENT_ID,
            oauth_client_secret=OAUTH_CLIENT_SECRET,
            login_hint="2048ad8c-16a6-4ec1-bb63-b38300118f28",
            binding_message=BINDING_MSG,
            actor_token=ACTOR_TOKEN,
        )
        await client.aclose()
        [request] = httpx_mock.get_requests()
        assert "login_hint=2048ad8c-16a6-4ec1-bb63-b38300118f28" in request.content.decode()
