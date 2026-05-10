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


# ---------------------------------------------------------------------------
# Sprint 4 S4.4 (UC-15) — A3 / A6 / A7 tests
# ---------------------------------------------------------------------------


def _admin_session(session_id: str = "sess-admin-001") -> Session:
    """HR Admin session with hr_read_rest + hr_approve_rest scopes."""
    return Session(
        session_id=session_id,
        user_sub="user-sub-admin",
        user_label="Admin",
        token_a=_make_token(scope="openid hr_read_rest hr_approve_rest"),
        pkce_state=None,
        code_verifier=None,
        sse_queue=asyncio.Queue(),
    )


def _build_admin_app(
    *, http_client: MagicMock, session: Session
) -> tuple[TestClient, MagicMock]:
    mock_store = MagicMock(spec=SessionStore)

    async def _get_or_404(sid: str) -> Session:
        if sid == session.session_id:
            return session
        raise KeyError(sid)

    mock_store.get_or_404 = AsyncMock(side_effect=_get_or_404)

    # chat_deps is required for A6/A7 (CIBA-driven endpoints) but the
    # tests here intercept _run_serial_fan_out via monkeypatch in the
    # individual test bodies; we only need a non-None placeholder.
    chat_deps_placeholder = MagicMock()

    deps = ReportsRouterDeps(
        session_store=mock_store,
        http_client=http_client,  # type: ignore[arg-type]
        session_cookie_name="orch_sid",
        hr_server_url="http://hr_server:8000",
        it_server_url="http://it_server:8004",
        a2a_clients={},
        agent_registry=None,
        chat_deps=chat_deps_placeholder,
    )

    app = FastAPI()
    app.include_router(build_reports_router(deps))
    return TestClient(app), mock_store


def test_a3_pending_leaves_proxies_with_query_string() -> None:
    """A3: GET /api/reports/leave-requests?status=pending forwards query verbatim."""
    upstream_body = {
        "data": [
            {
                "request_id": "LR001",
                "employee_username": "employee_user",
                "employee_email": "employee@example.com",
                "leave_type": "Annual Leave",
                "days_requested": 5,
                "start_date": "2026-06-10",
                "status": "Pending",
            }
        ],
        "count": 1,
    }
    session = _admin_session()
    http_client = _make_http_client(status_code=200, body=upstream_body)
    client, _ = _build_admin_app(http_client=http_client, session=session)

    resp = client.get(
        "/api/reports/leave-requests?status=Pending",
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 200
    assert resp.json() == upstream_body
    target_url = http_client.get.call_args.args[0]
    assert target_url == "http://hr_server:8000/api/reports/leave-requests?status=Pending"


def test_a6_approve_requires_x_request_id_header() -> None:
    """A6: POST .../approve without X-Request-ID returns 400 (CSRF guard F-02)."""
    session = _admin_session()
    http_client = _make_http_client()
    client, _ = _build_admin_app(http_client=http_client, session=session)

    resp = client.post(
        "/api/reports/leave-requests/LR001/approve",
        cookies={"orch_sid": session.session_id},
    )
    assert resp.status_code == 400
    assert "X-Request-ID" in resp.text


def test_a7_reject_dispatches_with_reason() -> None:
    """A7: reject endpoint validates X-Request-ID + non-empty reason; ack includes hr_agent."""
    import orchestrator.chat.routes as _chat_routes_mod

    session = _admin_session()
    http_client = _make_http_client()
    client, _ = _build_admin_app(http_client=http_client, session=session)

    captured: dict = {}

    async def _fake_fan_out(_session, tool_calls, rid, _chat_deps):
        captured["tool_id"] = tool_calls[0].tool_id
        captured["args"] = tool_calls[0].args
        captured["rid"] = rid

    # Patch the symbol the routes module imports at module-load time.
    import orchestrator.reports.routes as _routes
    original = _routes._run_serial_fan_out
    _routes._run_serial_fan_out = _fake_fan_out
    try:
        # Empty reason → 400.
        resp_empty = client.post(
            "/api/reports/leave-requests/LR001/reject",
            cookies={"orch_sid": session.session_id},
            headers={"X-Request-ID": "rid-reject-1"},
            json={"reason": "   "},
        )
        assert resp_empty.status_code == 400
        body_empty = resp_empty.json()
        assert body_empty["error_id"] == "ERR-VALIDATION-reason-empty"

        # Valid reason → 200 + dispatched.
        resp_ok = client.post(
            "/api/reports/leave-requests/LR001/reject",
            cookies={"orch_sid": session.session_id},
            headers={"X-Request-ID": "rid-reject-2"},
            json={"reason": "Insufficient notice"},
        )
        assert resp_ok.status_code == 200
        body_ok = resp_ok.json()
        assert body_ok["ok"] is True
        assert body_ok["agent_id"] == "hr_agent"
        assert body_ok["request_id"] == "rid-reject-2"
        # Allow the background task to run.
        import asyncio as _asyncio
        _asyncio.get_event_loop().run_until_complete(_asyncio.sleep(0))
        # Captured the synthetic tool_call.
        assert captured.get("tool_id") == "hr.reject_leave"
        assert captured.get("args", {}).get("leave_id") == "LR001"
        assert captured.get("args", {}).get("reason") == "Insufficient notice"
    finally:
        _routes._run_serial_fan_out = original
