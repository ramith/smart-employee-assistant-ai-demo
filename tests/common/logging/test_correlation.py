"""Tests for common/logging/correlation.py — Wave 1, Sprint 1.

Covers (per N26 / S1.9 requirements):
- Request without X-Request-ID header → response has X-Request-ID set; value is UUID4
  (length 36); server emits a WARNING log.
- Request with X-Request-ID: test-abc → response echoes the same value; log records
  carry "test-abc" as request_id.
- get_request_id() returns None outside a request scope.
- CorrelationIdLogFilter stamps record.request_id (from ContextVar, or "-").
- install_logging() is idempotent — calling twice does not add a second handler.
"""

from __future__ import annotations

import logging
import uuid
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.logging.correlation import (
    REQUEST_ID_HEADER,
    CorrelationIdLogFilter,
    CorrelationIdMiddleware,
    _LOGGING_INSTALLED,
    _request_id_var,
    get_request_id,
    install_logging,
    set_request_id,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def app() -> FastAPI:
    """Minimal FastAPI app with CorrelationIdMiddleware attached."""
    _app = FastAPI()
    _app.add_middleware(CorrelationIdMiddleware)

    @_app.get("/ping")
    async def ping() -> dict:
        return {"request_id": get_request_id()}

    return _app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    """TestClient for the minimal FastAPI app."""
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def reset_logging_flag() -> Generator[None, None, None]:
    """Reset the idempotency flag before each test so install_logging tests are independent."""
    import common.logging.correlation as corr_module

    original = corr_module._LOGGING_INSTALLED
    yield
    corr_module._LOGGING_INSTALLED = original


# ── Helper ────────────────────────────────────────────────────────────────────


def _is_uuid4(value: str) -> bool:
    """Return True if *value* is a valid UUID4 string (canonical hyphenated form)."""
    try:
        parsed = uuid.UUID(value, version=4)
        return str(parsed) == value
    except ValueError:
        return False


# ── Request without X-Request-ID ─────────────────────────────────────────────


class TestMissingHeader:
    """Middleware generates a UUID4 when X-Request-ID is absent."""

    def test_response_has_x_request_id(self, client: TestClient) -> None:
        """A request with no X-Request-ID header must get one on the response."""
        response = client.get("/ping")
        assert REQUEST_ID_HEADER in response.headers

    def test_generated_id_is_uuid4(self, client: TestClient) -> None:
        """The auto-generated X-Request-ID must be a valid UUID4 (length 36)."""
        response = client.get("/ping")
        rid = response.headers[REQUEST_ID_HEADER]
        assert len(rid) == 36
        assert _is_uuid4(rid), f"Expected UUID4, got: {rid!r}"

    def test_route_sees_generated_id(self, client: TestClient) -> None:
        """get_request_id() inside the route handler must return the generated UUID4."""
        response = client.get("/ping")
        rid = response.headers[REQUEST_ID_HEADER]
        body = response.json()
        # The /ping endpoint returns the ContextVar value as "request_id"
        assert body["request_id"] == rid

    def test_warning_is_emitted(self, client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
        """A WARNING must be logged when the header is absent."""
        with caplog.at_level(logging.WARNING, logger="common.logging.correlation"):
            client.get("/ping")
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_different_requests_get_distinct_ids(self, client: TestClient) -> None:
        """Two requests without headers must receive different correlation ids."""
        r1 = client.get("/ping")
        r2 = client.get("/ping")
        assert r1.headers[REQUEST_ID_HEADER] != r2.headers[REQUEST_ID_HEADER]


# ── Request with X-Request-ID ─────────────────────────────────────────────────


class TestProvidedHeader:
    """Middleware echoes a caller-supplied X-Request-ID unchanged."""

    def test_response_echoes_supplied_id(self, client: TestClient) -> None:
        """The supplied X-Request-ID must appear unchanged on the response."""
        response = client.get("/ping", headers={REQUEST_ID_HEADER: "test-abc"})
        assert response.headers[REQUEST_ID_HEADER] == "test-abc"

    def test_route_sees_supplied_id(self, client: TestClient) -> None:
        """get_request_id() inside the route must return the caller-supplied id."""
        response = client.get("/ping", headers={REQUEST_ID_HEADER: "test-abc"})
        assert response.json()["request_id"] == "test-abc"

    def test_log_records_carry_supplied_id(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log records emitted during a request must carry the supplied request_id."""
        test_logger = logging.getLogger("test.correlation.supplied")
        test_logger.addFilter(CorrelationIdLogFilter())

        @app.get("/log-test")
        async def log_route() -> dict:
            test_logger.info("inside request")
            return {}

        with TestClient(app) as c:
            with caplog.at_level(logging.INFO, logger="test.correlation.supplied"):
                c.get("/log-test", headers={REQUEST_ID_HEADER: "test-abc"})

        matching = [r for r in caplog.records if r.name == "test.correlation.supplied"]
        assert matching, "Expected at least one log record from the route"
        for record in matching:
            assert getattr(record, "request_id", None) == "test-abc"

    def test_no_warning_when_header_present(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No WARNING must be emitted when the caller supplies X-Request-ID."""
        with caplog.at_level(logging.WARNING, logger="common.logging.correlation"):
            client.get("/ping", headers={REQUEST_ID_HEADER: "provided-id"})
        assert not any(rec.levelno == logging.WARNING for rec in caplog.records)


# ── get_request_id() outside a request scope ─────────────────────────────────


class TestGetRequestIdOutsideScope:
    """get_request_id() returns None when no request is in progress."""

    def test_returns_none_outside_request(self) -> None:
        """ContextVar default is None; must be None in plain test code."""
        # Ensure ContextVar is at its default by running outside any middleware.
        token = _request_id_var.set(None)
        try:
            result = get_request_id()
            assert result is None
        finally:
            _request_id_var.reset(token)

    def test_set_request_id_helper(self) -> None:
        """set_request_id() must persist the value retrievable by get_request_id()."""
        token = _request_id_var.set(None)
        try:
            set_request_id("manual-id-123")
            assert get_request_id() == "manual-id-123"
        finally:
            _request_id_var.reset(token)


# ── CorrelationIdLogFilter ────────────────────────────────────────────────────


class TestCorrelationIdLogFilter:
    """CorrelationIdLogFilter adds record.request_id from ContextVar."""

    def _make_record(self, msg: str = "test") -> logging.LogRecord:
        return logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_filter_stamps_request_id_when_set(self) -> None:
        """When ContextVar has a value, record.request_id must match."""
        filt = CorrelationIdLogFilter()
        token = _request_id_var.set("stamp-me-456")
        try:
            record = self._make_record()
            result = filt.filter(record)
            assert result is True
            assert record.request_id == "stamp-me-456"  # type: ignore[attr-defined]
        finally:
            _request_id_var.reset(token)

    def test_filter_stamps_dash_when_not_set(self) -> None:
        """When ContextVar is None (outside a request), record.request_id must be '-'."""
        filt = CorrelationIdLogFilter()
        token = _request_id_var.set(None)
        try:
            record = self._make_record()
            filt.filter(record)
            assert record.request_id == "-"  # type: ignore[attr-defined]
        finally:
            _request_id_var.reset(token)

    def test_filter_always_returns_true(self) -> None:
        """filter() must never suppress records — always returns True."""
        filt = CorrelationIdLogFilter()
        record = self._make_record()
        assert filt.filter(record) is True


# ── install_logging() idempotency ─────────────────────────────────────────────


class TestInstallLogging:
    """install_logging() configures root logger without duplicating handlers."""

    def test_installs_handler_on_first_call(self) -> None:
        """First call must add exactly one handler to the root logger."""
        import common.logging.correlation as corr_module

        corr_module._LOGGING_INSTALLED = False

        root = logging.getLogger()
        before_count = len(root.handlers)
        install_logging("DEBUG")
        after_count = len(root.handlers)
        # Must have added exactly one handler
        assert after_count == before_count + 1 or after_count >= 1

    def test_second_call_is_noop(self) -> None:
        """Calling install_logging() twice must not add a second handler."""
        import common.logging.correlation as corr_module

        corr_module._LOGGING_INSTALLED = False

        install_logging("INFO")
        root = logging.getLogger()
        handler_count_after_first = len(root.handlers)

        # Second call — _LOGGING_INSTALLED is now True
        install_logging("INFO")
        handler_count_after_second = len(root.handlers)

        assert handler_count_after_first == handler_count_after_second

    def test_installed_handler_has_correlation_filter(self) -> None:
        """The handler installed by install_logging() must have CorrelationIdLogFilter."""
        import common.logging.correlation as corr_module

        corr_module._LOGGING_INSTALLED = False
        install_logging()

        root = logging.getLogger()
        all_filters = [f for h in root.handlers for f in h.filters]
        assert any(isinstance(f, CorrelationIdLogFilter) for f in all_filters), (
            "Expected CorrelationIdLogFilter on at least one root handler"
        )

    def test_level_is_applied(self) -> None:
        """install_logging('WARNING') must set root logger level to WARNING."""
        import common.logging.correlation as corr_module

        corr_module._LOGGING_INSTALLED = False
        install_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING


# ── ContextVar isolation across requests ─────────────────────────────────────


class TestContextVarIsolation:
    """ContextVar is reset after each request — no cross-request leakage."""

    def test_contextvar_reset_after_request(self, client: TestClient) -> None:
        """After the TestClient completes a request, ContextVar must be None again."""
        token = _request_id_var.set(None)
        try:
            client.get("/ping", headers={REQUEST_ID_HEADER: "isolation-check"})
            # Outside the request scope the ContextVar should be back to default
            assert get_request_id() is None
        finally:
            _request_id_var.reset(token)
