"""Uniform error envelope shared across orchestrator, specialists, and backends.

Used to ensure HR Agent's `_check_tool_errors()` parser (existing convention)
keeps working unchanged when error responses cross service boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Application-level error codes (per milestone-plan §3.4 task 19).
# Standard JSON-RPC codes (-32600..-32603) are emitted directly by jsonrpc.py.
ERR_INVALID_AUDIENCE = "invalid_audience"          # -32001
ERR_INSUFFICIENT_SCOPE = "insufficient_scope"      # -32002
ERR_PEER_NOT_TRUSTED = "peer_not_trusted"          # -32003
ERR_SESSION_TERMINATED = "session_terminated"      # -32004
ERR_TOKEN_EXPIRED = "token_expired"                # -32005
ERR_TOKEN_REVOKED = "token_revoked"                # -32006

JSONRPC_CODE = {
    ERR_INVALID_AUDIENCE: -32001,
    ERR_INSUFFICIENT_SCOPE: -32002,
    ERR_PEER_NOT_TRUSTED: -32003,
    ERR_SESSION_TERMINATED: -32004,
    ERR_TOKEN_EXPIRED: -32005,
    ERR_TOKEN_REVOKED: -32006,
}


@dataclass
class ErrorEnvelope:
    """Typed error payload — never raw exception strings.

    Mirrors the existing hr-server error shape so legacy callers
    (agent/main.py::_check_tool_errors) keep working.
    """

    error: str
    message: str
    required_scope: Optional[str] = None
    available_scopes: list[str] = field(default_factory=list)
    correlation_id: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"error": self.error, "message": self.message}
        if self.required_scope is not None:
            d["required_scope"] = self.required_scope
        if self.available_scopes:
            d["available_scopes"] = self.available_scopes
        if self.correlation_id is not None:
            d["correlation_id"] = self.correlation_id
        return d


class AuthError(Exception):
    """Raised by validators; mapped to JSON-RPC or HTTP 4xx by the caller."""

    def __init__(self, envelope: ErrorEnvelope, http_status: int = 401):
        super().__init__(envelope.message)
        self.envelope = envelope
        self.http_status = http_status
