"""Sprint 3 3A.2: tests for common.revocation.JtiDenylist."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


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
JtiDenylist = _jti_mod.JtiDenylist
RevocationState = _jti_mod.RevocationState


def test_add_then_contains() -> None:
    d = JtiDenylist()
    d.add("jti-1", exp=10_000.0)
    assert "jti-1" in d
    assert "other" not in d


def test_add_idempotent_does_not_grow() -> None:
    d = JtiDenylist()
    d.add("jti-1", 10_000.0)
    d.add("jti-1", 20_000.0)
    assert len(d) == 1


def test_hard_cap_evicts_fifo() -> None:
    d = JtiDenylist(hard_cap=3)
    d.add("a", 1)
    d.add("b", 2)
    d.add("c", 3)
    d.add("d", 4)
    # FIFO eviction: 'a' was oldest.
    assert "a" not in d
    assert "b" in d
    assert "c" in d
    assert "d" in d
    assert len(d) == 3


def test_sweep_drops_expired() -> None:
    d = JtiDenylist()
    d.add("expired", exp=100.0)
    d.add("future", exp=10**12)
    removed = d.sweep_once(now=200.0)
    assert removed == 1
    assert "expired" not in d
    assert "future" in d


def test_sweep_loop_continues_on_exception(monkeypatch) -> None:
    d = JtiDenylist(sweep_interval_seconds=0)  # immediately ready
    calls = {"sweep": 0}

    def boom_then_ok(now=None):
        calls["sweep"] += 1
        if calls["sweep"] == 1:
            raise RuntimeError("simulated")
        return 0

    monkeypatch.setattr(d, "sweep_once", boom_then_ok)

    async def _run() -> None:
        task = asyncio.create_task(d.sweep_loop())
        # Let the loop tick a few times.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert calls["sweep"] >= 1


def test_revocation_state_default() -> None:
    state = RevocationState()
    assert isinstance(state.revoked_jtis, JtiDenylist)
    assert state.sweep_task is None
