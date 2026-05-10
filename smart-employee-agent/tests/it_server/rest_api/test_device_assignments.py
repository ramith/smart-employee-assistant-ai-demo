"""Tests for it_server REST endpoint ``GET /api/reports/device-assignments`` — Sprint 4 S4.5 (UC-16 C1).

Coverage (2 tests):
    1. Happy path — Bearer token with ``it_assets_read_rest`` returns the
       locked ``{data, count}`` envelope; rows include ``username`` +
       ``email`` + ``asset_id`` + ``type`` + ``model`` + ``status``;
       ``sub`` never leaks.
    2. Missing scope — token without ``it_assets_read_rest`` → 403; no records
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
# Module isolation (mirrors tests/hr_server/rest_api/test_pending_leaves.py)
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


for _pkg in ("it_server", "it_server.service", "it_server.auth", "it_server.rest_api"):
    _ensure_pkg(_pkg)

_store = _load("it_server.service.store", "it_server/service/store.py")
_load("it_server.service.it_service", "it_server/service/it_service.py")

_jwt_stub = types.ModuleType("it_server.auth.jwt_validator")


class _StubJWTValidator:  # noqa: D401
    pass


class _StubTokenError(Exception):
    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        self.message = message
        super().__init__(message)


_jwt_stub.JWTValidator = _StubJWTValidator
_jwt_stub.TokenError = _StubTokenError
sys.modules["it_server.auth.jwt_validator"] = _jwt_stub

_server_mod = _load("it_server.rest_api.server", "it_server/rest_api/server.py")

ITRestRouterDeps = _server_mod.ITRestRouterDeps
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
    deps = ITRestRouterDeps(validator=_StubValidator(payload))
    app = FastAPI()
    app.include_router(build_rest_router(deps))
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_device_assignments_happy_path_returns_envelope_without_sub() -> None:
    """C1 returns {data, count}; rows include username/email/asset_id/type/model/status; sub never leaks."""
    payload = {
        "sub": "user-sub-admin",
        "scope": "openid it_assets_read_rest",
        "username": "hr_admin_user",
    }

    client = _build_app(payload)
    resp = client.get(
        "/api/reports/device-assignments",
        headers={"Authorization": "Bearer fake-tok-A"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"data", "count"}
    # Seed asserts >= 5 rows per it_server/service/store._SEED_ASSETS.
    assert body["count"] >= 5
    expected_fields = {"username", "email", "asset_id", "type", "model", "status"}
    for row in body["data"]:
        assert expected_fields <= set(row.keys())
        # Identity surface lock: sub / employee_id never returned.
        assert "sub" not in row
        assert "employee_id" not in row
    # At least one seeded user has both username and email populated
    # (named-user seed is loaded by reset_data via _SEED_USERS).
    populated = [
        r for r in body["data"]
        if r["username"] == "employee_user"
    ]
    assert populated, "expected employee_user row from seed"
    assert populated[0]["email"] == "employee.user@example.com"


# ---------------------------------------------------------------------------
# 2. Missing scope → 403
# ---------------------------------------------------------------------------


def test_device_assignments_missing_scope_returns_403() -> None:
    """Token without it_assets_read_rest is rejected; no records leak."""
    payload = {
        "sub": "user-sub-employee",
        "scope": "openid",  # it_assets_read_rest absent
        "username": "employee_user",
    }

    client = _build_app(payload)
    resp = client.get(
        "/api/reports/device-assignments",
        headers={"Authorization": "Bearer fake-tok-A"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert "data" not in body
