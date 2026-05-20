"""Sprint 3 3A.2: tests for orchestrator.agent_registry.revoke_client.

Covers fan-out parallelism, retry-once on transient failure, partial-failure
reporting, and the SECURITY_DEGRADED ERROR log on all-legs-failure (FIX-6).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest


_REPO = Path(__file__).resolve().parents[3]


def _load_module(dotted: str, rel: str) -> types.ModuleType:
    full = _REPO / rel
    spec = importlib.util.spec_from_file_location(dotted, full)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_module(
    "orchestrator.agent_registry.revoke_client",
    "orchestrator/agent_registry/revoke_client.py",
)
FanOutTarget = _mod.FanOutTarget
InternalEventsClient = _mod.InternalEventsClient


@pytest.mark.asyncio
async def test_fan_out_all_succeed() -> None:
    handler_calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        handler_calls.append(str(request.url))
        return httpx.Response(200, json={"acked": True})

    transport = httpx.MockTransport(handler)
    client = InternalEventsClient(
        targets=[
            FanOutTarget(label="hr", url="http://hr-mock"),
            FanOutTarget(label="it", url="http://it-mock"),
        ],
        shared_secret="s",
        retry_once_after_ms=1,
    )
    # Inject the mock transport.
    client._http = httpx.AsyncClient(transport=transport, timeout=5.0)

    report = await client.fan_out(
        jti="j1", user_sub="u1", exp=10**12, reason="user_signed_out", request_id="r"
    )
    assert sorted(report.successes) == ["hr", "it"]
    assert report.failures == []
    assert all("/internal/events" in u for u in handler_calls)
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_once_on_first_failure() -> None:
    """500 then 200 → success on retry."""
    state = {"hr": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["hr"] += 1
        if state["hr"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"acked": True})

    transport = httpx.MockTransport(handler)
    client = InternalEventsClient(
        targets=[FanOutTarget(label="hr", url="http://hr-mock")],
        shared_secret="s",
        retry_once_after_ms=1,
    )
    client._http = httpx.AsyncClient(transport=transport, timeout=5.0)

    report = await client.fan_out(
        jti="j", user_sub="u", exp=1.0, reason="x", request_id="r"
    )
    assert report.successes == ["hr"]
    assert report.failures == []
    assert state["hr"] == 2  # one retry
    await client.aclose()


@pytest.mark.asyncio
async def test_persistent_failure_records_in_failures() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    transport = httpx.MockTransport(handler)
    client = InternalEventsClient(
        targets=[
            FanOutTarget(label="hr", url="http://hr-mock"),
            FanOutTarget(label="it", url="http://it-mock"),
        ],
        shared_secret="s",
        retry_once_after_ms=1,
    )
    client._http = httpx.AsyncClient(transport=transport, timeout=5.0)

    report = await client.fan_out(
        jti="j", user_sub="u", exp=1.0, reason="x", request_id="r"
    )
    assert report.successes == []
    assert sorted(label for label, _ in report.failures) == ["hr", "it"]
    assert report.all_failed
    await client.aclose()


@pytest.mark.asyncio
async def test_r_logout_7b_security_degraded_log_emitted_on_all_legs_failure(caplog) -> None:
    """R-LOGOUT-7b (FIX-6): all-legs-failure must emit SECURITY_DEGRADED label.

    SIEM grep target. The exact string ``logout_fanout_total_failure
    SECURITY_DEGRADED`` is what an alert pipeline keys on. Any future
    rename of either token must propagate to ops dashboards in lockstep.
    """
    import logging

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    client = InternalEventsClient(
        targets=[FanOutTarget(label="hr", url="http://hr-mock")],
        shared_secret="s",
        retry_once_after_ms=1,
    )
    client._http = httpx.AsyncClient(transport=transport, timeout=5.0)

    with caplog.at_level(logging.ERROR):
        await client.fan_out(jti="j", user_sub="u", exp=1.0, reason="x", request_id="r")

    matched = [
        rec for rec in caplog.records
        if "logout_fanout_total_failure" in rec.getMessage() and "SECURITY_DEGRADED" in rec.getMessage()
    ]
    assert matched, f"Expected SECURITY_DEGRADED log; got: {[r.getMessage() for r in caplog.records]}"
    await client.aclose()


@pytest.mark.asyncio
async def test_r_logout_7_partial_failure_logs_warning_per_target(caplog) -> None:
    """R-LOGOUT-7: half fan-out — IT receiver returns 503; HR succeeds.

    Pins the partial-failure semantics:
      * ``report.successes`` carries the labels that ack'd 200.
      * ``report.failures`` carries (label, err) tuples for the rest.
      * One ``logout_fanout_partial`` WARN per failed leg — these are
        the per-target diagnostic lines an operator greps.

    No SECURITY_DEGRADED ERROR is emitted (R-LOGOUT-7b's territory).
    Confirms the cascade does NOT short-circuit on a single failed leg —
    the legs that DID succeed still propagate the denylist push, so
    captured-token replay against those receivers is rejected as
    designed.
    """
    state = {"hr": 0, "it": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if "hr" in str(request.url):
            return httpx.Response(200, json={"acked": True})
        return httpx.Response(503)

    import logging

    transport = httpx.MockTransport(handler)
    client = InternalEventsClient(
        targets=[
            FanOutTarget(label="hr", url="http://hr-mock"),
            FanOutTarget(label="it", url="http://it-mock"),
        ],
        shared_secret="s",
        retry_once_after_ms=1,
    )
    client._http = httpx.AsyncClient(transport=transport, timeout=5.0)

    with caplog.at_level(logging.WARNING):
        report = await client.fan_out(
            jti="j", user_sub="u", exp=1.0, reason="x", request_id="r"
        )

    assert report.successes == ["hr"]
    assert [t for t, _ in report.failures] == ["it"]
    # Partial-failure WARN exists for the failed leg.
    matched = [r for r in caplog.records if "logout_fanout_partial" in r.getMessage()]
    assert matched
    await client.aclose()


def test_factory_rejects_empty_secret() -> None:
    with pytest.raises(ValueError):
        InternalEventsClient(targets=[], shared_secret="")
