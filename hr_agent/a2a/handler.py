"""HR-agent A2A router factory — per-service bind layer (Wave 7).

This module is a thin wiring file.  Its sole responsibility is to read
``HRAgentConfig`` and ``HRDispatcher``, assemble an ``A2ARouterConfig``,
and delegate the actual endpoint logic to ``common/a2a/server.py``
(``build_a2a_router``).  No endpoint logic lives here.

Protocol
--------
Two-phase A2A pattern (sprint-1-fixes.md F-01):
  1. ``POST /a2a/message/send``  — token-A validation, dispatch
  2. ``POST /a2a/await``         — long-poll CIBA completion
  3. ``POST /a2a/cancel``        — abort in-flight CIBA

Token validation (F-04)
-----------------------
ValidatorConfig is built from HRAgentConfig:
  - ``expected_iss``    = ``config.is_issuer``
  - ``jwks_url``        = ``config.is_jwks_url``
  - ``expected_aud``    = ``config.expected_inbound_aud``
  - ``required_scopes`` = ``frozenset()``  (no scope restriction on A2A inbound)
  - ``leeway_seconds``  = 30
  - ``insecure_tls``    = ``config.is_insecure_tls``

Peer trust (F-01 / F-04)
-------------------------
``trusted_orchestrator_subs`` is ``frozenset(config.trusted_orchestrator_subs)``.

Boundary rule (F-09)
--------------------
``HRA2AHandlerDeps`` holds ``asyncio``-containing types (``pending`` dict) so it
MUST be a ``@dataclass``, NOT a Pydantic ``BaseModel``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter

from common.a2a.server import A2APendingState, A2ARouterConfig, build_a2a_router
from common.auth.jwt_validator import JWKSCache, ValidatorConfig
from hr_agent.ciba.orchestrator import HRDispatcher
from hr_agent.config import HRAgentConfig

__all__ = ["HRA2AHandlerDeps", "build_hr_a2a_router"]


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------


@dataclass
class HRA2AHandlerDeps:
    """Dependencies injected into the HR-agent A2A router factory.

    Attributes:
        config: Immutable HR-agent configuration (from env / startup).
        dispatcher: ``HRDispatcher`` instance that implements
            ``DispatchProtocol``; called once per ``/a2a/message/send``.
        pending: In-process map of in-flight CIBA states keyed by
            ``auth_req_id``.  Shared between the router and the dispatcher
            so that ``/a2a/await`` and ``/a2a/cancel`` can locate entries
            registered by the dispatcher.
    """

    config: HRAgentConfig
    dispatcher: HRDispatcher
    pending: dict[str, A2APendingState] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_hr_a2a_router(
    deps: HRA2AHandlerDeps,
    *,
    jwks_cache: JWKSCache | None = None,
) -> APIRouter:
    """Build and return a FastAPI ``APIRouter`` for the three HR-agent A2A endpoints.

    Wires ``HRAgentConfig`` fields into the common ``A2ARouterConfig`` then
    delegates to ``common.a2a.server.build_a2a_router``.

    Wiring decisions:
    - ``validator_config.expected_iss``    ← ``deps.config.is_issuer``
    - ``validator_config.jwks_url``        ← ``deps.config.is_jwks_url``
    - ``validator_config.expected_aud``    ← ``deps.config.expected_inbound_aud``
    - ``validator_config.required_scopes`` ← ``frozenset()``
    - ``validator_config.leeway_seconds``  ← ``30``
    - ``validator_config.insecure_tls``    ← ``deps.config.is_insecure_tls``
    - ``trusted_orchestrator_subs``        ← ``frozenset(deps.config.trusted_orchestrator_subs)``
    - ``pending``                          ← ``deps.pending``
    - ``dispatch``                         ← ``deps.dispatcher``
    - ``await_max_wait_seconds``           ← ``330.0``

    Args:
        deps: Config, dispatcher, and shared pending map for this specialist.
        jwks_cache: Optional pre-built ``JWKSCache``; primarily for testing
            (avoids live JWKS network traffic).

    Returns:
        Configured ``APIRouter`` exposing:
        - ``POST /a2a/message/send``
        - ``POST /a2a/await``
        - ``POST /a2a/cancel``
    """
    cfg = deps.config

    validator_config = ValidatorConfig(
        expected_iss=cfg.is_issuer,
        jwks_url=cfg.is_jwks_url,
        expected_aud=cfg.expected_inbound_aud,
        required_scopes=frozenset(),
        leeway_seconds=30,
        insecure_tls=cfg.is_insecure_tls,
    )

    router_config = A2ARouterConfig(
        validator_config=validator_config,
        trusted_orchestrator_subs=frozenset(cfg.trusted_orchestrator_subs),
        pending=deps.pending,
        dispatch=deps.dispatcher,
        await_max_wait_seconds=330.0,
    )

    return build_a2a_router(router_config, jwks_cache=jwks_cache)
