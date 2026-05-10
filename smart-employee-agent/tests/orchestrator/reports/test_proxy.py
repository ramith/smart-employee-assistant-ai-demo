"""Tests for orchestrator/reports/proxy.py — Sprint 4 S4.3.

Coverage (4 tests):
    1. Happy path — valid session + token-A scope → 200 with upstream body verbatim.
    2. Missing cookie → 401 ``ERR-AUTH-001`` (no upstream call made).
    3. Terminating session → 401 ``ERR-AUTH-001`` (no upstream call made).
    4. Pre-flight scope mismatch → 403 ``ERR-AUTH-scope-missing``;
       upstream client mock is asserted *not* called (defence-in-depth gate).
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Module isolation
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted: str) -> None:
    if dotted in sys.modules:
        return
    stub = types.ModuleType(dotted)
    stub.__package__ = dotted
    stub.__path__ = [str(_ROOT / dotted.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted] = stub


def _load(dotted: str, rel: str) -> types.ModuleType:
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, _ROOT / rel)
    assert spec and spec.loader, f"Cannot load {rel}"
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


for _pkg in (
    "common",
    "common.auth",
    "common.logging",
    "orchestrator",
    "orchestrator.auth",
    "orchestrator.reports",
):
    _ensure_pkg(_pkg)

_auth_models = _load("common.auth.models", "common/auth/models.py")
_correlation = _load("common.logging.correlation", "common/logging/correlation.py")
_session_store_mod = _load(
    "orchestrator.auth.session_store", "orchestrator/auth/session_store.py"
)
_proxy_mod = _load("orchestrator.reports.proxy", "orchestrator/reports/proxy.py")

OAuthToken = _auth_models.OAuthToken
SessionStore = _session_store_mod.SessionStore
Session = _session_store_mod.Session
forward_with_token_a = _proxy_mod.forward_with_token_a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_token(scope: str = "openid hr_self_rest") -> OAuthToken:
    return OAuthToken(
        access_token="tok-A-access",
        token_type="Bearer",
        expires_in=3600,
        expires_at=_utc_now() + timedelta(seconds=3600),
        refresh_token=None,
        scope=scope,
        id_token=None,
    )


def _make_session(
    *,
    session_id: str = "sess-001",
    scope: str = "openid hr_self_rest",
    terminating: bool = False,
) -> Session:
    s = Session(
        session_id=session_id,
        user_sub="user-sub-123",
        user_label="Alice",
        token_a=_make_token(scope=scope),
        pkce_state=None,
        code_verifier=None,
        sse_queue=asyncio.Queue(),
    )
    s.terminating = terminating
    return s


def _make_http_client(*, status_code: int = 200, body: dict | None = None) -> MagicMock:
    """Return a mock ``httpx.AsyncClient`` with a controllable ``.get(...)``."""
    mock = MagicMock()
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body if body is not None else {"data": [], "count": 0}
    mock.get = AsyncMock(return_value=response)
    return mock


def _build_app(
    *,
    session: Session | None = None,
    cookie_name: str = "orch_sid",
    target_url: str = "http://hr_server:8000/api/me/leaves",
    required_scope: str = "hr_self_rest",
    http_client: MagicMock | None = None,
) -> tuple[FastAPI, MagicMock]:
    """Build a minimal app exposing GET /probe → forward_with_token_a()."""
    if session is None:
        session = _make_session()

    mock_store = MagicMock(spec=SessionStore)

    async def _get_or_404(sid: str) -> Session:
        if sid == session.session_id:
            return session
        raise KeyError(sid)

    mock_store.get_or_404 = AsyncMock(side_effect=_get_or_404)

    if http_client is None:
        http_client = _make_http_client()

    # Use a Starlette route directly to side-step the FastAPI / Pydantic v2
    # forward-ref evaluation that trips when `from __future__ import
    # annotations` lifts a closure-scoped `Request` annotation into a
    # ForwardRef the validator can't resolve.
    from starlette.routing import Route as _Route
    from starlette.applications import Starlette as _Starlette

    async def probe(request):  # type: ignore[no-untyped-def]
        return await forward_with_token_a(
            request,
            session_store=mock_store,
            session_cookie_name=cookie_name,
            target_url=target_url,
            required_scope=required_scope,
            http_client=http_client,  # type: ignore[arg-type]
        )

    app = _Starlette(routes=[_Route("/probe", probe, methods=["GET"])])
    return app, http_client


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_proxy_happy_path_returns_upstream_body_verbatim() -> None:
    """Valid session + matching scope → upstream body forwarded verbatim."""
    upstream_body = {
        "data": [
            {
                "request_id": "LR-001",
                "type": "Annual Leave",
                "start_date": "2026-06-10",
                "end_date": "2026-06-14",
                "days_requested": 5,
                "status": "Pending",
                "reason": "Vacation",
            }
        ],
        "count": 1,
    }
    http_client = _make_http_client(status_code=200, body=upstream_body)
    app, _ = _build_app(http_client=http_client)
    client = TestClient(app)

    resp = client.get("/probe", cookies={"orch_sid": "sess-001"})
    assert resp.status_code == 200
    assert resp.json() == upstream_body

    # Bearer token-A is forwarded.
    http_client.get.assert_awaited_once()
    call_args = http_client.get.call_args
    assert call_args.args[0] == "http://hr_server:8000/api/me/leaves"
    headers = call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok-A-access"


# ---------------------------------------------------------------------------
# 2. Missing cookie → 401
# ---------------------------------------------------------------------------


def test_proxy_no_cookie_returns_401_and_skips_upstream() -> None:
    """No ``orch_sid`` cookie → 401 with ERR-AUTH-001; backend not contacted."""
    http_client = _make_http_client()
    app, _ = _build_app(http_client=http_client)
    client = TestClient(app)

    resp = client.get("/probe")  # no cookies
    assert resp.status_code == 401
    body = resp.json()
    assert body["error_id"] == "ERR-AUTH-001"

    # Defence-in-depth: no upstream round-trip on a missing-cookie reject.
    http_client.get.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Terminating session → 401
# ---------------------------------------------------------------------------


def test_proxy_terminating_session_returns_401() -> None:
    """A session whose logout cascade has fired must reject with 401."""
    session = _make_session(terminating=True)
    http_client = _make_http_client()
    app, _ = _build_app(session=session, http_client=http_client)
    client = TestClient(app)

    resp = client.get("/probe", cookies={"orch_sid": session.session_id})
    assert resp.status_code == 401
    assert resp.json()["error_id"] == "ERR-AUTH-001"

    # The cookie authenticated, but the cascade fence trips before any
    # upstream call is made.
    http_client.get.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Pre-flight scope mismatch → 403, no upstream call
# ---------------------------------------------------------------------------


def test_proxy_preflight_scope_missing_returns_403_and_skips_upstream() -> None:
    """Token-A lacks the required scope → 403; backend round-trip skipped."""
    # token-A only carries openid — `hr_self_rest` is required by the proxy.
    session = _make_session(scope="openid")
    http_client = _make_http_client()
    app, _ = _build_app(session=session, http_client=http_client)
    client = TestClient(app)

    resp = client.get("/probe", cookies={"orch_sid": session.session_id})
    assert resp.status_code == 403
    body = resp.json()
    assert body["error_id"] == "ERR-AUTH-scope-missing"
    assert "hr_self_rest" in body["message"]

    # Defence-in-depth: no upstream call even though the cookie is valid.
    http_client.get.assert_not_called()
