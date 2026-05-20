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
    JsonRpcResponse,
    make_request,
    make_success,
    make_error,
    parse_request,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    ERR_PEER_NOT_TRUSTED,
    ERR_INVALID_TOKEN_A,
    ERR_AGENT_INTERNAL,
    ERR_TOOL_NOT_FOUND,
    ERR_AGENT_BAD_REQUEST,
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
    "JsonRpcResponse",
    "make_request",
    "make_success",
    "make_error",
    "parse_request",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "ERR_PEER_NOT_TRUSTED",
    "ERR_INVALID_TOKEN_A",
    "ERR_AGENT_INTERNAL",
    "ERR_TOOL_NOT_FOUND",
    "ERR_AGENT_BAD_REQUEST",
    "A2AClient",
    "A2AClientConfig",
    "HeaderCallable",
]
