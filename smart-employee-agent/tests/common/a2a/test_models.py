"""Tests for common/a2a/models.py — Wave 1, Sprint 1.

Coverage targets
----------------
- Round-trip (model_dump_json → model_validate_json) for every payload class.
- A2AMessageResponse discriminated union: each ``type`` value resolves to the
  correct concrete class.
- Unknown ``type`` value raises ``ValidationError``.
- ``ResultPayload(token_exp=1.5)`` raises ``ValidationError`` (strict int).
- ``ConsentRequiredPayload`` default values for ``is_refresh`` and
  ``prior_consent_at``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from common.a2a.models import (
    A2AMessageResponse,
    AwaitRequest,
    CancelRequest,
    CancelResponse,
    ConsentRequiredPayload,
    ErrorPayload,
    MessageSendParams,
    ResultPayload,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_A2A_ADAPTER: TypeAdapter[A2AMessageResponse] = TypeAdapter(A2AMessageResponse)


def _roundtrip_a2a(payload: ConsentRequiredPayload | ResultPayload | ErrorPayload) -> A2AMessageResponse:
    """Serialize to JSON then parse back through the discriminated union adapter."""
    raw_json = payload.model_dump_json()
    return _A2A_ADAPTER.validate_json(raw_json)


# ---------------------------------------------------------------------------
# MessageSendParams round-trip
# ---------------------------------------------------------------------------


class TestMessageSendParams:
    def test_roundtrip_basic(self) -> None:
        original = MessageSendParams(tool="get_leave_balance", args={"employee_id": "abc"})
        restored = MessageSendParams.model_validate_json(original.model_dump_json())
        assert restored.tool == "get_leave_balance"
        assert restored.args == {"employee_id": "abc"}

    def test_roundtrip_empty_args(self) -> None:
        original = MessageSendParams(tool="list_available_assets", args={})
        restored = MessageSendParams.model_validate_json(original.model_dump_json())
        assert restored.args == {}

    def test_missing_tool_raises(self) -> None:
        with pytest.raises(ValidationError):
            MessageSendParams.model_validate({"args": {}})


# ---------------------------------------------------------------------------
# ConsentRequiredPayload round-trip + defaults
# ---------------------------------------------------------------------------


class TestConsentRequiredPayload:
    def _build(self, **overrides: object) -> ConsentRequiredPayload:
        base: dict = {
            "auth_req_id": "ari-001",
            "auth_url": "https://is.example.com/authz/ciba?req=001",
            "agent_label": "HR Agent",
            "action": "View your leave balance",
            "scope": "openid hr.read",
            "binding_message": "HR Agent wants to View your leave balance — request ari-001",
            "expires_in": 300,
        }
        base.update(overrides)
        return ConsentRequiredPayload.model_validate(base)

    def test_roundtrip(self) -> None:
        original = self._build()
        restored = ConsentRequiredPayload.model_validate_json(original.model_dump_json())
        assert restored.auth_req_id == "ari-001"
        assert restored.agent_label == "HR Agent"
        assert restored.scope == "openid hr.read"

    def test_is_refresh_defaults_to_false(self) -> None:
        payload = self._build()
        assert payload.is_refresh is False

    def test_prior_consent_at_defaults_to_none(self) -> None:
        payload = self._build()
        assert payload.prior_consent_at is None

    def test_is_refresh_explicit_true(self) -> None:
        payload = self._build(is_refresh=True)
        assert payload.is_refresh is True

    def test_prior_consent_at_roundtrip(self) -> None:
        dt = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)
        payload = self._build(prior_consent_at=dt)
        restored = ConsentRequiredPayload.model_validate_json(payload.model_dump_json())
        # Compare as UTC timestamps to avoid tz-repr differences
        assert restored.prior_consent_at is not None
        assert restored.prior_consent_at.timestamp() == pytest.approx(dt.timestamp())

    def test_expires_in_must_be_int(self) -> None:
        with pytest.raises(ValidationError):
            self._build(expires_in=300.5)  # float where int required (strict)

    def test_type_discriminant_is_consent_required(self) -> None:
        payload = self._build()
        assert payload.type == "consent_required"


# ---------------------------------------------------------------------------
# ResultPayload round-trip + strict int enforcement
# ---------------------------------------------------------------------------


class TestResultPayload:
    def _build(self, **overrides: object) -> ResultPayload:
        base: dict = {
            "data": {"leave_days": 12, "leave_type": "Annual"},
            "token_jti": "jti-abc-123",
            "token_exp": 1_746_700_000,
            "token_iat": 1_746_696_400,
        }
        base.update(overrides)
        return ResultPayload.model_validate(base)

    def test_roundtrip(self) -> None:
        original = self._build()
        restored = ResultPayload.model_validate_json(original.model_dump_json())
        assert restored.data == {"leave_days": 12, "leave_type": "Annual"}
        assert restored.token_jti == "jti-abc-123"
        assert restored.token_exp == 1_746_700_000
        assert restored.token_iat == 1_746_696_400

    def test_token_exp_float_raises_validation_error(self) -> None:
        """F-03 lock: token_exp must be int; float input must be rejected."""
        with pytest.raises(ValidationError):
            ResultPayload.model_validate(
                {
                    "data": {},
                    "token_jti": "jti-x",
                    "token_exp": 1.5,  # float — must fail under strict=True
                    "token_iat": 1_746_696_400,
                }
            )

    def test_token_iat_float_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            ResultPayload.model_validate(
                {
                    "data": {},
                    "token_jti": "jti-x",
                    "token_exp": 1_746_700_000,
                    "token_iat": 1.9,
                }
            )

    def test_type_discriminant_is_result(self) -> None:
        payload = self._build()
        assert payload.type == "result"

    def test_data_field_name_is_data_not_payload(self) -> None:
        """F-03 lock: field must be 'data', never 'payload'."""
        raw = json.loads(self._build().model_dump_json())
        assert "data" in raw
        assert "payload" not in raw


# ---------------------------------------------------------------------------
# ErrorPayload round-trip
# ---------------------------------------------------------------------------


class TestErrorPayload:
    def _build(self, **overrides: object) -> ErrorPayload:
        base: dict = {
            "error_id": "ERR-CIBA-005",
            "reason": "user denied consent",
        }
        base.update(overrides)
        return ErrorPayload.model_validate(base)

    def test_roundtrip(self) -> None:
        original = self._build()
        restored = ErrorPayload.model_validate_json(original.model_dump_json())
        assert restored.error_id == "ERR-CIBA-005"
        assert restored.reason == "user denied consent"

    def test_type_discriminant_is_error(self) -> None:
        assert self._build().type == "error"

    def test_mcp_error_code(self) -> None:
        payload = self._build(error_id="ERR-MCP-002", reason="act.sub mismatch")
        restored = ErrorPayload.model_validate_json(payload.model_dump_json())
        assert restored.error_id == "ERR-MCP-002"


# ---------------------------------------------------------------------------
# A2AMessageResponse discriminated union
# ---------------------------------------------------------------------------


class TestA2AMessageResponseUnion:
    def _parse(self, raw: dict) -> A2AMessageResponse:
        return _A2A_ADAPTER.validate_python(raw)

    def test_type_result_returns_result_payload(self) -> None:
        raw = {
            "type": "result",
            "data": {"leave_days": 5},
            "token_jti": "jti-1",
            "token_exp": 1_746_700_000,
            "token_iat": 1_746_696_000,
        }
        parsed = self._parse(raw)
        assert isinstance(parsed, ResultPayload)
        assert parsed.data == {"leave_days": 5}

    def test_type_consent_required_returns_consent_required_payload(self) -> None:
        raw = {
            "type": "consent_required",
            "auth_req_id": "ari-002",
            "auth_url": "https://is.example.com/authz/ciba?req=002",
            "agent_label": "IT Agent",
            "action": "List available assets",
            "scope": "openid it.read",
            "binding_message": "IT Agent wants to List available assets — request ari-002",
            "expires_in": 300,
        }
        parsed = self._parse(raw)
        assert isinstance(parsed, ConsentRequiredPayload)
        assert parsed.auth_req_id == "ari-002"

    def test_type_error_returns_error_payload(self) -> None:
        raw = {
            "type": "error",
            "error_id": "ERR-CIBA-009",
            "reason": "auth_req_id expired",
        }
        parsed = self._parse(raw)
        assert isinstance(parsed, ErrorPayload)
        assert parsed.error_id == "ERR-CIBA-009"

    def test_unknown_type_raises_validation_error(self) -> None:
        raw = {
            "type": "unknown_type_xyz",
            "data": "some data",
        }
        with pytest.raises(ValidationError):
            self._parse(raw)

    def test_missing_type_raises_validation_error(self) -> None:
        raw = {"data": {"x": 1}}
        with pytest.raises(ValidationError):
            self._parse(raw)

    def test_json_roundtrip_via_union_result(self) -> None:
        original = ResultPayload(
            data={"assets": []},
            token_jti="jti-rr",
            token_exp=1_746_700_000,
            token_iat=1_746_696_000,
        )
        json_str = original.model_dump_json()
        restored = _A2A_ADAPTER.validate_json(json_str)
        assert isinstance(restored, ResultPayload)
        assert restored.token_jti == "jti-rr"

    def test_json_roundtrip_via_union_error(self) -> None:
        original = ErrorPayload(error_id="ERR-AGENT-001", reason="bad sig")
        restored = _A2A_ADAPTER.validate_json(original.model_dump_json())
        assert isinstance(restored, ErrorPayload)

    def test_json_roundtrip_via_union_consent(self) -> None:
        original = ConsentRequiredPayload(
            auth_req_id="ari-003",
            auth_url="https://is.example.com/ciba",
            agent_label="HR Agent",
            action="View balance",
            scope="openid hr.read",
            binding_message="HR Agent wants to View balance — request ari-003",
            expires_in=300,
        )
        restored = _A2A_ADAPTER.validate_json(original.model_dump_json())
        assert isinstance(restored, ConsentRequiredPayload)


# ---------------------------------------------------------------------------
# Two-phase A2A helpers (AwaitRequest, CancelRequest, CancelResponse)
# ---------------------------------------------------------------------------


class TestAwaitRequest:
    def test_roundtrip(self) -> None:
        original = AwaitRequest(auth_req_id="ari-await-1")
        restored = AwaitRequest.model_validate_json(original.model_dump_json())
        assert restored.auth_req_id == "ari-await-1"

    def test_missing_auth_req_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            AwaitRequest.model_validate({})


class TestCancelRequest:
    def test_roundtrip(self) -> None:
        original = CancelRequest(auth_req_id="ari-cancel-1")
        restored = CancelRequest.model_validate_json(original.model_dump_json())
        assert restored.auth_req_id == "ari-cancel-1"


class TestCancelResponse:
    def test_cancelled_true(self) -> None:
        resp = CancelResponse(cancelled=True, reason="polling task aborted")
        restored = CancelResponse.model_validate_json(resp.model_dump_json())
        assert restored.cancelled is True
        assert restored.reason == "polling task aborted"

    def test_cancelled_false_reason_defaults_to_none(self) -> None:
        resp = CancelResponse(cancelled=False)
        assert resp.reason is None
        restored = CancelResponse.model_validate_json(resp.model_dump_json())
        assert restored.reason is None

    def test_cancelled_false_with_reason(self) -> None:
        resp = CancelResponse(cancelled=False, reason="auth_req_id not found")
        restored = CancelResponse.model_validate_json(resp.model_dump_json())
        assert restored.cancelled is False
        assert restored.reason == "auth_req_id not found"


# ---------------------------------------------------------------------------
# __all__ completeness
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_all_exported_names_importable(self) -> None:
        import common.a2a.models as m

        for name in m.__all__:
            assert hasattr(m, name), f"__all__ lists {name!r} but it is not defined"

    def test_all_contains_expected_names(self) -> None:
        import common.a2a.models as m

        expected = {
            "MessageSendParams",
            "ConsentRequiredPayload",
            "ResultPayload",
            "ErrorPayload",
            "A2AMessageResponse",
            "AwaitRequest",
            "CancelRequest",
            "CancelResponse",
        }
        assert expected.issubset(set(m.__all__))
