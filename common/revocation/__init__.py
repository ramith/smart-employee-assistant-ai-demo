"""common.revocation — shared denylist + /internal/events receiver primitive.

Used by 4 receivers (HR-AGENT, IT-AGENT, hr_server, it_server) to host
the orchestrator-driven cache-bust fan-out from Sprint 3 3A.2.

Boundary rules
--------------
- F-09: classes here are plain Python (not Pydantic). They hold runtime
  asyncio state (sweeper Task, denylist set/dict).
- Q5 single-process invariant (BLOCK-I): the denylist is in-process. With
  uvicorn ``--workers > 1`` worker A's revoke would not reach worker B,
  silently breaking correctness. Each receiver MUST assert
  ``UVICORN_WORKERS == 1`` at startup.
- Source-confirmed F-21: revoking token-A at IS does NOT propagate to OBO
  tokens. The orchestrator-driven fan-out fed into this denylist is the
  ONLY revocation primitive for OBO tokens. Don't weaken it.

Public surface
--------------
- ``JtiDenylist``  — the in-process denylist (FIX-3 single implementation).
- ``RevocationState`` — DI container for receiver state.
- ``build_internal_events_router(...)`` — FastAPI router factory shared by
  all 4 receivers (avoids 4 copies of the same handler).
"""

from common.revocation.jti_denylist import JtiDenylist, RevocationState
from common.revocation.internal_events import build_internal_events_router

__all__ = [
    "JtiDenylist",
    "RevocationState",
    "build_internal_events_router",
]
