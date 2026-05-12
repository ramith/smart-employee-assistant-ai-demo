"""Tests for hr_server REST endpoint ``GET /api/me/leaves`` — Sprint 4 S4.3.

Coverage (2 tests):
    1. Happy path — Bearer token with ``hr_self_rest`` scope returns the
       caller's own leave records under the locked ``{data, count}`` envelope.
    2. Missing scope — token lacks ``hr_self_rest`` → 403 ``insufficient_scope``;
       no records leaked. (Defence-in-depth pair to the orchestrator-side
       pre-flight check.)

Strategy
--------
The REST router authenticates via ``deps.validator.validate_token``; we
inject a stub validator that returns a controlled JWT-payload dict. The
in-memory store is reset per test and seeded directly so we can assert on
the projected fields.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from unittest.mock import AsyncMock

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


for _pkg in ("hr_server", "hr_server.service", "hr_server.auth", "hr_server.rest_api"):
    _ensure_pkg(_pkg)

_store = _load("hr_server.service.store", "hr_server/service/store.py")
_svc = _load("hr_server.service.hr_service", "hr_server/service/hr_service.py")

# The hr_server REST router only imports `JWTValidator` and `TokenError`
# from `hr_server.auth.jwt_validator` for type/duck-typing purposes (it
# never instantiates them in this test). Provide a lightweight stub so we
# don't pay the cost of importing PyJWT in the test environment.
_jwt_stub = types.ModuleType("hr_server.auth.jwt_validator")


class _StubJWTValidator:  # noqa: D401 — not invoked in tests
    pass


class _StubTokenError(Exception):
    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        self.message = message
        super().__init__(message)


_jwt_stub.JWTValidator = _StubJWTValidator
_jwt_stub.TokenError = _StubTokenError
sys.modules["hr_server.auth.jwt_validator"] = _jwt_stub

_server_mod = _load(
    "hr_server.rest_api.server", "hr_server/rest_api/server.py"
)

RestApiDeps = _server_mod.RestApiDeps
build_rest_router = _server_mod.build_rest_router


# ---------------------------------------------------------------------------
# Stub validator — returns a pre-built payload, never hits IS.
# ---------------------------------------------------------------------------


class _StubValidator:
    """Drop-in replacement for ``JWTValidator`` in tests.

    Args:
        payload: The decoded JWT claim set to return on a successful
            ``validate_token`` call. ``scope`` is a space-separated string
            (matching the IS access-token convention).
    """

    def __init__(self, payload: dict):
        self._payload = payload

    async def validate_token(self, token: str) -> dict:  # noqa: ARG002
        return dict(self._payload)


# ---------------------------------------------------------------------------
# Per-test fixture — reset store
# ---------------------------------------------------------------------------


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
# 1. Happy path — caller's own leaves with hr_self_rest envelope
# ---------------------------------------------------------------------------


def test_my_leaves_happy_path_returns_caller_leaves_with_envelope() -> None:
    """``GET /api/me/leaves`` returns ``{data, count}`` with caller's records only."""
    payload = {
        "sub": "user-sub-employee",
        "scope": "openid hr_self_rest",
        "username": "employee_user",
        "given_name": "Emma",
        "last_name": "Worker",
    }

    # Seed three leaves: two belong to the caller, one to a different user.
    _seed_leave(
        request_id="LR-001",
        user_sub="user-sub-employee",
        user_name="Emma Worker",
        leave_type="Annual Leave",
        start_date="2026-06-10",
        end_date="2026-06-14",
        days=5,
        status="Pending",
    )
    _seed_leave(
        request_id="LR-002",
        user_sub="user-sub-employee",
        user_name="Emma Worker",
        leave_type="Sick Leave",
        start_date="2026-05-02",
        end_date="2026-05-03",
        days=2,
        status="Approved",
        reason="Flu",
    )
    _seed_leave(
        request_id="LR-099",
        user_sub="user-sub-someone-else",
        user_name="Other Person",
        leave_type="Personal Leave",
        start_date="2026-07-01",
        end_date="2026-07-02",
        days=2,
        status="Pending",
    )

    client = _build_app(payload)
    resp = client.get(
        "/api/me/leaves",
        headers={"Authorization": "Bearer fake-tok-A"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"data", "count"}
    assert body["count"] == 2
    request_ids = sorted(item["request_id"] for item in body["data"])
    assert request_ids == ["LR-001", "LR-002"]
    # Non-self records are filtered out.
    assert all(item["request_id"] != "LR-099" for item in body["data"])
    # Projected shape includes the locked fields.
    sample = body["data"][0]
    assert {
        "request_id",
        "type",
        "start_date",
        "end_date",
        "days_requested",
        "status",
        "reason",
    } <= set(sample.keys())


# ---------------------------------------------------------------------------
# 2. Missing scope → 401 (per Stage 5 §6: REST scope-deny path returns 401)
# ---------------------------------------------------------------------------


def test_my_leaves_missing_scope_returns_403() -> None:
    """Token without ``hr_self_rest`` is rejected with 403 ``insufficient_scope``."""
    payload = {
        "sub": "user-sub-employee",
        "scope": "openid",   # hr_self_rest absent
        "username": "employee_user",
        "given_name": "Emma",
        "last_name": "Worker",
    }

    # Seed something so we can assert the response did NOT leak it.
    _seed_leave(
        request_id="LR-001",
        user_sub="user-sub-employee",
        user_name="Emma Worker",
    )

    client = _build_app(payload)
    resp = client.get(
        "/api/me/leaves",
        headers={"Authorization": "Bearer fake-tok-A"},
    )
    # The REST router's `_require_scope` returns a 403 with `insufficient_scope`.
    assert resp.status_code == 403
    body = resp.json()
    assert body.get("error") == "insufficient_scope"
    # Make sure no leave data leaked into the error envelope.
    assert "data" not in body


# ---------------------------------------------------------------------------
# 3. /api/me/cubicle — caller's own cubicle assignment
# ---------------------------------------------------------------------------


def test_my_cubicle_returns_assignment_for_assigned_user() -> None:
    """A user whose cubicle is assigned gets their assignment record back."""
    payload = {
        "sub": "user-sub-employee",
        "scope": "openid hr_self_rest",
        "username": "employee_user",
    }
    # S5.12: no seed assignments — assign C-005 to employee_user directly.
    for row in _store.cubicles:
        if row["cubicle_id"] == "C-005":
            row["occupied"] = True
            row["assigned_to_username"] = "employee_user"
            row["assigned_to_email"] = "employee_user@example.com"
            row["assigned_to_sub"] = "user-sub-employee"
            row["assigned_at"] = "2026-05-11T10:00:00+00:00"
            break

    client = _build_app(payload)
    resp = client.get("/api/me/cubicle", headers={"Authorization": "Bearer fake-tok-A"})
    assert resp.status_code == 200
    body = resp.json()
    # employee_user is assigned C-005 (floor 1).
    assert body.get("cubicle_id") == "C-005"
    assert body.get("floor") == 1
    assert "sub" not in body and "assigned_to_sub" not in body


def test_my_cubicle_matches_when_token_a_has_only_email_sub() -> None:
    """Realistic case: token-A's access token carries `sub` = the user's email
    but NO `username` claim; the cubicle was assigned via chat with
    `assigned_to_username` set and `assigned_to_sub` = None (assign_cubicle
    gets sub=None). `_AuthContext` must derive `username` from the email
    local-part so the username branch of get_my_cubicle still matches."""
    payload = {
        "sub": "employee_user@example.com",
        "scope": "openid hr_self_rest",
        # no `username` / `given_name` / `email` claim — derived from `sub`
    }
    for row in _store.cubicles:
        if row["cubicle_id"] == "C-100":
            row["occupied"] = True
            row["assigned_to_username"] = "employee_user"
            row["assigned_to_email"] = "employee_user@example.com"
            row["assigned_to_sub"] = None  # assign_cubicle stores sub=None
            row["assigned_at"] = "2026-05-11T10:00:00+00:00"
            break
    client = _build_app(payload)
    resp = client.get("/api/me/cubicle", headers={"Authorization": "Bearer fake-tok-A"})
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("cubicle_id") == "C-100"
    assert body.get("floor") == 4
    assert "sub" not in body and "assigned_to_sub" not in body


def test_my_cubicle_unassigned_user_returns_not_assigned() -> None:
    payload = {
        "sub": "user-sub-nobody",
        "scope": "openid hr_self_rest",
        "username": "nobody.unassigned",
    }
    client = _build_app(payload)
    resp = client.get("/api/me/cubicle", headers={"Authorization": "Bearer fake-tok-A"})
    assert resp.status_code == 200
    assert resp.json() == {"assigned": False}


def test_my_cubicle_missing_scope_returns_403() -> None:
    payload = {"sub": "x", "scope": "openid", "username": "employee_user"}
    client = _build_app(payload)
    resp = client.get("/api/me/cubicle", headers={"Authorization": "Bearer fake-tok-A"})
    assert resp.status_code == 403
