"""Shared authentication primitives.

Consumed by orchestrator/, hr-agent/, it-agent/, hr-server/ (introspection
feature-flagged), it-server/.

See docs/milestone-plan.md §2.2.
"""
from .errors import (
    AuthError,
    ErrorEnvelope,
    JSONRPC_CODE,
    ERR_INVALID_AUDIENCE,
    ERR_INSUFFICIENT_SCOPE,
    ERR_PEER_NOT_TRUSTED,
    ERR_SESSION_TERMINATED,
    ERR_TOKEN_EXPIRED,
    ERR_TOKEN_REVOKED,
)
from .jwt_validator import ValidatedClaims, ValidatorConfig, validate
from .introspector import Introspector, IntrospectionConfig, IntrospectionResult
from .peer_trust import extract_chain, validate_chain

__all__ = [
    "AuthError",
    "ErrorEnvelope",
    "JSONRPC_CODE",
    "ERR_INVALID_AUDIENCE",
    "ERR_INSUFFICIENT_SCOPE",
    "ERR_PEER_NOT_TRUSTED",
    "ERR_SESSION_TERMINATED",
    "ERR_TOKEN_EXPIRED",
    "ERR_TOKEN_REVOKED",
    "ValidatedClaims",
    "ValidatorConfig",
    "validate",
    "Introspector",
    "IntrospectionConfig",
    "IntrospectionResult",
    "extract_chain",
    "validate_chain",
]
