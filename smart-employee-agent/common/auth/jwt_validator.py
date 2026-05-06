"""JWT validator — JWKS signature, exact-aud, scope, nested act chain.

Sprint 0 scaffold. Real implementation lands in Sprint 1 (consumed by hr-agent,
it-agent, it-server). Wired into hr-server as a no-op pass-through behind a
feature flag for now.

Design notes:
- Issuer is **hardcoded from configuration**, never read from request data
  (agent-card or otherwise). See milestone-plan §3.4 task 17.
- Audience is **exact-match**; multi-aud arrays must have every entry in
  an explicit allowlist (RFC 8707, §3.4 task 17).
- `act` chain is walked recursively; missing `act` is rejected for resources
  that require delegation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .errors import (
    AuthError,
    ErrorEnvelope,
    ERR_INVALID_AUDIENCE,
    ERR_INSUFFICIENT_SCOPE,
    ERR_TOKEN_EXPIRED,
)


@dataclass(frozen=True)
class ValidatorConfig:
    """Per-service validator configuration.

    `issuer` is hardcoded from env/config at startup. Never derived from a card.
    """

    issuer: str
    expected_aud: str  # exact-match canonical resource URI
    required_scopes: list[str]  # at least one must be present
    require_act: bool = True  # reject tokens whose `act` claim is absent
    jwks_url: Optional[str] = None
    introspection_url: Optional[str] = None


@dataclass
class ValidatedClaims:
    sub: str
    aud: str
    scopes: list[str]
    act_chain: list[str]  # outermost-first; e.g., ["hr-agent", "orchestrator-agent"]
    jti: Optional[str]
    sid: Optional[str]
    raw: dict


def validate(token: str, cfg: ValidatorConfig) -> ValidatedClaims:
    """Validate JWT signature, claims, and chain.

    Sprint 0: stub raises NotImplementedError. Sprint 1 implementation:
      1. Decode without verification → check `iss` matches cfg.issuer.
      2. Fetch JWKS from cfg.jwks_url; verify signature.
      3. Check exp/nbf/iat (with skew tolerance).
      4. Check `aud` exact-match against cfg.expected_aud.
      5. Extract scopes; require intersection with cfg.required_scopes.
      6. Walk `act` chain; if cfg.require_act and chain empty, reject.
      7. Optionally introspect (handled by introspector.py, not here).
    """
    raise NotImplementedError(
        "common.auth.jwt_validator.validate — implemented in Sprint 1"
    )


def reject(envelope: ErrorEnvelope, http_status: int = 401) -> AuthError:
    """Helper for validator call sites to raise consistently."""
    return AuthError(envelope, http_status=http_status)
