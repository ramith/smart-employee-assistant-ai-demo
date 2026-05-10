"""Tests for hr_server REST endpoint ``GET /api/reports/leave-requests`` — Sprint 4 S4.4 (UC-15 B2).

Coverage (2 tests):
    1. Happy path — Bearer token with ``hr_read_rest`` returns the locked
       ``{data, count}`` envelope; rows include ``request_id`` (Stage 6.5
       D5 — get_all_leave_requests preserves it; get_leaves_for_dashboard
       does NOT). Identity surfaced as ``employee_username`` + ``employee_email``;
       sub never leaks.
    2. Missing scope — token without ``hr_read_rest`` → 403; no records
       leaked.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Module isolation (mirrors test_my_leaves.py S4.3 pattern)
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


for _pkg in ("hr_server", "hr_server.service", "hr_server.auth", "hr_server.rest_api"):
    _ensure_pkg(_pkg)

_store = _load("hr_server.service.store", "hr_server/service/store.py")
_load("hr_server.service.hr_service", "hr_server/service/hr_service.py")

_jwt_stub = types.ModuleType("hr_server.auth.jwt_validator")


class _StubJWTValidator:  # noqa: D401
    pass


class _StubTokenError(Exception):
    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        self.message = message
        super().__init__(message)


_jwt_stub.JWTValidator = _StubJWTValidator
_jwt_stub.TokenError = _StubTokenError
sys.modules["hr_server.auth.jwt_validator"] = _jwt_stub

_server_mod = _load("hr_server.rest_api.server", "hr_server/rest_api/server.py")

RestApiDeps = _server_mod.RestApiDeps
build_rest_router = _server_mod.build_rest_router


class _StubValidator:
    def __init__(self, payload: dict):
        self._payload = payload

    async def validate_token(self, token: str) -> dict:  # noqa: ARG002
        return dict(self._payload)


@pytest.fixture(autouse=True)
def _reset_store():
    _store.reset_data()
    yield
    _store.reset_data()


def _build_app(payload: dict) -> TestClient:
    deps = RestApiDeps(validator=_StubValidator(payload))
    app = FastAPI()
    app.include_router(build_rest_router(deps))
    return TestClient(app)


def _seed_leave(
    *,
    request_id: str,
    user_sub: str,
    user_name: str,
    leave_type: str = "Annual Leave",
    start_date: str = "2026-06-10",
    end_date: str = "2026-06-14",
    days: int = 5,
    status: str = "Pending",
    reason: str = "Vacation",
) -> None:
    _store.leave_requests[request_id] = {
        "user_sub": user_sub,
        "user_name": user_name,
        "leave_type": leave_type,
        "start_date": start_date,
        "end_date": end_date,
        "days_requested": days,
        "status": status,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_pending_leaves_happy_path_returns_envelope_with_request_ids() -> None:
    """B2 returns {data, count}; rows include request_id + username + email; sub never leaks."""
    payload = {
        "sub": "user-sub-admin",
        "scope": "openid hr_read_rest",
        "username": "hr_admin_user",
    }

    # Inject a known user record so the projection can surface email.
    _store.users["user-sub-employee"] = {
        "username": "employee_user",
        "email": "employee@example.com",
        "name": "Emma Worker",
    }

    _seed_leave(
        request_id="LR001",
        user_sub="user-sub-employee",
        user_name="Emma Worker",
        status="Pending",
    )
    _seed_leave(
        request_id="LR002",
        user_sub="user-sub-employee",
        user_name="Emma Worker",
        leave_type="Sick Leave",
        start_date="2026-05-02",
        end_date="2026-05-03",
        days=2,
        status="Pending",
    )
    _seed_leave(
        request_id="LR099",
        user_sub="user-sub-other",
        user_name="Other Person",
        status="Approved",  # filtered by status=Pending
    )

    client = _build_app(payload)
    resp = client.get(
        "/api/reports/leave-requests?status=Pending",
        headers={"Authorization": "Bearer fake-tok-A"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"data", "count"}
    assert body["count"] == 2
    rids = sorted(item["request_id"] for item in body["data"])
    assert rids == ["LR001", "LR002"]
    sample = body["data"][0]
    # Locked field shape per Stage 5 §B2 + Stage 6.5 D5.
    assert {
        "request_id",
        "employee_username",
        "employee_email",
        "leave_type",
        "days_requested",
        "start_date",
        "status",
    } <= set(sample.keys())
    # Identity is surfaced as username + email; sub never leaks.
    for row in body["data"]:
        assert "sub" not in row
        assert "user_sub" not in row
        assert row["employee_username"] == "employee_user"
        assert row["employee_email"] == "employee@example.com"


# ---------------------------------------------------------------------------
# 2. Missing scope → 403
# ---------------------------------------------------------------------------


def test_pending_leaves_missing_scope_returns_403() -> None:
    """Token without hr_read_rest is rejected; no records leak."""
    payload = {
        "sub": "user-sub-admin",
        "scope": "openid",  # hr_read_rest absent
        "username": "hr_admin_user",
    }
    _seed_leave(
        request_id="LR001",
        user_sub="user-sub-employee",
        user_name="Emma Worker",
        status="Pending",
    )

    client = _build_app(payload)
    resp = client.get(
        "/api/reports/leave-requests?status=Pending",
        headers={"Authorization": "Bearer fake-tok-A"},
    )
    assert resp.status_code == 403
    body = resp.json()
    # No data field on rejection.
    assert "data" not in body
