"""Sprint 3 3A.2: tests for common.revocation.build_internal_events_router."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


_REPO = Path(__file__).resolve().parents[3]


def _load_module(dotted: str, rel: str) -> types.ModuleType:
    full = _REPO / rel
    spec = importlib.util.spec_from_file_location(dotted, full)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module


_jti_mod = _load_module("common.revocation.jti_denylist", "common/revocation/jti_denylist.py")
_events_mod = _load_module("common.revocation.internal_events", "common/revocation/internal_events.py")
RevocationState = _jti_mod.RevocationState
build_internal_events_router = _events_mod.build_internal_events_router


def _make_client(secret: str = "shared-test-secret", on_revoke=None):
    state = RevocationState()
    app = FastAPI()
    app.include_router(
        build_internal_events_router(
            state=state,
            shared_secret=secret,
            on_revoke=on_revoke,
            service_label="test",
        )
    )
    return TestClient(app), state


def _body(jti: str = "jti-1", sub: str = "user-1", exp: float = 10**12, reason: str = "user_signed_out"):
    return {
        "type": "session-revoked",
        "subject": {"sub": sub, "jti": jti},
        "exp": exp,
        "reason": reason,
    }


def test_happy_path_acks_and_adds_to_denylist() -> None:
    client, state = _make_client()
    resp = client.post(
        "/internal/events",
        json=_body("jti-happy"),
        headers={"X-Internal-Auth": "shared-test-secret", "X-Request-ID": "rid-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["acked"] is True
    assert "jti-happy" in state.revoked_jtis


def test_missing_secret_returns_401() -> None:
    client, state = _make_client()
    resp = client.post("/internal/events", json=_body(), headers={})
    assert resp.status_code == 401
    assert "jti-1" not in state.revoked_jtis


def test_wrong_secret_returns_401() -> None:
    client, state = _make_client()
    resp = client.post(
        "/internal/events",
        json=_body(),
        headers={"X-Internal-Auth": "WRONG"},
    )
    assert resp.status_code == 401
    assert "jti-1" not in state.revoked_jtis


def test_idempotent_repeat_ack_includes_note() -> None:
    client, state = _make_client()
    headers = {"X-Internal-Auth": "shared-test-secret"}
    r1 = client.post("/internal/events", json=_body("dup"), headers=headers)
    r2 = client.post("/internal/events", json=_body("dup"), headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("note") == "jti already in denylist"


def test_unknown_event_type_400() -> None:
    client, _ = _make_client()
    body = _body()
    body["type"] = "something-else"
    resp = client.post(
        "/internal/events",
        json=body,
        headers={"X-Internal-Auth": "shared-test-secret"},
    )
    assert resp.status_code == 400


def test_on_revoke_callback_invoked() -> None:
    captured: list[tuple[str, str, float, str]] = []

    async def cb(jti, user_sub, exp, reason):
        captured.append((jti, user_sub, exp, reason))

    client, _ = _make_client(on_revoke=cb)
    resp = client.post(
        "/internal/events",
        json=_body("jti-cb", sub="user-cb", exp=999.0, reason="admin_terminated"),
        headers={"X-Internal-Auth": "shared-test-secret"},
    )
    assert resp.status_code == 200
    assert captured == [("jti-cb", "user-cb", 999.0, "admin_terminated")]


def test_on_revoke_failure_does_not_500() -> None:
    async def cb(jti, user_sub, exp, reason):
        raise RuntimeError("bang")

    client, state = _make_client(on_revoke=cb)
    resp = client.post(
        "/internal/events",
        json=_body("jti-bang"),
        headers={"X-Internal-Auth": "shared-test-secret"},
    )
    # Denylist still updated; callback failure swallowed.
    assert resp.status_code == 200
    assert "jti-bang" in state.revoked_jtis


def test_empty_secret_factory_rejects() -> None:
    import pytest

    state = RevocationState()
    with pytest.raises(ValueError):
        build_internal_events_router(state=state, shared_secret="")
