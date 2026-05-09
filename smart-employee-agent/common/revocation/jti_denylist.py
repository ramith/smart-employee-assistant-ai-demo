"""common.revocation.jti_denylist — bounded, swept jti denylist.

Sprint 3 3A.2 deliverable. Single shared implementation per Stage 4 FIX-3
so the four receivers (HR-AGENT, IT-AGENT, hr_server, it_server) don't
each carry a copy.

Design constraints (from sprint-3-tech-arch.md §4.1, §4.4):
- In-process state; multi-worker breaks correctness silently (BLOCK-I).
- Hard-capped at 10k entries with FIFO eviction + WARN (FIX-13).
- Periodic sweep drops entries past their JWT ``exp`` (FIX-19 — receivers
  receive ``exp`` on the wire to avoid having to re-derive from the jti
  string).
- Sweeper supervised: re-raise on cancel, log + restart loop on other
  exceptions (FIX-13 supervisor wrap).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = ["JtiDenylist", "RevocationState"]


@dataclass
class JtiDenylist:
    """Bounded, swept set of revoked jtis with their ``exp`` timestamps.

    Use ``add(jti, exp)`` to record a revocation; use ``jti in denylist``
    to check.

    Single-process / single-worker per Q5 + BLOCK-I (see ``common/revocation``).

    Attributes:
        hard_cap: Maximum number of entries; on overflow, FIFO-evict the
            oldest with a WARN log line.
        sweep_interval_seconds: How often the sweeper drops expired entries.
        _items: Ordered jti → exp epoch seconds. Insertion order = FIFO
            eviction order.
    """

    hard_cap: int = 10_000
    sweep_interval_seconds: int = 300
    _items: "OrderedDict[str, float]" = field(default_factory=OrderedDict)

    def add(self, jti: str, exp: float) -> None:
        """Add a (jti, exp) pair to the denylist; evict on overflow.

        Idempotent: re-adding an existing jti is a no-op for membership
        but moves it to the head of the FIFO order (most-recently-touched).

        Args:
            jti: The jti to record.
            exp: JWT ``exp`` claim as Unix epoch seconds. Used by the
                sweeper to drop expired entries.
        """
        # Make idempotent at OrderedDict level + bump recency.
        if jti in self._items:
            self._items.move_to_end(jti, last=True)
            self._items[jti] = exp
            return
        if len(self._items) >= self.hard_cap:
            # FIFO eviction. Logged so demo-runbook can grep for it.
            evicted_jti, evicted_exp = self._items.popitem(last=False)
            logger.warning(
                "denylist_evicted_for_capacity | jti=%s evicted_exp=%s reason=hard_cap",
                evicted_jti[:8],
                evicted_exp,
            )
        self._items[jti] = exp

    def __contains__(self, jti: str) -> bool:
        return jti in self._items

    def __len__(self) -> int:
        return len(self._items)

    def sweep_once(self, now: float | None = None) -> int:
        """Drop expired entries; return count removed."""
        cutoff = now if now is not None else time.time()
        expired = [j for j, e in self._items.items() if e < cutoff]
        for j in expired:
            del self._items[j]
        return len(expired)

    async def sweep_loop(self) -> None:
        """Background task: periodically sweep expired entries.

        Wired via FastAPI ``lifespan`` (FIX-21). On unexpected exception,
        log and continue — the loop must not die on a single sweep failure.
        On ``CancelledError``, propagate.
        """
        while True:
            try:
                await asyncio.sleep(self.sweep_interval_seconds)
            except asyncio.CancelledError:
                raise
            try:
                removed = self.sweep_once()
                if removed:
                    logger.info(
                        "denylist_sweep | removed=%d remaining=%d",
                        removed,
                        len(self._items),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("denylist_sweep_failed | continuing supervisor loop")


@dataclass
class RevocationState:
    """DI container for per-service revocation state.

    Each receiver's ``main.py`` constructs one of these in its lifespan
    and stashes it on ``app.state.revocation``. The shared ``/internal/events``
    router and any service-specific consumers (e.g. HRDispatcher) reach
    for it via that handle.

    Sprint 3 3A.2 ships the denylist field. Sprint 3 3A.3 will add an
    ``introspection_cache`` field for the MCP server flavour.

    Attributes:
        revoked_jtis: The shared ``JtiDenylist`` instance.
        sweep_task: Lifespan-managed sweeper task; populated by main.py.
    """

    revoked_jtis: JtiDenylist = field(default_factory=JtiDenylist)
    sweep_task: asyncio.Task | None = None
