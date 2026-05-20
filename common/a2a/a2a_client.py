"""Orchestrator-side A2A client.

Sprint 0 scaffold; real impl in Sprint 1.

Responsibilities:
- Fetch agent cards from URLs in ORCHESTRATOR_AGENT_CARD_URLS allowlist.
- Validate schema; cache 5 min (jittered).
- Per-card refresh cooldown ≥30 s between forced refetches.
- POST `message/send` with header-callable for per-request bearer injection.
- The token-exchange `resource` parameter is the **allowlisted URL**, not
  the card body's `auth.audience` (advisory only).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


HeaderCallable = Callable[[], Awaitable[dict[str, str]]]
"""Async callable that returns headers for the next request.

Used to inject Bearer tokens that are minted just-in-time per call,
without rebuilding the underlying httpx.AsyncClient. See
milestone-plan §3.4 task 10.
"""


@dataclass
class A2AClientConfig:
    allowed_card_urls: list[str]  # ORCHESTRATOR_AGENT_CARD_URLS
    card_cache_ttl_seconds: int = 300
    refresh_cooldown_seconds: int = 30
    timeout_seconds: float = 10.0


class A2AClient:
    """Fetch cards, dispatch JSON-RPC. Single instance per orchestrator process."""

    def __init__(self, cfg: A2AClientConfig):
        self.cfg = cfg
        self._cards: dict[str, "object"] = {}  # url -> AgentCard
        self._last_refresh: dict[str, float] = {}

    async def discover_all(self) -> list[tuple[str, "object"]]:
        """Sprint 0 stub. Sprint 1 fetches each allowlisted URL, validates, caches."""
        raise NotImplementedError(
            "common.a2a.a2a_client.A2AClient.discover_all — implemented in Sprint 1"
        )

    async def message_send(
        self,
        agent_url: str,
        method: str,
        params: dict,
        get_headers: HeaderCallable,
        correlation_id: Optional[str] = None,
    ) -> dict:
        """POST /a2a {jsonrpc, method, params, id} → return result or raise on JSON-RPC error.

        Sprint 0 stub.
        """
        raise NotImplementedError(
            "common.a2a.a2a_client.A2AClient.message_send — implemented in Sprint 1"
        )
