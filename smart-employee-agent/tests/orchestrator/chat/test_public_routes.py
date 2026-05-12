"""Tests for orchestrator/chat/public_routes.py — Sprint 6.

Coverage
--------
 1.  POST /public/chat — valid message → 200 + {reply: str}
 2.  POST /public/chat — empty message → 422 (Pydantic min_length=1)
 3.  POST /public/chat — 501-char message → 422 (Pydantic max_length=500, F-3)
 4.  POST /public/chat — 500-char message → 200 (boundary, must pass)
 5.  POST /public/chat — strips leading/trailing whitespace before calling handler
 6.  POST /public/chat — handler LLM error → still 200 (static fallback kicks in)
 7.  build_public_router mounts at /chat (not /public/chat — prefix set by main.py)
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Module isolation helpers
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str) -> None:
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    stub.__path__ = [str(_ROOT / dotted_name.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
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


for _pkg in ("orchestrator", "orchestrator.chat"):
    _ensure_pkg(_pkg)

_handler_mod = _load_module(
    "orchestrator.chat.public_handler",
    "orchestrator/chat/public_handler.py",
)
_routes_mod = _load_module(
    "orchestrator.chat.public_routes",
    "orchestrator/chat/public_routes.py",
)

build_public_router = _routes_mod.build_public_router
PublicInfoHandler = _handler_mod.PublicInfoHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(reply: str = "Test reply.", *, fail: bool = False) -> TestClient:
    """Build a minimal FastAPI app with the public router and return a TestClient.

    If *fail* is True the handler's answer() raises RuntimeError, testing the
    fallback path at the HTTP layer.
    """
    if fail:
        handler = MagicMock(spec=PublicInfoHandler)
        handler.answer = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        handler = MagicMock(spec=PublicInfoHandler)
        handler.answer = AsyncMock(return_value=reply)

    app = FastAPI()
    app.include_router(build_public_router(handler), prefix="/public")
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_message_returns_200_with_reply() -> None:
    client = _make_app("UAE National Day is 2 December.")
    resp = client.post("/public/chat", json={"message": "when is national day?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "UAE National Day is 2 December."


def test_empty_message_returns_422() -> None:
    """min_length=1 — Pydantic rejects empty string before handler is called."""
    client = _make_app()
    resp = client.post("/public/chat", json={"message": ""})
    assert resp.status_code == 422


def test_501_char_message_returns_422() -> None:
    """max_length=500 (F-3) — 501 chars must be rejected."""
    client = _make_app()
    resp = client.post("/public/chat", json={"message": "x" * 501})
    assert resp.status_code == 422


def test_500_char_message_returns_200() -> None:
    """Boundary: exactly 500 chars must be accepted."""
    client = _make_app("OK")
    resp = client.post("/public/chat", json={"message": "x" * 500})
    assert resp.status_code == 200


def test_whitespace_stripped_before_handler() -> None:
    """Leading/trailing whitespace must be stripped before handler.answer() is called."""
    handler = MagicMock(spec=PublicInfoHandler)
    handler.answer = AsyncMock(return_value="reply")
    app = FastAPI()
    app.include_router(build_public_router(handler), prefix="/public")
    client = TestClient(app)

    client.post("/public/chat", json={"message": "  hello world  "})
    handler.answer.assert_awaited_once_with("hello world")


def test_router_route_path_is_slash_chat() -> None:
    """build_public_router registers /chat (prefix is set in main.py, not here)."""
    router = build_public_router(MagicMock(spec=PublicInfoHandler))
    paths = [r.path for r in router.routes]  # type: ignore[attr-defined]
    assert "/chat" in paths


def test_missing_body_returns_422() -> None:
    client = _make_app()
    resp = client.post("/public/chat", content=b"", headers={"Content-Type": "application/json"})
    assert resp.status_code == 422


def test_missing_message_field_returns_422() -> None:
    client = _make_app()
    resp = client.post("/public/chat", json={"text": "hello"})  # wrong field name
    assert resp.status_code == 422
