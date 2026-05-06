"""RFC 7662 token introspection client with short positive cache.

Sprint 0 scaffold; activated in Sprint 2 (HR introspect ON, IT introspect ON).

Design notes:
- Default cache TTL: 2 s (positive only). Set short to keep revocation
  latency budget at ≤5 s end-to-end.
- Cache key: `jti` if present, else `hash(token)` (P9 deferred — hash fallback
  is unconditional in v3).
- BCL-driven cache-bust: orchestrator pushes invalidation events to
  `/internal/auth/cache-bust`; this module exposes `evict_by_sid()` and
  `evict_by_sub()` for the receiving handler.
- Fail-closed: if introspection endpoint is unreachable AND no cached
  positive entry exists for the token, return `503 introspection_unavailable`
  (callers map to that response).
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class IntrospectionConfig:
    introspection_url: str
    client_id: str
    client_secret: str
    cache_ttl_seconds: float = 2.0  # POC default; production uses BCL cache-bust


@dataclass
class IntrospectionResult:
    active: bool
    sub: Optional[str] = None
    sid: Optional[str] = None
    aud: Optional[str] = None
    scope: Optional[str] = None
    raw: dict = field(default_factory=dict)


class Introspector:
    """Per-service introspection client with cache + bust hooks.

    Single instance per resource server.
    """

    def __init__(self, cfg: IntrospectionConfig):
        self.cfg = cfg
        # cache: key -> (expiry_unix_ts, IntrospectionResult)
        self._cache: dict[str, tuple[float, IntrospectionResult]] = {}

    @staticmethod
    def _key(token: str, jti: Optional[str]) -> str:
        return jti or hashlib.sha256(token.encode()).hexdigest()

    def introspect(self, token: str, jti: Optional[str] = None) -> IntrospectionResult:
        """Sprint 0 stub. Sprint 2 implementation:
        1. Check cache; if hit and unexpired, return.
        2. POST to /oauth2/introspect with client_credentials.
        3. Cache positive result for ttl seconds.
        4. Raise on transport error so caller can fail-closed.
        """
        raise NotImplementedError(
            "common.auth.introspector.Introspector.introspect — implemented in Sprint 2"
        )

    def evict_by_sid(self, sid: str) -> int:
        """Drop all cached entries whose result.sid matches. Returns count evicted."""
        evicted = 0
        for k, (_, result) in list(self._cache.items()):
            if result.sid == sid:
                del self._cache[k]
                evicted += 1
        return evicted

    def evict_by_sub(self, sub: str) -> int:
        evicted = 0
        for k, (_, result) in list(self._cache.items()):
            if result.sub == sub:
                del self._cache[k]
                evicted += 1
        return evicted

    def _gc(self) -> None:
        now = time.time()
        for k, (exp, _) in list(self._cache.items()):
            if exp < now:
                del self._cache[k]
