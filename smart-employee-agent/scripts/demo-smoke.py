#!/usr/bin/env python3
"""demo-smoke.py — Healthz smoke test for the smart-employee-agent demo stack.

Usage:
    python3 scripts/demo-smoke.py                 # full smoke (healthz only for Sprint 1)
    python3 scripts/demo-smoke.py --skip-chat-test # same effect in Sprint 1; explicit flag

Exit codes:
    0  — all services healthy
    1  — one or more services unhealthy or unreachable

Sprint 1 note:
    Full chat-flow smoke (the UC-03 consent widget sequence) requires a browser
    automation layer for the CIBA consent step.  That is deferred to Sprint 2
    manual QA.  For Sprint 1 this script confirms /healthz on all 5 backend
    services and the SPA root path.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Final

# httpx is a project dependency (used by orchestrator/agents); fall back to
# urllib if it is not installed in the caller's environment.
try:
    import httpx

    _USE_HTTPX: bool = True
except ImportError:  # pragma: no cover
    import urllib.request
    import urllib.error

    _USE_HTTPX = False


# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServiceSpec:
    name: str
    healthz_url: str
    expected_status: int = 200


SERVICES: Final[list[ServiceSpec]] = [
    ServiceSpec("orchestrator", "http://localhost:8090/healthz"),
    ServiceSpec("hr_agent",     "http://localhost:8001/healthz"),
    ServiceSpec("it_agent",     "http://localhost:8002/healthz"),
    ServiceSpec("hr_server",    "http://localhost:8000/healthz"),
    ServiceSpec("it_server",    "http://localhost:8004/healthz"),
    # SPA: just check root responds (no /healthz on static server)
    ServiceSpec("client (SPA)", "http://localhost:3001", expected_status=200),
]

# Timeout per request in seconds
REQUEST_TIMEOUT: Final[float] = 5.0


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    service: str
    url: str
    ok: bool
    status: int | None
    elapsed_ms: float
    error: str = ""


def _probe_httpx(spec: ServiceSpec) -> ProbeResult:
    t0 = time.monotonic()
    try:
        # verify=False because IS dev cert is self-signed; healthz endpoints
        # are internal-only so this is acceptable for smoke testing.
        with httpx.Client(verify=False, timeout=REQUEST_TIMEOUT) as client:
            resp = client.get(spec.healthz_url)
        elapsed = (time.monotonic() - t0) * 1000
        ok = resp.status_code == spec.expected_status
        return ProbeResult(
            service=spec.name,
            url=spec.healthz_url,
            ok=ok,
            status=resp.status_code,
            elapsed_ms=elapsed,
            error="" if ok else f"expected {spec.expected_status}, got {resp.status_code}",
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.monotonic() - t0) * 1000
        return ProbeResult(
            service=spec.name,
            url=spec.healthz_url,
            ok=False,
            status=None,
            elapsed_ms=elapsed,
            error=str(exc),
        )


def _probe_urllib(spec: ServiceSpec) -> ProbeResult:  # pragma: no cover
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    t0 = time.monotonic()
    try:
        req = urllib.request.Request(spec.healthz_url)
        with urllib.request.urlopen(req, context=ctx, timeout=REQUEST_TIMEOUT) as resp:
            status = resp.status
        elapsed = (time.monotonic() - t0) * 1000
        ok = status == spec.expected_status
        return ProbeResult(
            service=spec.name,
            url=spec.healthz_url,
            ok=ok,
            status=status,
            elapsed_ms=elapsed,
            error="" if ok else f"expected {spec.expected_status}, got {status}",
        )
    except Exception as exc:
        elapsed = (time.monotonic() - t0) * 1000
        return ProbeResult(
            service=spec.name,
            url=spec.healthz_url,
            ok=False,
            status=None,
            elapsed_ms=elapsed,
            error=str(exc),
        )


def probe(spec: ServiceSpec) -> ProbeResult:
    if _USE_HTTPX:
        return _probe_httpx(spec)
    return _probe_urllib(spec)  # pragma: no cover


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_COL_NAME: Final[int] = 18
_COL_URL: Final[int] = 42
_COL_STATUS: Final[int] = 8
_COL_TIME: Final[int] = 10

_PASS = "PASS"
_FAIL = "FAIL"


def _badge(ok: bool) -> str:
    return f"[ {_PASS} ]" if ok else f"[ {_FAIL} ]"


def _render_table(results: list[ProbeResult]) -> None:
    header = (
        f"{'Service':<{_COL_NAME}}"
        f"{'URL':<{_COL_URL}}"
        f"{'Status':<{_COL_STATUS}}"
        f"{'Time(ms)':<{_COL_TIME}}"
        f"Result"
    )
    sep = "-" * (len(header) + 8)
    print(sep)
    print(header)
    print(sep)
    for r in results:
        status_str = str(r.status) if r.status is not None else "N/A"
        badge = _badge(r.ok)
        row = (
            f"{r.service:<{_COL_NAME}}"
            f"{r.url:<{_COL_URL}}"
            f"{status_str:<{_COL_STATUS}}"
            f"{r.elapsed_ms:>{_COL_TIME - 2}.1f}ms  "
            f"{badge}"
        )
        print(row)
        if not r.ok and r.error:
            print(f"{'':>{_COL_NAME + _COL_URL}}  ERROR: {r.error}")
    print(sep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test: hit /healthz on all demo services.",
    )
    parser.add_argument(
        "--skip-chat-test",
        action="store_true",
        help=(
            "Skip the chat-flow test (Sprint 1: always skipped; "
            "Sprint 2 will add browser-automation for the CIBA consent step)."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.skip_chat_test:
        print("Note: --skip-chat-test set; running healthz checks only.")
    else:
        print(
            "Note: full chat-flow smoke is deferred to Sprint 2 "
            "(requires browser automation for CIBA consent). "
            "Running healthz checks only."
        )

    print()
    print("Probing services...")
    print()

    wall_start = time.monotonic()
    results: list[ProbeResult] = [probe(s) for s in SERVICES]
    total_ms = (time.monotonic() - wall_start) * 1000

    _render_table(results)

    all_ok = all(r.ok for r in results)
    pass_count = sum(1 for r in results if r.ok)
    fail_count = len(results) - pass_count

    print(
        f"\n{pass_count}/{len(results)} services healthy  |  "
        f"total probe time {total_ms:.0f} ms"
    )

    if all_ok:
        print("\nSmoke check PASSED.")
        # TODO (Sprint 2): add chat-flow smoke using playwright for the CIBA
        # consent widget sequence described in UC-03 §Demo storyboard.
        return 0
    else:
        print(f"\nSmoke check FAILED ({fail_count} service(s) unhealthy).")
        print("Check docker compose logs:  docker compose logs --tail=50")
        return 1


if __name__ == "__main__":
    sys.exit(main())
