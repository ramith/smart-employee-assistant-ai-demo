"""Shared authentication primitives.

Consumed by orchestrator/, hr_agent/, it_agent/, hr_server/, it_server/.

See docs/architecture/module-layout.md (Sprint 1).
"""
from .errors import (
    AuthError,
    JWTValidationError,
    PeerTrustError,
    ScopeError,
    CIBAError,
    CIBAInitiationError,
    CIBADeniedError,
    CIBAExpiredError,
    CIBATimeoutError,
    CIBAPollError,
    ActorTokenError,
)
from .models import OAuthToken, OBOToken, JWTClaims
from .jwt_validator import ValidatorConfig, JWKSCache, validate
from .peer_trust import extract_chain, validate_chain
from .introspector import Introspector, IntrospectionConfig, IntrospectionResult
from .binding_messages import FRESH, REFRESH, render

__all__ = [
    # errors
    "AuthError", "JWTValidationError", "PeerTrustError", "ScopeError",
    "CIBAError", "CIBAInitiationError", "CIBADeniedError", "CIBAExpiredError",
    "CIBATimeoutError", "CIBAPollError", "ActorTokenError",
    # models
    "OAuthToken", "OBOToken", "JWTClaims",
    # validation
    "ValidatorConfig", "JWKSCache", "validate",
    "extract_chain", "validate_chain",
    # introspection (Sprint 3)
    "Introspector", "IntrospectionConfig", "IntrospectionResult",
    # binding messages (F-05)
    "FRESH", "REFRESH", "render",
]
