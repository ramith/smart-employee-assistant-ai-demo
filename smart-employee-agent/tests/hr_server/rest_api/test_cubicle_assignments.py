"""Tests for hr_server REST endpoint ``GET /api/reports/cubicle-assignments`` — Sprint 4 S4.5 (UC-16 B3).

Coverage (2 tests):
    1. Happy path — Bearer token with ``hr_read_rest`` returns the locked
       ``{data, count}`` envelope; rows include ``username`` + ``email`` +
       ``cubicle_id`` + ``floor`` + ``assigned_at``; ``sub`` is never returned.
    2. Missing scope — token without ``hr_read_rest`` → 403; no records leaked.
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
# Module isolation (mirrors test_pending_leaves.py S4.4 pattern)
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


def _assign(
    *,
    cubicle_id: str,
    floor: int,
    username: str,
    email: str,
    sub: str = "sub-internal-only",
    assigned_at: str = "2026-05-10T12:00:00+00:00",
) -> None:
    """Flip a seeded cubicle from vacant to occupied by mutating the store directly."""
    for row in _store.cubicles:
        if row["cubicle_id"] == cubicle_id:
            row["occupied"] = True
            row["assigned_to_username"] = username
            row["assigned_to_email"] = email
            row["assigned_to_sub"] = sub
            row["assigned_at"] = assigned_at
            return
    # If not found (cubicle_id outside seed range), append a fresh row.
    _store.cubicles.append({
        "cubicle_id": cubicle_id,
        "floor": floor,
        "occupied": True,
        "assigned_to_username": username,
        "assigned_to_email": email,
        "assigned_to_sub": sub,
        "assigned_at": assigned_at,
    })


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_cubicle_assignments_happy_path_returns_envelope_without_sub() -> None:
    """B3 returns {data, count}; rows include username/email/cubicle_id/floor/assigned_at; sub never leaks."""
    payload = {
        "sub": "user-sub-admin",
        "scope": "openid hr_read_rest",
        "username": "hr_admin_user",
    }

    _assign(
        cubicle_id="C-001",
        floor=1,
        username="employee_user",
        email="employee.user@example.com",
    )
    _assign(
        cubicle_id="C-002",
        floor=1,
        username="jane.doe",
        email="jane.doe@example.com",
    )

    client = _build_app(payload)
    resp = client.get(
        "/api/reports/cubicle-assignments",
        headers={"Authorization": "Bearer fake-tok-A"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"data", "count"}
    # 3 seed assignments (C-005, C-030, C-052) + the 2 this test appended.
    assert body["count"] == 5
    # Identity surface lock: username + email only; sub never returned.
    for row in body["data"]:
        assert {"username", "email", "cubicle_id", "floor", "assigned_at"} <= set(row.keys())
        assert "sub" not in row
        assert "assigned_to_sub" not in row
        assert "employee_id" not in row
    cubicles_returned = sorted(r["cubicle_id"] for r in body["data"])
    assert cubicles_returned == ["C-001", "C-002", "C-005", "C-030", "C-052"]


# ---------------------------------------------------------------------------
# 2. Missing scope → 403
# ---------------------------------------------------------------------------


def test_cubicle_assignments_missing_scope_returns_403() -> None:
    """Token without hr_read_rest is rejected; no records leak."""
    payload = {
        "sub": "user-sub-employee",
        "scope": "openid hr_self_rest",  # hr_read_rest absent
        "username": "employee_user",
    }
    _assign(
        cubicle_id="C-001",
        floor=1,
        username="employee_user",
        email="employee.user@example.com",
    )

    client = _build_app(payload)
    resp = client.get(
        "/api/reports/cubicle-assignments",
        headers={"Authorization": "Bearer fake-tok-A"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert "data" not in body
