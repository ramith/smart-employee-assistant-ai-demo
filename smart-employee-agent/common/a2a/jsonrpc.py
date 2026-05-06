"""JSON-RPC 2.0 helpers for A2A `message/send`.

Per milestone-plan §3.4 task 14:
- Single endpoint per specialist (`POST /a2a`); method only in body.
- Reject batch requests with -32600 (POC scope).
- `data` field is a typed payload — never raw exception strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union


# Standard JSON-RPC 2.0 codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application codes (mirrors common.auth.errors.JSONRPC_CODE)
INVALID_AUDIENCE = -32001
INSUFFICIENT_SCOPE = -32002
PEER_NOT_TRUSTED = -32003
SESSION_TERMINATED = -32004
TOKEN_EXPIRED = -32005
TOKEN_REVOKED = -32006


@dataclass
class JsonRpcRequest:
    method: str
    params: dict[str, Any]
    id: Union[str, int, None]

    @classmethod
    def from_body(cls, body: Any) -> "JsonRpcRequest":
        if isinstance(body, list):
            raise JsonRpcError(INVALID_REQUEST, "batch requests not supported")
        if not isinstance(body, dict):
            raise JsonRpcError(INVALID_REQUEST, "request must be a JSON object")
        if body.get("jsonrpc") != "2.0":
            raise JsonRpcError(INVALID_REQUEST, "jsonrpc must be '2.0'")
        method = body.get("method")
        if not isinstance(method, str):
            raise JsonRpcError(INVALID_REQUEST, "method must be a string")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise JsonRpcError(INVALID_PARAMS, "params must be an object")
        return cls(method=method, params=params, id=body.get("id"))


@dataclass
class JsonRpcError(Exception):
    code: int
    message: str
    data: Optional[dict[str, Any]] = None

    def __post_init__(self):
        super().__init__(self.message)

    def to_response(self, request_id: Union[str, int, None]) -> dict:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            err["data"] = self.data
        return {"jsonrpc": "2.0", "id": request_id, "error": err}


def success_response(request_id: Union[str, int, None], result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}
