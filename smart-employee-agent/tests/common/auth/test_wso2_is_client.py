"""Tests for common/auth/wso2_is_client.py — Wave 2, Sprint 1.

Covers:
1.  URL properties constructed correctly from base_url
2.  post_authorize sends correct form fields and Basic auth; parses {flowId, nextStep}
3.  post_authorize on 4xx raises CIBAInitiationError
4.  post_authn returns code from authData.code
5.  post_authn returns code from top-level code key (fallback shape)
6.  exchange_code without actor_token — correct body, parses OAuthToken with expires_at
7.  exchange_code WITH actor_token — actor_token in BODY, not Authorization header (F-01 / C1)
8.  client_credentials — correct grant_type body, returns OAuthToken
9.  insecure_tls=True passes verify=False to httpx (owned client)
10. aclose closes owned client; does NOT close injected client
11. post_authn on non-200 raises CIBAInitiationError
12. exchange_code on non-2xx raises AuthError
"""

from __future__ import annotations

# ── Bootstrap (mirrors conftest pattern) ──────────────────────────────────────
# The root conftest already registers the 'common' and 'common.auth' package stubs
# and loads common.auth.models into sys.modules.  We load the two extra modules
# that this test file depends on using the same importlib.util technique, so that
# they live under the correct dotted names and can be imported normally below.

import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/

# Ensure package stubs exist (conftest does this first, but be idempotent)
for _pkg in ("common", "common.auth"):
    if _pkg not in sys.modules:
        _stub = types.ModuleType(_pkg)
        _stub.__package__ = _pkg
        _stub.__path__ = [str(_ROOT / _pkg.replace(".", "/"))]  # type: ignore[assignment]
        sys.modules[_pkg] = _stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]
    file_path = _ROOT / rel_path
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted_name.rsplit(".", 1)[0] if "." in dotted_name else ""
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Load dependency chain in order
_load_module("common.auth.models", "common/auth/models.py")
_load_module("common.auth.errors", "common/auth/errors.py")
_load_module("common.auth.wso2_is_client", "common/auth/wso2_is_client.py")

# ── Imports ────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_httpx

from common.auth.errors import AuthError, CIBAInitiationError
from common.auth.models import OAuthToken
from common.auth.wso2_is_client import WSO2ISClient, WSO2ISClientConfig

# ── Fixtures / helpers ─────────────────────────────────────────────────────────

BASE_URL = "https://is.example.com:9443"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
REDIRECT_URI = "http://localhost:9999/agent-callback"

AUTHORIZE_URL = f"{BASE_URL}/oauth2/authorize"
AUTHN_URL = f"{BASE_URL}/oauth2/authn"
TOKEN_URL = f"{BASE_URL}/oauth2/token"
JWKS_URL = f"{BASE_URL}/oauth2/jwks"


def _make_client(httpx_mock: pytest_httpx.HTTPXMock, *, insecure_tls: bool = False) -> WSO2ISClient:
    """Build a WSO2ISClient whose httpx.AsyncClient is intercepted by pytest-httpx."""
    cfg = WSO2ISClientConfig(base_url=BASE_URL, insecure_tls=insecure_tls)
    # Inject the mock-intercepted client so pytest-httpx can intercept its calls
    http = httpx.AsyncClient(verify=not insecure_tls, headers={"Accept": "application/json"})
    return WSO2ISClient(cfg, http=http)


def _token_response_body() -> dict[str, Any]:
    return {
        "access_token": "eyJhbGciOiJSUzI1NiJ9.payload.sig",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid hr.read",
    }


# ── 1. URL properties ─────────────────────────────────────────────────────────


class TestURLProperties:
    """URL properties derive correctly from base_url."""

    def setup_method(self) -> None:
        cfg = WSO2ISClientConfig(base_url=BASE_URL)
        self.client = WSO2ISClient(cfg)

    def test_authorize_url(self) -> None:
        assert self.client.authorize_url == f"{BASE_URL}/oauth2/authorize"

    def test_authn_url(self) -> None:
        assert self.client.authn_url == f"{BASE_URL}/oauth2/authn"

    def test_token_url(self) -> None:
        assert self.client.token_url == f"{BASE_URL}/oauth2/token"

    def test_jwks_url(self) -> None:
        assert self.client.jwks_url == f"{BASE_URL}/oauth2/jwks"

    def test_issuer_equals_token_url(self) -> None:
        """Per C4 probe: WSO2 IS sets iss == token endpoint URL."""
        assert self.client.issuer == self.client.token_url

    def test_different_base_url(self) -> None:
        other = WSO2ISClientConfig(base_url="https://other-is.example.com:9444")
        c = WSO2ISClient(other)
        assert c.authorize_url == "https://other-is.example.com:9444/oauth2/authorize"
        assert c.token_url == "https://other-is.example.com:9444/oauth2/token"

    @pytest.mark.asyncio
    async def test_teardown(self) -> None:
        await self.client.aclose()


# ── 2. post_authorize — happy path ────────────────────────────────────────────


class TestPostAuthorize:
    """post_authorize sends the correct form fields and Basic auth."""

    @pytest.mark.asyncio
    async def test_sends_basic_auth_and_form_fields(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """post_authorize must use Basic auth and include all required form fields."""
        flow_body = {
            "flowId": "test-flow-id-001",
            "nextStep": {"authenticators": [{"authenticatorId": "auth-basic-001"}]},
        }
        httpx_mock.add_response(
            method="POST",
            url=AUTHORIZE_URL,
            json=flow_body,
            status_code=200,
        )
        client = _make_client(httpx_mock)
        result = await client.post_authorize(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scope="openid internal_login",
            code_challenge="test-challenge",
            code_challenge_method="S256",
            response_mode="direct",
        )
        await client.aclose()

        # Verify parsed response
        assert result["flowId"] == "test-flow-id-001"
        assert result["nextStep"]["authenticators"][0]["authenticatorId"] == "auth-basic-001"

        # Verify the outbound request
        [request] = httpx_mock.get_requests()
        assert request.method == "POST"
        # Basic auth header must be present
        assert "Authorization" in request.headers
        auth_header = request.headers["Authorization"]
        assert auth_header.startswith("Basic ")

        # Form body must include required fields
        content = request.content.decode()
        assert "response_mode=direct" in content
        assert "code_challenge=test-challenge" in content
        assert "code_challenge_method=S256" in content
        assert "grant_type" not in content  # authorize step has no grant_type

    @pytest.mark.asyncio
    async def test_returns_full_body(self, httpx_mock: pytest_httpx.HTTPXMock) -> None:
        """post_authorize returns the full parsed JSON body from IS."""
        flow_body = {
            "flowId": "flow-abc",
            "nextStep": {"authenticators": [{"authenticatorId": "BasicAuthenticator"}]},
            "links": [],
        }
        httpx_mock.add_response(method="POST", url=AUTHORIZE_URL, json=flow_body)
        client = _make_client(httpx_mock)
        result = await client.post_authorize(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scope="openid",
            code_challenge="ch",
        )
        await client.aclose()
        assert result == flow_body


# ── 3. post_authorize — error path ────────────────────────────────────────────


class TestPostAuthorizeErrors:
    """post_authorize raises CIBAInitiationError on non-200 responses."""

    @pytest.mark.asyncio
    async def test_400_raises_ciba_initiation_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=AUTHORIZE_URL,
            status_code=400,
            json={"error": "unauthorized_client", "error_description": "App-Native Auth not enabled"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBAInitiationError) as exc_info:
            await client.post_authorize(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                redirect_uri=REDIRECT_URI,
                scope="openid",
                code_challenge="ch",
            )
        await client.aclose()
        assert exc_info.value.error_id == "ERR-CIBA-001"

    @pytest.mark.asyncio
    async def test_500_raises_ciba_initiation_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=AUTHORIZE_URL,
            status_code=500,
            text="Internal Server Error",
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBAInitiationError):
            await client.post_authorize(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                redirect_uri=REDIRECT_URI,
                scope="openid",
                code_challenge="ch",
            )
        await client.aclose()


# ── 4 + 5. post_authn — code extraction ───────────────────────────────────────


class TestPostAuthn:
    """post_authn extracts the code from both IS response shapes."""

    @pytest.mark.asyncio
    async def test_extracts_code_from_auth_data_shape(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """Standard WSO2 IS 7.2 response: authData.code (C4/C8 empirical)."""
        httpx_mock.add_response(
            method="POST",
            url=AUTHN_URL,
            json={"authData": {"code": "auth-code-from-authn"}},
        )
        client = _make_client(httpx_mock)
        code = await client.post_authn(
            flow_id="flow-001",
            authenticator_id="BasicAuthenticator",
            params={"username": "agent-uuid", "password": "secret"},
        )
        await client.aclose()
        assert code == "auth-code-from-authn"

    @pytest.mark.asyncio
    async def test_extracts_code_from_top_level_shape(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """Fallback IS response: top-level 'code' key (api-contracts §5.2, C8 line 107)."""
        httpx_mock.add_response(
            method="POST",
            url=AUTHN_URL,
            json={"code": "top-level-auth-code"},
        )
        client = _make_client(httpx_mock)
        code = await client.post_authn(
            flow_id="flow-002",
            authenticator_id="BasicAuthenticator",
            params={"username": "agent-uuid", "password": "secret"},
        )
        await client.aclose()
        assert code == "top-level-auth-code"

    @pytest.mark.asyncio
    async def test_non_200_raises_ciba_initiation_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=AUTHN_URL,
            status_code=401,
            json={"error": "invalid_credentials"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBAInitiationError):
            await client.post_authn(
                flow_id="flow-bad",
                authenticator_id="BasicAuthenticator",
                params={"username": "wrong", "password": "wrong"},
            )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_missing_code_raises_ciba_initiation_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """200 response that has neither authData.code nor code key must raise."""
        httpx_mock.add_response(
            method="POST",
            url=AUTHN_URL,
            json={"status": "INCOMPLETE"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(CIBAInitiationError):
            await client.post_authn(
                flow_id="flow-incomplete",
                authenticator_id="BasicAuthenticator",
                params={"username": "u", "password": "p"},
            )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_sends_json_body(self, httpx_mock: pytest_httpx.HTTPXMock) -> None:
        """IS requires JSON body for /oauth2/authn (not form-encoded) — verified in C4."""
        httpx_mock.add_response(
            method="POST",
            url=AUTHN_URL,
            json={"authData": {"code": "code-abc"}},
        )
        client = _make_client(httpx_mock)
        await client.post_authn(
            flow_id="flow-json",
            authenticator_id="BasicAuthenticator",
            params={"username": "u", "password": "p"},
        )
        await client.aclose()

        [request] = httpx_mock.get_requests()
        # Must be JSON (Content-Type: application/json)
        assert "application/json" in request.headers.get("Content-Type", "")


# ── 6. exchange_code — without actor_token ────────────────────────────────────


class TestExchangeCodeWithoutActorToken:
    """exchange_code without actor_token sends the correct body and returns OAuthToken."""

    @pytest.mark.asyncio
    async def test_correct_form_body_no_actor_token(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json=_token_response_body(),
        )
        client = _make_client(httpx_mock)
        token = await client.exchange_code(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code="auth-code-xyz",
            code_verifier="verifier-abc",
            redirect_uri=REDIRECT_URI,
        )
        await client.aclose()

        assert isinstance(token, OAuthToken)
        assert token.access_token == "eyJhbGciOiJSUzI1NiJ9.payload.sig"
        assert token.expires_in == 3600
        assert token.scope == "openid hr.read"

        [request] = httpx_mock.get_requests()
        body = request.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code=auth-code-xyz" in body
        assert "code_verifier=verifier-abc" in body
        # actor_token must NOT be present when not supplied
        assert "actor_token" not in body

    @pytest.mark.asyncio
    async def test_expires_at_is_computed(self, httpx_mock: pytest_httpx.HTTPXMock) -> None:
        """expires_at must be set to a future UTC datetime (now + expires_in)."""
        httpx_mock.add_response(method="POST", url=TOKEN_URL, json=_token_response_body())
        before = datetime.now(tz=timezone.utc)
        client = _make_client(httpx_mock)
        token = await client.exchange_code(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code="code",
            code_verifier="verifier",
            redirect_uri=REDIRECT_URI,
        )
        after = datetime.now(tz=timezone.utc)
        await client.aclose()

        assert token.expires_at > before
        assert token.expires_at.tzinfo is not None  # must be timezone-aware


# ── 7. exchange_code — WITH actor_token ───────────────────────────────────────


class TestExchangeCodeWithActorToken:
    """actor_token is sent in the POST body, NOT in the Authorization header (F-01 / P10.B)."""

    @pytest.mark.asyncio
    async def test_actor_token_in_body_not_header(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        """Critical Pattern C empirical requirement: actor_token in BODY only."""
        httpx_mock.add_response(method="POST", url=TOKEN_URL, json=_token_response_body())
        client = _make_client(httpx_mock)
        await client.exchange_code(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code="auth-code",
            code_verifier="verifier",
            redirect_uri=REDIRECT_URI,
            actor_token="agent-i4-token",
        )
        await client.aclose()

        [request] = httpx_mock.get_requests()
        body = request.content.decode()
        # actor_token must be in the form body
        assert "actor_token=agent-i4-token" in body
        assert "actor_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Aaccess_token" in body
        # The Authorization header must be Basic (client_id:secret), NOT the actor_token
        auth_header = request.headers.get("Authorization", "")
        assert auth_header.startswith("Basic ")
        # Sanity check: actor_token value must NOT appear in the Authorization header
        assert "agent-i4-token" not in auth_header

    @pytest.mark.asyncio
    async def test_returns_oauth_token(self, httpx_mock: pytest_httpx.HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=TOKEN_URL, json=_token_response_body())
        client = _make_client(httpx_mock)
        token = await client.exchange_code(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code="c",
            code_verifier="v",
            redirect_uri=REDIRECT_URI,
            actor_token="actor-tok",
        )
        await client.aclose()
        assert isinstance(token, OAuthToken)


# ── 8. client_credentials ─────────────────────────────────────────────────────


class TestClientCredentials:
    """client_credentials sends grant_type=client_credentials and returns OAuthToken."""

    @pytest.mark.asyncio
    async def test_returns_oauth_token(self, httpx_mock: pytest_httpx.HTTPXMock) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={
                "access_token": "cc-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid",
            },
        )
        client = _make_client(httpx_mock)
        token = await client.client_credentials(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scope="openid",
        )
        await client.aclose()

        assert isinstance(token, OAuthToken)
        assert token.access_token == "cc-token"

    @pytest.mark.asyncio
    async def test_form_body_contains_grant_type(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"access_token": "t", "token_type": "Bearer", "expires_in": 60, "scope": ""},
        )
        client = _make_client(httpx_mock)
        await client.client_credentials(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
        await client.aclose()

        [request] = httpx_mock.get_requests()
        body = request.content.decode()
        assert "grant_type=client_credentials" in body

    @pytest.mark.asyncio
    async def test_scope_included_when_provided(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"access_token": "t", "token_type": "Bearer", "expires_in": 60, "scope": "openid"},
        )
        client = _make_client(httpx_mock)
        await client.client_credentials(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, scope="openid"
        )
        await client.aclose()

        [request] = httpx_mock.get_requests()
        body = request.content.decode()
        assert "scope=openid" in body

    @pytest.mark.asyncio
    async def test_non_2xx_raises_auth_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            status_code=401,
            json={"error": "invalid_client"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(AuthError):
            await client.client_credentials(
                client_id=CLIENT_ID, client_secret=CLIENT_SECRET
            )
        await client.aclose()


# ── 9. insecure_tls ───────────────────────────────────────────────────────────


class TestInsecureTLS:
    """When insecure_tls=True the owned httpx.AsyncClient is created with verify=False."""

    def test_owned_client_uses_verify_false(self) -> None:
        """WSO2ISClient creates its internal client with verify=False when insecure_tls=True."""
        cfg = WSO2ISClientConfig(base_url=BASE_URL, insecure_tls=True)
        with patch("common.auth.wso2_is_client.httpx.AsyncClient") as mock_cls:
            # Return a plain MagicMock (no spec) to avoid AsyncClient introspection issues
            mock_cls.return_value = MagicMock()
            WSO2ISClient(cfg)
            mock_cls.assert_called_once()
            call_kwargs = mock_cls.call_args.kwargs
            assert call_kwargs.get("verify") is False

    def test_secure_tls_uses_verify_true(self) -> None:
        cfg = WSO2ISClientConfig(base_url=BASE_URL, insecure_tls=False)
        with patch("common.auth.wso2_is_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = MagicMock()
            WSO2ISClient(cfg)
            call_kwargs = mock_cls.call_args.kwargs
            assert call_kwargs.get("verify") is True


# ── 10. aclose ownership ─────────────────────────────────────────────────────


class TestAclose:
    """aclose() closes the owned httpx client; does NOT close an injected one."""

    @pytest.mark.asyncio
    async def test_owned_client_is_closed(self) -> None:
        """When no http is injected, aclose() must close the internal client."""
        cfg = WSO2ISClientConfig(base_url=BASE_URL)
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        with patch("common.auth.wso2_is_client.httpx.AsyncClient", return_value=mock_http):
            client = WSO2ISClient(cfg)
        await client.aclose()
        mock_http.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_injected_client_is_not_closed(self) -> None:
        """When http is injected, aclose() must NOT close it — the caller owns it."""
        cfg = WSO2ISClientConfig(base_url=BASE_URL)
        injected = AsyncMock(spec=httpx.AsyncClient)
        client = WSO2ISClient(cfg, http=injected)
        await client.aclose()
        injected.aclose.assert_not_awaited()


# ── 12. exchange_code error handling ─────────────────────────────────────────


class TestExchangeCodeErrors:
    """exchange_code raises AuthError on non-2xx IS responses."""

    @pytest.mark.asyncio
    async def test_invalid_grant_raises_auth_error(
        self, httpx_mock: pytest_httpx.HTTPXMock
    ) -> None:
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            status_code=400,
            json={"error": "invalid_grant", "error_description": "Code expired"},
        )
        client = _make_client(httpx_mock)
        with pytest.raises(AuthError):
            await client.exchange_code(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                code="expired-code",
                code_verifier="v",
                redirect_uri=REDIRECT_URI,
            )
        await client.aclose()
