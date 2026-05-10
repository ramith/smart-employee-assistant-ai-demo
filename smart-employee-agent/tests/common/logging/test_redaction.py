"""Tests for common.logging.redaction.

Covers every regex pattern and validates F-11 correctness (tuple rebuild,
not in-place mutation).

Run with::

    pytest tests/common/logging/test_redaction.py -v
"""

from __future__ import annotations

import logging

import pytest

from common.logging.redaction import RedactionFilter, redact

# ---------------------------------------------------------------------------
# Shared fixture: a fresh RedactionFilter instance per test
# ---------------------------------------------------------------------------

SAMPLE_JWT = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJ1c2VyMTIzIiwiZXhwIjoxNzE2MDAwMDAwfQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
SAMPLE_OPAQUE = "someOpaqueToken123abc"


# ---------------------------------------------------------------------------
# Helper: build a real LogRecord and run it through the filter
# ---------------------------------------------------------------------------


def _make_record(msg: str, args: tuple | dict | None = None) -> logging.LogRecord:
    """Create a :class:`logging.LogRecord` with the given msg and args."""
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )
    return record


def _apply(record: logging.LogRecord) -> logging.LogRecord:
    """Run RedactionFilter on the record; return the same (mutated) record."""
    f = RedactionFilter()
    result = f.filter(record)
    assert result is True, "filter() must always return True"
    return record


# ===========================================================================
# 1. redact() function — unit tests per pattern
# ===========================================================================


class TestRedactFunction:
    """Unit tests for the standalone :func:`redact` helper."""

    def test_jwt_in_plain_string(self) -> None:
        """JWT three-segment base64 string is replaced with <JWT>."""
        text = f"token={SAMPLE_JWT}"
        result = redact(text)
        assert "<JWT>" in result
        assert SAMPLE_JWT not in result

    def test_bearer_with_jwt_becomes_bearer_redacted(self) -> None:
        """'Bearer eyJ...' is replaced with 'Bearer <REDACTED>'.

        Bearer fires BEFORE the JWT pattern so the whole 'Bearer <token>'
        segment is captured in one pass.  Net outcome: 'Bearer <REDACTED>'.
        """
        text = f"Authorization: Bearer {SAMPLE_JWT}"
        result = redact(text)
        assert "Bearer <REDACTED>" in result
        assert SAMPLE_JWT not in result

    def test_bearer_with_opaque_token(self) -> None:
        """'Bearer <opaque>' is replaced with 'Bearer <REDACTED>'."""
        text = f"Authorization: Bearer {SAMPLE_OPAQUE}"
        result = redact(text)
        assert "Bearer <REDACTED>" in result
        assert SAMPLE_OPAQUE not in result

    def test_auth_req_id_value_redacted(self) -> None:
        """auth_req_id followed by a long value is redacted."""
        long_id = "abc-123-def-456-789-xyz-qrs"  # 27 chars, > 20 threshold
        text = f"auth_req_id={long_id}"
        result = redact(text)
        assert "auth_req_id=<REDACTED>" in result
        assert long_id not in result

    def test_auth_req_id_colon_style(self) -> None:
        """auth_req_id: 'long-value' (JSON style) is redacted."""
        long_id = "urn:openid:ciba:long-auth-request-id-12345"
        text = f'"auth_req_id": "{long_id}"'
        result = redact(text)
        assert long_id not in result
        assert "<REDACTED>" in result

    def test_auth_req_id_short_not_redacted(self) -> None:
        """Short values (< 20 chars) after auth_req_id are NOT redacted.

        This prevents false-positives on debug labels like 'auth_req_id=N/A'.
        """
        text = "auth_req_id=short"  # only 5 chars — below threshold
        result = redact(text)
        # Should be unchanged because the minimum length is 20.
        assert result == text

    def test_actor_token_redacted(self) -> None:
        """actor_token= followed by a JWT is replaced with actor_token=<REDACTED>.

        The actor_token key=value pattern fires BEFORE the bare JWT pattern,
        so the entire 'actor_token=eyJ...' is caught as one unit and the
        result is 'actor_token=<REDACTED>' (not 'actor_token=<JWT>').
        """
        text = f"actor_token={SAMPLE_JWT}"
        result = redact(text)
        assert "actor_token=<REDACTED>" in result
        assert SAMPLE_JWT not in result

    def test_actor_token_opaque_redacted(self) -> None:
        """actor_token=<opaque> is also redacted (not just JWTs)."""
        text = f"actor_token={SAMPLE_OPAQUE}"
        result = redact(text)
        assert "<REDACTED>" in result
        assert SAMPLE_OPAQUE not in result

    def test_client_secret_redacted(self) -> None:
        """client_secret=<value> is replaced with client_secret=<REDACTED>."""
        text = "client_secret=secret123abc"
        result = redact(text)
        assert "client_secret=<REDACTED>" in result
        assert "secret123abc" not in result

    def test_client_secret_json_style(self) -> None:
        """'client_secret': 'value' (JSON-like) is redacted."""
        text = '"client_secret": "mysupersecret"'
        result = redact(text)
        assert "mysupersecret" not in result
        assert "<REDACTED>" in result

    def test_password_redacted(self) -> None:
        """password=<value> is replaced with password=<REDACTED>."""
        text = "password=hunter2"
        result = redact(text)
        assert "password=<REDACTED>" in result
        assert "hunter2" not in result

    def test_password_json_style(self) -> None:
        """password: 'hunter2' is redacted."""
        text = '"password": "hunter2"'
        result = redact(text)
        assert "hunter2" not in result
        assert "<REDACTED>" in result

    def test_clean_message_unchanged(self) -> None:
        """A plain log message with no secrets passes through unchanged."""
        text = "user login successful"
        assert redact(text) == text

    def test_case_insensitive_bearer(self) -> None:
        """BEARER (upper-case) is also redacted."""
        text = f"BEARER {SAMPLE_OPAQUE}"
        result = redact(text)
        assert SAMPLE_OPAQUE not in result

    def test_case_insensitive_auth_req_id(self) -> None:
        """AUTH_REQ_ID (upper-case) is redacted."""
        long_id = "ABCDEF-GHI-JKL-MNO-PQRSTUVWXYZ"  # 30 chars
        text = f"AUTH_REQ_ID={long_id}"
        result = redact(text)
        assert long_id not in result

    def test_jwt_short_segment_not_matched(self) -> None:
        """A string starting with eyJ but with a too-short header is not redacted."""
        # Only 8 chars after "eyJ" — below the ≥10 threshold
        text = "eyJshort.payload.sig"
        result = redact(text)
        # Should NOT be replaced because header segment is only 5 chars.
        assert result == text

    def test_multiple_secrets_in_one_message(self) -> None:
        """Multiple patterns fire independently on the same string."""
        text = (
            f"Sending actor_token={SAMPLE_OPAQUE} "
            f"with client_secret=abc123 "
            f"and Bearer {SAMPLE_OPAQUE}"
        )
        result = redact(text)
        assert SAMPLE_OPAQUE not in result
        assert "abc123" not in result
        assert "<REDACTED>" in result


# ===========================================================================
# 2. RedactionFilter on record.msg
# ===========================================================================


class TestRedactionFilterMsg:
    """Tests that record.msg is sanitised in place."""

    def test_jwt_in_msg_is_replaced(self) -> None:
        """JWT embedded in record.msg is replaced with <JWT>."""
        record = _make_record(f"access_token={SAMPLE_JWT}")
        _apply(record)
        assert SAMPLE_JWT not in record.msg
        assert "<JWT>" in record.msg

    def test_bearer_in_msg(self) -> None:
        """'Bearer <token>' in record.msg is replaced."""
        record = _make_record(f"header=Bearer {SAMPLE_OPAQUE}")
        _apply(record)
        assert SAMPLE_OPAQUE not in record.msg

    def test_clean_msg_unchanged(self) -> None:
        """Clean record.msg is not modified."""
        record = _make_record("user login successful")
        _apply(record)
        assert record.msg == "user login successful"

    def test_filter_always_returns_true_for_msg(self) -> None:
        """filter() returns True even for a record with a JWT in msg."""
        record = _make_record(f"token={SAMPLE_JWT}")
        assert RedactionFilter().filter(record) is True


# ===========================================================================
# 3. RedactionFilter on record.args — F-11 correctness
# ===========================================================================


class TestRedactionFilterArgs:
    """Tests for F-11: tuple rebuild, dict rebuild, and getMessage() safety."""

    def test_jwt_in_args_redacted_in_formatted_message(self) -> None:
        """JWT in record.args is redacted so getMessage() returns no JWT.

        Strategy (per test spec): pass through real LogRecord, call
        getMessage(), assert no JWT in the formatted output.
        """
        record = _make_record("token=%s", args=(SAMPLE_JWT,))
        _apply(record)
        formatted = record.getMessage()
        assert SAMPLE_JWT not in formatted
        assert "<JWT>" in formatted

    def test_args_tuple_rebuilt_as_tuple(self) -> None:
        """After filtering, record.args is still a tuple (not a list)."""
        record = _make_record("a=%s b=%s", args=(SAMPLE_JWT, "normal"))
        _apply(record)
        assert isinstance(record.args, tuple), (
            "F-11 violation: record.args must remain a tuple after filtering"
        )

    def test_args_tuple_values_redacted(self) -> None:
        """Each element of the args tuple has secrets removed."""
        record = _make_record(
            "token=%s secret=%s",
            args=(SAMPLE_JWT, "client_secret=abc"),
        )
        _apply(record)
        assert isinstance(record.args, tuple)
        for arg in record.args:
            assert SAMPLE_JWT not in arg

    def test_args_none_not_touched(self) -> None:
        """record.args=None (no format args) is left as-is."""
        record = _make_record("plain message", args=None)
        _apply(record)
        assert record.args is None

    def test_args_empty_tuple_not_touched(self) -> None:
        """record.args=() (empty tuple) is left falsy / unchanged."""
        record = _make_record("no args", args=())
        _apply(record)
        # Empty tuple is falsy; filter skips it; getMessage() still works.
        assert record.getMessage() == "no args"

    def test_args_dict_rebuilt_as_dict(self) -> None:
        """Dict-style args (%(key)s format) are rebuilt as a new dict.

        Python's logging.LogRecord constructor has a quirk when instantiated
        directly with a dict as args — it tries args[0] which raises KeyError.
        Use logging.makeLogRecord() to bypass the constructor guard and set
        args directly on the record object.
        """
        record = logging.makeLogRecord(
            {
                "name": "test.logger",
                "level": logging.INFO,
                "msg": "token=%(tok)s",
                "args": {"tok": SAMPLE_JWT},
            }
        )
        _apply(record)
        assert isinstance(record.args, dict)
        # actor_token key=value pattern fires first, then JWT catches residuals.
        # Here the dict value is just a bare JWT string so the JWT pattern fires.
        assert SAMPLE_JWT not in record.args["tok"]

    def test_multiple_args_all_redacted(self) -> None:
        """All elements in a multi-element tuple are processed."""
        # Use a realistic CIBA auth_req_id value (UUID-style, ≥20 chars)
        long_id = "3f2b1a0e-9c8d-4e5f-b6a7-1234567890ab"
        record = _make_record(
            "id=%s tok=%s",
            args=(f"auth_req_id={long_id}", SAMPLE_JWT),
        )
        _apply(record)
        assert isinstance(record.args, tuple)
        formatted = record.getMessage()
        assert SAMPLE_JWT not in formatted
        assert long_id not in formatted

    def test_non_string_args_coerced_and_redacted(self) -> None:
        """Non-string args (e.g. integers) are coerced via str() without error."""
        record = _make_record("value=%s", args=(42,))
        _apply(record)
        assert isinstance(record.args, tuple)
        assert record.args[0] == "42"

    def test_getMessage_with_clean_args_unchanged(self) -> None:
        """A record with no secrets in args produces unchanged getMessage()."""
        record = _make_record("user=%s action=%s", args=("alice", "login"))
        _apply(record)
        assert record.getMessage() == "user=alice action=login"

    def test_filter_always_returns_true_for_args(self) -> None:
        """filter() always returns True regardless of args content."""
        record = _make_record("tok=%s", args=(SAMPLE_JWT,))
        assert RedactionFilter().filter(record) is True


# ===========================================================================
# 4. install_redaction_filter smoke test
# ===========================================================================


class TestInstallRedactionFilter:
    """Smoke test: install_redaction_filter attaches to the target logger."""

    def test_installed_filter_redacts_on_emit(self, caplog: pytest.LogCaptureFixture) -> None:
        """After install, log records through that logger have secrets stripped."""
        from common.logging.redaction import install_redaction_filter

        install_redaction_filter("test.redaction.install")
        logger = logging.getLogger("test.redaction.install")
        logger.setLevel(logging.DEBUG)

        with caplog.at_level(logging.DEBUG, logger="test.redaction.install"):
            logger.info("token=%s", SAMPLE_JWT)

        # caplog captures the formatted message
        assert SAMPLE_JWT not in caplog.text


class TestInternalAuthRedaction:
    """Hardening: X-Internal-Auth header value must be stripped if ever logged."""

    def test_x_internal_auth_value_stripped_from_log_line(self) -> None:
        """A log line containing 'X-Internal-Auth: <secret>' has the value redacted."""
        from common.logging.redaction import redact

        line = "outbound headers: X-Internal-Auth: 04e3fcde75749022af1327497937fb1343890610 ; other=ok"
        out = redact(line)
        assert "04e3fcde75749022af1327497937fb1343890610" not in out
        assert "<REDACTED>" in out
        # Header name must survive so the redaction is auditable.
        assert "X-Internal-Auth" in out or "x-internal-auth" in out.lower()

    def test_x_internal_auth_in_dict_repr_stripped(self) -> None:
        """Dict-repr style: {'X-Internal-Auth': 'shhh'} has the value redacted."""
        from common.logging.redaction import redact

        line = "headers={'X-Internal-Auth': 'shared-secret-here', 'Content-Type': 'json'}"
        out = redact(line)
        assert "shared-secret-here" not in out
        assert "<REDACTED>" in out
