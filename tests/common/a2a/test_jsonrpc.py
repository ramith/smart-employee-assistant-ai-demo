"""Tests for common/a2a/jsonrpc.py — JSON-RPC 2.0 envelope helpers.

Coverage targets:
- make_request: method, params, auto-id generation, caller-supplied id
- Round-trip: serialize → parse_request → equal to original
- make_success / make_error: correct envelope shapes
- Invariant: both result+error set raises ValidationError
- Invariant: neither result nor error set raises ValidationError
- No-notifications guarantee: make_request always produces an id
- parse_request: rejects batch (list) input
- Error code constants: spot-check values from api-contracts.md §3
"""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from common.a2a.jsonrpc import (
    ERR_AGENT_BAD_REQUEST,
    ERR_AGENT_INTERNAL,
    ERR_INVALID_TOKEN_A,
    ERR_PEER_NOT_TRUSTED,
    ERR_TOOL_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcResponse,
    make_error,
    make_request,
    make_success,
    parse_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_uuid4(value: str) -> bool:
    """Return True if value is a valid UUID4 string."""
    try:
        parsed = uuid.UUID(value, version=4)
        return str(parsed) == value
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# make_request
# ---------------------------------------------------------------------------


class TestMakeRequest:
    def test_produces_valid_jsonrpc_envelope(self) -> None:
        req = make_request("message/send", {"foo": "bar"})
        assert req.jsonrpc == "2.0"
        assert req.method == "message/send"
        assert req.params == {"foo": "bar"}

    def test_auto_generates_uuid4_id_when_none_given(self) -> None:
        req = make_request("message/send")
        assert isinstance(req.id, str)
        assert _is_uuid4(req.id), f"Expected UUID4, got {req.id!r}"

    def test_caller_supplied_id_is_preserved(self) -> None:
        req = make_request("message/send", request_id="fixed-id-42")
        assert req.id == "fixed-id-42"

    def test_integer_id_via_caller_supply(self) -> None:
        # make_request signature is str | None for request_id, but callers who
        # need an integer id build JsonRpcRequest directly; this test confirms
        # the model accepts int ids.
        req = JsonRpcRequest(method="ping", params={}, id=7)
        assert req.id == 7

    def test_no_notifications_make_request_always_has_id(self) -> None:
        """This profile does not support notifications — id must always be set."""
        for _ in range(5):
            req = make_request("any/method")
            assert req.id is not None, "id must never be None from make_request"

    def test_consecutive_calls_produce_distinct_ids(self) -> None:
        ids = {make_request("m").id for _ in range(20)}
        assert len(ids) == 20, "UUID4 ids should be unique across calls"

    def test_list_params_accepted(self) -> None:
        req = make_request("sum", [1, 2, 3])
        assert req.params == [1, 2, 3]

    def test_default_params_is_empty_dict(self) -> None:
        req = make_request("ping")
        assert req.params == {}


# ---------------------------------------------------------------------------
# Round-trip: serialize → parse_request
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_dict_roundtrip_equals_original(self) -> None:
        original = make_request("message/send", {"tool": "get_leave_balance"})
        wire = original.model_dump()
        parsed = parse_request(wire)
        assert parsed.jsonrpc == original.jsonrpc
        assert parsed.id == original.id
        assert parsed.method == original.method
        assert parsed.params == original.params

    def test_parse_request_validates_jsonrpc_version(self) -> None:
        with pytest.raises(ValidationError):
            parse_request({"jsonrpc": "1.0", "id": "x", "method": "ping", "params": {}})

    def test_parse_request_rejects_missing_method(self) -> None:
        with pytest.raises(ValidationError):
            parse_request({"jsonrpc": "2.0", "id": "x", "params": {}})

    def test_parse_request_rejects_missing_id(self) -> None:
        """No-notifications profile: id is required."""
        with pytest.raises(ValidationError):
            parse_request({"jsonrpc": "2.0", "method": "ping", "params": {}})

    def test_parse_request_rejects_batch(self) -> None:
        with pytest.raises(TypeError, match="Batch requests are not supported"):
            parse_request([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# make_success
# ---------------------------------------------------------------------------


class TestMakeSuccess:
    def test_success_envelope_shape(self) -> None:
        resp = make_success("req-1", {"leave_days": 12})
        assert resp.jsonrpc == "2.0"
        assert resp.id == "req-1"
        assert resp.result == {"leave_days": 12}
        assert resp.error is None

    def test_success_with_none_id(self) -> None:
        resp = make_success(None, {"ok": True})
        assert resp.id is None
        assert resp.result == {"ok": True}

    def test_success_with_integer_id(self) -> None:
        resp = make_success(99, {"data": "x"})
        assert resp.id == 99


# ---------------------------------------------------------------------------
# make_error
# ---------------------------------------------------------------------------


class TestMakeError:
    def test_error_envelope_shape(self) -> None:
        resp = make_error("req-1", ERR_PEER_NOT_TRUSTED, "Token validation failed")
        assert resp.jsonrpc == "2.0"
        assert resp.id == "req-1"
        assert resp.result is None
        assert resp.error is not None
        assert resp.error.code == ERR_PEER_NOT_TRUSTED
        assert resp.error.message == "Token validation failed"
        assert resp.error.data is None

    def test_error_with_data_payload(self) -> None:
        resp = make_error(
            "req-2",
            INTERNAL_ERROR,
            "Unexpected",
            data={"detail": "null pointer"},
        )
        assert resp.error is not None
        assert resp.error.data == {"detail": "null pointer"}

    def test_error_with_none_id(self) -> None:
        resp = make_error(None, PARSE_ERROR, "Could not parse JSON")
        assert resp.id is None
        assert resp.error is not None
        assert resp.error.code == PARSE_ERROR


# ---------------------------------------------------------------------------
# JsonRpcResponse invariant: exactly one of result / error
# ---------------------------------------------------------------------------


class TestResponseInvariant:
    def test_both_result_and_error_raises(self) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            JsonRpcResponse(
                id="x",
                result={"ok": True},
                error=JsonRpcError(code=INTERNAL_ERROR, message="oops"),
            )

    def test_neither_result_nor_error_raises(self) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            JsonRpcResponse(id="x")

    def test_result_only_is_valid(self) -> None:
        resp = JsonRpcResponse(id="x", result={"a": 1})
        assert resp.result == {"a": 1}
        assert resp.error is None

    def test_error_only_is_valid(self) -> None:
        resp = JsonRpcResponse(
            id="x",
            error=JsonRpcError(code=INVALID_REQUEST, message="bad"),
        )
        assert resp.error is not None
        assert resp.result is None


# ---------------------------------------------------------------------------
# Error code constants (spot-check against api-contracts.md §3)
# ---------------------------------------------------------------------------


class TestErrorCodeConstants:
    def test_standard_codes(self) -> None:
        assert PARSE_ERROR == -32700
        assert INVALID_REQUEST == -32600
        assert METHOD_NOT_FOUND == -32601
        assert INVALID_PARAMS == -32602
        assert INTERNAL_ERROR == -32603

    def test_application_codes(self) -> None:
        assert ERR_PEER_NOT_TRUSTED == -32001
        assert ERR_INVALID_TOKEN_A == -32002
        assert ERR_AGENT_INTERNAL == -32003
        assert ERR_TOOL_NOT_FOUND == -32004
        assert ERR_AGENT_BAD_REQUEST == -32005

    def test_application_codes_distinct(self) -> None:
        app_codes = [
            ERR_PEER_NOT_TRUSTED,
            ERR_INVALID_TOKEN_A,
            ERR_AGENT_INTERNAL,
            ERR_TOOL_NOT_FOUND,
            ERR_AGENT_BAD_REQUEST,
        ]
        assert len(app_codes) == len(set(app_codes)), "Application error codes must be distinct"


# ---------------------------------------------------------------------------
# JsonRpcError model
# ---------------------------------------------------------------------------


class TestJsonRpcError:
    def test_minimal_construction(self) -> None:
        err = JsonRpcError(code=-32600, message="Invalid request")
        assert err.code == -32600
        assert err.message == "Invalid request"
        assert err.data is None

    def test_with_data(self) -> None:
        err = JsonRpcError(code=-32001, message="Peer not trusted", data={"act_sub": "unknown"})
        assert err.data == {"act_sub": "unknown"}

    def test_serializes_to_dict(self) -> None:
        err = JsonRpcError(code=-32700, message="Parse error")
        d = err.model_dump()
        assert d["code"] == -32700
        assert d["message"] == "Parse error"
        assert d["data"] is None
