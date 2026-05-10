"""Tests for orchestrator/reports/routes.py — Sprint 4 S4.3.

Coverage (1 test):
    1. ``GET /api/me/leaves`` happy path — valid session + token-A scope →
       200 with the upstream HR-server envelope ``{data, count}`` forwarded
       verbatim. Validates the orchestrator-side wiring of the proxy
       primitive into the My Leaves panel endpoint.
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
_routes_mod = _load(
    "orchestrator.reports.routes", "orchestrator/reports/routes.py"
)

OAuthToken = _auth_models.OAuthToken
SessionStore = _session_store_mod.SessionStore
Session = _session_store_mod.Session
ReportsRouterDeps = _routes_mod.ReportsRouterDeps
build_reports_router = _routes_mod.build_reports_router


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


def _make_session(session_id: str = "sess-001") -> Session:
    return Session(
        session_id=session_id,
        user_sub="user-sub-123",
        user_label="Alice",
        token_a=_make_token(),
        pkce_state=None,
        code_verifier=None,
        sse_queue=asyncio.Queue(),
    )


def _make_http_client(*, status_code: int = 200, body: dict | None = None) -> MagicMock:
    mock = MagicMock()
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body if body is not None else {"data": [], "count": 0}
    mock.get = AsyncMock(return_value=response)
    return mock


# ---------------------------------------------------------------------------
# 1. GET /api/me/leaves — happy path returns {data, count} envelope verbatim.
# ---------------------------------------------------------------------------


def test_get_my_leaves_returns_upstream_envelope_verbatim() -> None:
    """Cookie-authed call → upstream envelope forwarded verbatim with status 200."""
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
            },
            {
                "request_id": "LR-002",
                "type": "Sick Leave",
                "start_date": "2026-05-02",
                "end_date": "2026-05-03",
                "days_requested": 2,
                "status": "Approved",
                "reason": "Flu",
            },
        ],
        "count": 2,
    }

    session = _make_session()

    mock_store = MagicMock(spec=SessionStore)

    async def _get_or_404(sid: str) -> Session:
        if sid == session.session_id:
            return session
        raise KeyError(sid)

    mock_store.get_or_404 = AsyncMock(side_effect=_get_or_404)

    http_client = _make_http_client(status_code=200, body=upstream_body)

    deps = ReportsRouterDeps(
        session_store=mock_store,
        http_client=http_client,  # type: ignore[arg-type]
        session_cookie_name="orch_sid",
        hr_server_url="http://hr_server:8000",
        it_server_url="http://it_server:8004",
    )

    app = FastAPI()
    app.include_router(build_reports_router(deps))
    client = TestClient(app)

    resp = client.get("/api/me/leaves", cookies={"orch_sid": session.session_id})
    assert resp.status_code == 200
    assert resp.json() == upstream_body

    # Upstream URL contract: trailing-slash safe, hr_self_rest pre-flight
    # carried implicitly via the 200 result.
    http_client.get.assert_awaited_once()
    target_url = http_client.get.call_args.args[0]
    assert target_url == "http://hr_server:8000/api/me/leaves"
