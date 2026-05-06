"""Shared Agent-to-Agent (A2A) primitives.

Hand-rolled JSON-RPC 2.0 transport + agent-card schema. See
docs/agent-card-schema.md and docs/milestone-plan.md §2.2.
"""
from .agent_card import (
    AgentCard,
    Skill,
    Capabilities,
    AuthBlock,
    SCHEMA_VERSION,
    llm_projection,
)
from .jsonrpc import (
    JsonRpcRequest,
    JsonRpcError,
    success_response,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    INVALID_AUDIENCE,
    INSUFFICIENT_SCOPE,
    PEER_NOT_TRUSTED,
    SESSION_TERMINATED,
    TOKEN_EXPIRED,
    TOKEN_REVOKED,
)
from .a2a_client import A2AClient, A2AClientConfig, HeaderCallable

__all__ = [
    "AgentCard",
    "Skill",
    "Capabilities",
    "AuthBlock",
    "SCHEMA_VERSION",
    "llm_projection",
    "JsonRpcRequest",
    "JsonRpcError",
    "success_response",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "INVALID_AUDIENCE",
    "INSUFFICIENT_SCOPE",
    "PEER_NOT_TRUSTED",
    "SESSION_TERMINATED",
    "TOKEN_EXPIRED",
    "TOKEN_REVOKED",
    "A2AClient",
    "A2AClientConfig",
    "HeaderCallable",
]
