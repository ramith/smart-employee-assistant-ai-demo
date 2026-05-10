"""Tests for orchestrator/auth/routes.py — Wave 7, Sprint 1.

Coverage targets (12 tests)
----------------------------
 1.  ``GET /auth/login`` redirects to IS /oauth2/authorize with correct params.
 2.  ``GET /auth/login?next=/profile`` stores ``redirect_after_login="/profile"``
     in ``pending_logins``.
 3.  Consecutive ``GET /auth/login`` calls produce distinct ``state`` values.
 4.  ``GET /auth/callback?code=X&state=Y`` with valid state → 200 HTML relay page
     that contains the code, state, and exchange fetch call.
 5.  ``GET /auth/callback?state=unknown`` → 400 invalid_state.
 6.  ``GET /auth/callback?error=access_denied&state=Y`` → 200 HTML redirect to
     SPA login error page (no exception).
 7.  ``POST /auth/exchange`` happy path: creates session, sets ``orch_sid`` cookie,
     returns ``{"ok": true, "user_label": ...}``.
 8.  ``POST /auth/exchange`` cookie has HttpOnly and SameSite=Lax flags.
 9.  ``POST /auth/exchange`` with unknown state → 400.
10.  ``POST /auth/exchange`` pops the pending_login so a second call with the
     same state → 400 (replay protection).
11.  ``POST /auth/logout`` with a valid cookie deletes the session and clears the
     cookie (Set-Cookie with max_age=0 or empty value).
12.  ``POST /auth/logout`` without a cookie → 200 (idempotent).

Style: Python 3.11+, FastAPI TestClient (sync), unittest.mock.
Isolation: all IS HTTP calls replaced by AsyncMock; SessionStore is real.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Module isolation bootstrap
# ---------------------------------------------------------------------------
# Load each source module directly from its .py file, bypassing __init__.py
# stubs that may not yet be complete.  Same pattern as test_session_store.py.

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str) -> None:
    """Register a bare package stub in sys.modules if not already present."""
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    stub.__path__ = [str(_ROOT / dotted_name.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules under *dotted_name*."""
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


# Ensure intermediate package namespaces.
for _pkg in (
    "common",
    "common.auth",
    "orchestrator",
    "orchestrator.auth",
    "orchestrator.events",
):
    _ensure_pkg(_pkg)

# Load dependency chain bottom-up.
_models_mod = _load_module("common.auth.models", "common/auth/models.py")
_store_mod = _load_module(
    "orchestrator.auth.session_store", "orchestrator/auth/session_store.py"
)

# We stub out heavy transitive dependencies that pattern_c.py imports so they
# don't need live environment variables or network access.
for _stub_name in (
    "common.auth.actor_token_provider",
    "common.auth.jwt_validator",
    "common.auth.wso2_is_client",
):
    if _stub_name not in sys.modules:
        _m = types.ModuleType(_stub_name)
        _m.__package__ = _stub_name.rsplit(".", 1)[0]
        sys.modules[_stub_name] = _m

# config.py imports AgentCredentials and WSO2ISClientConfig from the stubs above;
# provide minimal stand-ins.
_actor_stub = sys.modules["common.auth.actor_token_provider"]
if not hasattr(_actor_stub, "AgentCredentials"):
    from dataclasses import dataclass as _dc

    @_dc
    class _AgentCredentials:
        agent_id: str = "orch-agent-id"
        agent_secret: str = "secret"
        oauth_client_id: str = "orch-oauth-id"
        oauth_client_secret: str = "oauth-secret"
        redirect_uri: str = "http://localhost:8090/agent-callback"

    _actor_stub.AgentCredentials = _AgentCredentials  # type: ignore[attr-defined]

# ActorTokenProvider is imported by pattern_c.py — provide a minimal class stub.
if not hasattr(_actor_stub, "ActorTokenProvider"):
    class _ActorTokenProvider:
        pass

    _actor_stub.ActorTokenProvider = _ActorTokenProvider  # type: ignore[attr-defined]

_is_client_stub = sys.modules["common.auth.wso2_is_client"]
if not hasattr(_is_client_stub, "WSO2ISClientConfig"):
    from dataclasses import dataclass as _dc2

    @_dc2
    class _WSO2ISClientConfig:
        base_url: str = "https://is.example.com"
        insecure_tls: bool = False

    _is_client_stub.WSO2ISClientConfig = _WSO2ISClientConfig  # type: ignore[attr-defined]

if not hasattr(_is_client_stub, "WSO2ISClient"):
    class _WSO2ISClient:
        pass

    _is_client_stub.WSO2ISClient = _WSO2ISClient  # type: ignore[attr-defined]

_jwt_stub = sys.modules["common.auth.jwt_validator"]
for _jwt_name in ("JWKSCache", "ValidatorConfig", "validate"):
    if not hasattr(_jwt_stub, _jwt_name):
        setattr(_jwt_stub, _jwt_name, MagicMock())

_config_mod = _load_module("orchestrator.config", "orchestrator/config.py")
_pattern_c_mod = _load_module("orchestrator.auth.pattern_c", "orchestrator/auth/pattern_c.py")
_is_revoke_mod = _load_module("orchestrator.auth.is_revoke", "orchestrator/auth/is_revoke.py")
_logout_handler_mod = _load_module("orchestrator.auth.logout_handler", "orchestrator/auth/logout_handler.py")
_routes_mod = _load_module("orchestrator.auth.routes", "orchestrator/auth/routes.py")

# Bind public names.
OAuthToken = _models_mod.OAuthToken
OBOToken = _models_mod.OBOToken
JWTClaims = _models_mod.JWTClaims
Session = _store_mod.Session
SessionStore = _store_mod.SessionStore
OrchestratorConfig = _config_mod.OrchestratorConfig
AgentCredentials = _actor_stub.AgentCredentials
PatternCExchanger = _pattern_c_mod.PatternCExchanger
PatternCResult = _pattern_c_mod.PatternCResult

AuthRouterDeps = _routes_mod.AuthRouterDeps
PendingLogin = _routes_mod.PendingLogin
build_auth_router = _routes_mod.build_auth_router
ExchangeRequest = _routes_mod.ExchangeRequest


# ---------------------------------------------------------------------------
# Shared factory helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_oauth_token(
    access_token: str = "token-a-aaa",
    expires_in: int = 3600,
) -> OAuthToken:
    """Construct a minimal OAuthToken for test use."""
    now = _utc_now()
    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=expires_in,
        expires_at=now + timedelta(seconds=expires_in),
        refresh_token=None,
        scope="openid orchestrate",
        id_token=None,
    )


def _make_jwt_claims(sub: str = "user-uuid-001") -> JWTClaims:
    """Construct a minimal JWTClaims for test use."""
    return JWTClaims(
        sub=sub,
        iss="https://is.example.com/oauth2/token",
        aud="orchestrator-app-client-id",
        exp=int((_utc_now() + timedelta(seconds=3600)).timestamp()),
        iat=int(_utc_now().timestamp()),
        jti="jti-test-001",
        act={"sub": "orch-agent-id"},
        scope="openid orchestrate",
        aut="APPLICATION_USER",
    )


def _make_pattern_c_result(sub: str = "user-uuid-001") -> PatternCResult:
    """Return a PatternCResult with a mocked token and claims."""
    return PatternCResult(
        token_a=_make_oauth_token(),
        claims=_make_jwt_claims(sub=sub),
    )


def _make_config() -> OrchestratorConfig:
    """Return a minimal OrchestratorConfig suitable for tests."""
    creds = AgentCredentials(
        agent_id="orch-agent-id",
        agent_secret="agent-secret",
        oauth_client_id="orch-oauth-client-id",
        oauth_client_secret="orch-oauth-client-secret",
        redirect_uri="http://localhost:8090/agent-callback",
    )
    return OrchestratorConfig(
        is_base_url="https://is.example.com",
        is_insecure_tls=False,
        is_issuer="https://is.example.com/oauth2/token",
        is_jwks_url="https://is.example.com/oauth2/jwks",
        mcp_client_id="orchestrator-mcp-client-id",
        mcp_client_secret="mcp-secret",
        mcp_redirect_uri="http://localhost:8090/agent-callback",
        orchestrator_agent=creds,
        hr_agent_url="http://hr_agent:8001",
        it_agent_url="http://it_agent:8002",
        hr_agent_oauth_client_id="hr-oauth-client-id",
        it_agent_oauth_client_id="it-oauth-client-id",
        trusted_specialist_subs=frozenset({"hr_agent-id", "it_agent-id"}),
        allowed_origins=frozenset({"http://localhost:3001"}),
        cookie_secure=False,
    )


def _make_deps(
    pattern_c_result: PatternCResult | None = None,
    exchange_raises: Exception | None = None,
) -> AuthRouterDeps:
    """Build an ``AuthRouterDeps`` with a mocked ``PatternCExchanger``."""
    config = _make_config()
    session_store = SessionStore()

    mock_exchanger = MagicMock(spec=PatternCExchanger)
    if exchange_raises is not None:
        mock_exchanger.exchange = AsyncMock(side_effect=exchange_raises)
    else:
        result = pattern_c_result or _make_pattern_c_result()
        mock_exchanger.exchange = AsyncMock(return_value=result)

    # 3A.1: build a LogoutHandler with a mocked RevokeClient. revoke_access_token
    # is a no-op AsyncMock — covers the happy path; tests that assert revoke
    # behaviour can override.
    mock_revoke = MagicMock(spec=_is_revoke_mod.RevokeClient)
    mock_revoke.revoke_access_token = AsyncMock(return_value=None)
    logout_handler = _logout_handler_mod.LogoutHandler(
        config=config,
        session_store=session_store,
        revoke_client=mock_revoke,
    )

    return AuthRouterDeps(
        config=config,
        pattern_c=mock_exchanger,
        session_store=session_store,
        logout_handler=logout_handler,
        pending_logins={},
    )


def _make_client(deps: AuthRouterDeps | None = None) -> tuple[TestClient, AuthRouterDeps]:
    """Return ``(TestClient, deps)`` for a fresh test app."""
    d = deps or _make_deps()
    app = FastAPI()
    app.include_router(build_auth_router(d))
    return TestClient(app, follow_redirects=False), d


# ---------------------------------------------------------------------------
# Helper — seed a pending_login and return (state, code_verifier)
# ---------------------------------------------------------------------------


def _seed_pending(deps: AuthRouterDeps, redirect_after: str = "/") -> tuple[str, str]:
    """Directly insert a PendingLogin into deps and return (state, code_verifier)."""
    import secrets as _sec

    state = _sec.token_urlsafe(32)
    cv = "test-code-verifier-aaaaaaaaaaaaaaaaaaaaaaa"
    deps.pending_logins[state] = PendingLogin(
        code_verifier=cv,
        redirect_after_login=redirect_after,
        created_at=_utc_now(),
    )
    return state, cv


# ---------------------------------------------------------------------------
# Test 1 — GET /auth/login redirects to IS /authorize with correct params
# ---------------------------------------------------------------------------


def test_login_redirects_to_is_authorize() -> None:
    """GET /auth/login must redirect to IS /oauth2/authorize with required params."""
    client, deps = _make_client()

    resp = client.get("/auth/login")

    assert resp.status_code == 302
    location = resp.headers["location"]
    parsed = urllib.parse.urlparse(location)
    params = dict(urllib.parse.parse_qsl(parsed.query))

    assert parsed.scheme + "://" + parsed.netloc == "https://is.example.com"
    assert parsed.path == "/oauth2/authorize"
    # Pattern C login uses mcp_client_id (the confidential MCP-template app).
    # The legacy spa_client_id kwarg was renamed to client_id and the
    # vestigial orchestrator-app config field/env var was dropped in 3B.3
    # (memory: project_orchestrator_app_vestigial.md).
    assert params["client_id"] == "orchestrator-mcp-client-id"
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == "http://localhost:8090/agent-callback"
    assert "openid" in params["scope"]
    assert params["code_challenge_method"] == "S256"
    assert params["requested_actor"] == "orch-agent-id"
    # state must be present and non-empty
    assert len(params.get("state", "")) > 0
    # code_challenge must be present and non-empty
    assert len(params.get("code_challenge", "")) > 0
    # The state is stored in pending_logins
    assert params["state"] in deps.pending_logins


# ---------------------------------------------------------------------------
# Test 2 — GET /auth/login?next=/profile stores correct redirect_after_login
# ---------------------------------------------------------------------------


def test_login_stores_redirect_after_login() -> None:
    """GET /auth/login?next=/profile must store redirect_after_login='/profile'."""
    client, deps = _make_client()

    resp = client.get("/auth/login?next=/profile")

    assert resp.status_code == 302
    location = resp.headers["location"]
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))
    state = params["state"]

    pending = deps.pending_logins.get(state)
    assert pending is not None
    assert pending.redirect_after_login == "/profile"


# ---------------------------------------------------------------------------
# Test 3 — Consecutive GET /auth/login calls produce distinct state values
# ---------------------------------------------------------------------------


def test_login_produces_distinct_states() -> None:
    """Each GET /auth/login invocation must produce a unique state."""
    client, deps = _make_client()

    resp1 = client.get("/auth/login")
    resp2 = client.get("/auth/login")

    assert resp1.status_code == 302
    assert resp2.status_code == 302

    params1 = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(resp1.headers["location"]).query))
    params2 = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(resp2.headers["location"]).query))

    assert params1["state"] != params2["state"]
    # Both states are independently stored
    assert len(deps.pending_logins) == 2


# ---------------------------------------------------------------------------
# Test 4 — GET /auth/callback with valid state returns 200 HTML relay page
# ---------------------------------------------------------------------------


def test_callback_valid_state_returns_html_relay() -> None:
    """GET /auth/callback with valid state+code must return 200 HTML with exchange fetch."""
    client, deps = _make_client()
    state, _ = _seed_pending(deps)

    resp = client.get(f"/agent-callback?code=test-code-xyz&state={state}")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # The relay page must contain the code and state so the fetch can use them.
    assert "test-code-xyz" in body
    assert state in body
    # The page must attempt the exchange endpoint.
    assert "/auth/exchange" in body
    # The page must be a complete HTML document.
    assert "<!DOCTYPE html>" in body


# ---------------------------------------------------------------------------
# Test 5 — GET /auth/callback with unknown state → 400
# ---------------------------------------------------------------------------


def test_callback_unknown_state_returns_400() -> None:
    """GET /auth/callback with an unrecognised state must return 400."""
    client, _ = _make_client()

    resp = client.get("/agent-callback?code=any-code&state=not-a-real-state")

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Test 6 — GET /auth/callback with IS error returns 200 HTML that redirects
# ---------------------------------------------------------------------------


def test_callback_access_denied_returns_html_redirect() -> None:
    """GET /auth/callback?error=access_denied must return 200 HTML pointing to login error page."""
    client, _ = _make_client()

    resp = client.get("/agent-callback?error=access_denied&state=irrelevant")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # The page must redirect to the SPA login error URL.
    assert "/login?error=access_denied" in body


# ---------------------------------------------------------------------------
# Test 7 — POST /auth/exchange happy path: session created, cookie set
# ---------------------------------------------------------------------------


def test_exchange_happy_path_creates_session_and_cookie() -> None:
    """POST /auth/exchange must create a session, set orch_sid cookie, return user_label."""
    client, deps = _make_client()
    state, _ = _seed_pending(deps)

    resp = client.post("/auth/exchange", json={"code": "auth-code-abc", "state": state})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["user_label"], str)
    assert len(body["user_label"]) > 0

    # Sprint 4: scopes field exposed for SPA navigation gating.
    assert "scopes" in body
    assert isinstance(body["scopes"], list)
    # Default test fixture token-A has scope="openid orchestrate"; assert
    # both elements split correctly (canonical "is HR Admin" probe in
    # production: scopes.includes("hr_approve_rest")).
    assert "openid" in body["scopes"]

    # Session cookie must be set.
    assert "orch_sid" in resp.cookies
    session_id = resp.cookies["orch_sid"]
    assert len(session_id) == 36  # UUID4

    # Session must exist in the store.
    session = deps.session_store.get(session_id)
    assert session is not None
    assert session.session_id == session_id


# ---------------------------------------------------------------------------
# Test 8 — Cookie has HttpOnly and SameSite=Lax flags
# ---------------------------------------------------------------------------


def test_exchange_cookie_has_correct_flags() -> None:
    """The orch_sid cookie must carry HttpOnly and SameSite=Lax flags."""
    client, deps = _make_client()
    state, _ = _seed_pending(deps)

    resp = client.post("/auth/exchange", json={"code": "auth-code-def", "state": state})

    assert resp.status_code == 200
    # Inspect raw Set-Cookie header.
    set_cookie = resp.headers.get("set-cookie", "")
    assert set_cookie != "", "No Set-Cookie header found"
    # Case-insensitive flag checks.
    lower_cookie = set_cookie.lower()
    assert "httponly" in lower_cookie, f"HttpOnly missing from: {set_cookie}"
    # FIX-8 (mid-sprint review): docstring drifted; the real assertion is Strict.
    # 3A.1 FIX-9 tightened the cookie from SameSite=Lax to SameSite=Strict.
    assert "samesite=strict" in lower_cookie, f"SameSite=Strict missing from: {set_cookie}"


# ---------------------------------------------------------------------------
# Test 9 — POST /auth/exchange with unknown state → 400
# ---------------------------------------------------------------------------


def test_exchange_unknown_state_returns_400() -> None:
    """POST /auth/exchange with a state that was never registered must return 400."""
    client, _ = _make_client()

    resp = client.post("/auth/exchange", json={"code": "any", "state": "ghost-state"})

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Test 10 — POST /auth/exchange pops pending_login; second call with same state → 400
# ---------------------------------------------------------------------------


def test_exchange_pops_pending_login_on_success() -> None:
    """Successful POST /auth/exchange must consume (pop) the pending_login.

    A second call with the same state must return 400 (replay prevention).
    """
    client, deps = _make_client()
    state, _ = _seed_pending(deps)

    # First call — succeeds.
    resp1 = client.post("/auth/exchange", json={"code": "code-1", "state": state})
    assert resp1.status_code == 200

    # state is gone from pending_logins.
    assert state not in deps.pending_logins

    # Second call with same state — must fail.
    resp2 = client.post("/auth/exchange", json={"code": "code-1", "state": state})
    assert resp2.status_code == 400


# ---------------------------------------------------------------------------
# Test 11 — POST /auth/logout with cookie deletes session and clears cookie
# ---------------------------------------------------------------------------


def test_logout_with_cookie_deletes_session_and_clears_cookie() -> None:
    """POST /auth/logout (3A.1): runs cascade, deletes session, clears cookie, returns redirect_url."""
    client, deps = _make_client()
    state, _ = _seed_pending(deps)

    # Perform login exchange to establish a session.
    exchange_resp = client.post(
        "/auth/exchange", json={"code": "code-for-logout-test", "state": state}
    )
    assert exchange_resp.status_code == 200
    session_id = exchange_resp.cookies["orch_sid"]
    assert deps.session_store.get(session_id) is not None

    # 3A.1 FIX-9: X-Request-ID is required.
    client.cookies.set("orch_sid", session_id)
    logout_resp = client.post("/auth/logout", headers={"X-Request-ID": "test-rid-1"})

    assert logout_resp.status_code == 200
    body = logout_resp.json()
    assert body["ok"] is True
    # 3A.1 G-9: redirect_url goes to the IS /oidc/logout endpoint with id_token_hint + client_id.
    assert "redirect_url" in body
    assert "/oidc/logout" in body["redirect_url"]
    assert "client_id=" in body["redirect_url"]

    # Session must be gone from the store.
    assert deps.session_store.get(session_id) is None

    set_cookie = logout_resp.headers.get("set-cookie", "")
    assert set_cookie != "", "No Set-Cookie header on logout response"
    lower_cookie = set_cookie.lower()
    assert "max-age=0" in lower_cookie or 'orch_sid=""' in lower_cookie or "orch_sid=;" in lower_cookie, (
        f"Cookie was not cleared: {set_cookie}"
    )


# ---------------------------------------------------------------------------
# Test 12 — POST /auth/logout without cookie → 200 (idempotent)
# ---------------------------------------------------------------------------


def test_logout_without_cookie_returns_200() -> None:
    """POST /auth/logout (3A.1): no cookie → 200, ok=true, redirect_url='/'."""
    client, _ = _make_client()

    resp = client.post("/auth/logout", headers={"X-Request-ID": "test-rid-no-cookie"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["redirect_url"] == "/"


# ---------------------------------------------------------------------------
# Tests 13/14 — 3A.1 FIX-9: CSRF defense via required X-Request-ID
# ---------------------------------------------------------------------------


def test_logout_without_x_request_id_returns_400() -> None:
    """POST /auth/logout without X-Request-ID must 400 (FIX-9 CSRF defense)."""
    client, _ = _make_client()

    resp = client.post("/auth/logout")  # no X-Request-ID header

    assert resp.status_code == 400


def test_logout_x_request_id_required_even_without_cookie() -> None:
    """The X-Request-ID requirement applies even on the idempotent no-session path."""
    client, _ = _make_client()
    # No cookie + no X-Request-ID → 400 (FIX-9 fires before the cookie path).
    resp = client.post("/auth/logout")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Hardening tests — pending_logins cap + age-sweep
# ---------------------------------------------------------------------------


def test_pending_logins_inline_sweep_drops_expired_entries() -> None:
    """A login attempt evicts pending_logins entries older than the TTL."""
    from collections import OrderedDict
    from datetime import datetime, timedelta, timezone
    _enforce_bounds = _routes_mod._enforce_pending_logins_bounds  # noqa: SLF001
    PendingLoginCls = _routes_mod.PendingLogin  # noqa: SLF001

    pending: OrderedDict[str, object] = OrderedDict()
    old = datetime.now(tz=timezone.utc) - timedelta(seconds=3600)
    fresh = datetime.now(tz=timezone.utc)
    pending["stale"] = PendingLoginCls(
        code_verifier="v",
        redirect_after_login="/",
        created_at=old,
    )
    pending["new"] = PendingLoginCls(
        code_verifier="v2",
        redirect_after_login="/",
        created_at=fresh,
    )

    _enforce_bounds(pending)
    assert "stale" not in pending
    assert "new" in pending


def test_pending_logins_hard_cap_evicts_oldest() -> None:
    """At cap, an insert evicts the oldest entry FIFO."""
    from collections import OrderedDict
    from datetime import datetime, timezone
    _enforce_bounds = _routes_mod._enforce_pending_logins_bounds  # noqa: SLF001
    PendingLoginCls = _routes_mod.PendingLogin  # noqa: SLF001

    # Patch the cap down for the test so we don't have to insert 10k entries.
    original_cap = _routes_mod._PENDING_LOGINS_HARD_CAP  # noqa: SLF001
    _routes_mod._PENDING_LOGINS_HARD_CAP = 3  # noqa: SLF001
    try:
        pending: OrderedDict[str, object] = OrderedDict()
        for i in range(3):
            pending[f"s{i}"] = PendingLoginCls(
                code_verifier=f"v{i}",
                redirect_after_login="/",
                created_at=datetime.now(tz=timezone.utc),
            )
        # At cap; another insert path will trigger eviction.
        _enforce_bounds(pending)
        # The bounds-enforcement function evicts when len >= cap to leave
        # room for the next insert. With cap=3 and 3 entries, the oldest
        # is evicted.
        assert "s0" not in pending
        assert "s1" in pending
        assert "s2" in pending
    finally:
        _routes_mod._PENDING_LOGINS_HARD_CAP = original_cap  # noqa: SLF001
