"""X-Request-ID correlation header propagation for the Smart Employee Agent.

This module provides:
- A ContextVar for the current request_id, accessible anywhere in an async call stack.
- ``CorrelationIdMiddleware`` — Starlette/FastAPI middleware that reads or generates
  the ``X-Request-ID`` header on every inbound request and echoes it on the response.
- ``CorrelationIdLogFilter`` — logging.Filter that stamps ``record.request_id`` on
  every log record so structured formatters can include it without extra plumbing.
- ``install_logging`` — convenience helper that attaches a request_id-aware formatter
  to the root logger. Idempotent.

Design notes (per sprint-1-fixes.md F-16 / §4 T6):
    - Default policy when the header is absent: **generate UUID4 + emit WARNING**.
      Stricter refusal policy is a Sprint 2 task (N26).
    - The ContextVar is reset per request via ``Token`` returned by ``ContextVar.set()``
      so concurrent async requests each carry their own id.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ── Public constants ──────────────────────────────────────────────────────────

REQUEST_ID_HEADER: str = "X-Request-ID"

# Paths where the absence of an X-Request-ID is expected (browsers don't add
# custom headers to static asset GETs, healthchecks come from compose itself,
# favicon is a browser-driven side request). The middleware still generates a
# rid for these requests — it just skips the WARN to keep the log clean.
_NO_WARN_PATHS: frozenset[str] = frozenset({
    "/",
    "/app.js",
    "/styles.css",
    "/favicon.ico",
    "/healthz",
    # Top-level navigations triggered by user click or IdP redirect; the
    # browser cannot add custom headers to these.
    "/auth/login",
    "/agent-callback",
})

# Prefix matches for paths whose suffix is dynamic (e.g. session id) but for
# which a missing X-Request-ID is still expected. EventSource (SSE subscribe)
# cannot send custom headers per the W3C spec.
_NO_WARN_PREFIXES: tuple[str, ...] = (
    "/events/",
)


def _should_warn_on_missing_rid(path: str) -> bool:
    if path in _NO_WARN_PATHS:
        return False
    for prefix in _NO_WARN_PREFIXES:
        if path.startswith(prefix):
            return False
    return True

# ── Internal ContextVar ───────────────────────────────────────────────────────

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_logger = logging.getLogger(__name__)

# ── Public accessor helpers ───────────────────────────────────────────────────


def get_request_id() -> str | None:
    """Return the current request_id from the ContextVar, or None if not in a request scope.

    Returns:
        The UUID4 string previously set by ``CorrelationIdMiddleware`` (or
        ``set_request_id``), or ``None`` when called outside a request context
        (e.g., background tasks, startup code).
    """
    return _request_id_var.get()


def set_request_id(rid: str) -> None:
    """Set the request_id on the current ContextVar.

    Intended for internal middleware use and for outbound HTTP clients that
    need to propagate the id to a downstream service before entering the
    middleware-managed scope (e.g., pre-flight health checks, test harnesses).

    Args:
        rid: The request correlation identifier to store.
    """
    _request_id_var.set(rid)


# ── Middleware ────────────────────────────────────────────────────────────────


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that enforces ``X-Request-ID`` propagation.

    Behaviour:
        - If the incoming request carries ``X-Request-ID``, that value is accepted
          and stored in the ContextVar unchanged.
        - If the header is absent, a UUID4 is generated, a WARNING is logged
          (per F-16: auto-generate with WARN; stricter refusal is Sprint 2), and
          the generated id is stored.
        - The resolved id is echoed as ``X-Request-ID`` on the response in all cases.
        - The ContextVar is reset to its prior value after the response is returned,
          ensuring no cross-request bleed in long-lived workers.

    Usage::

        from fastapi import FastAPI
        from common.logging.correlation import CorrelationIdMiddleware

        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Set ContextVar from header (or generate), then call the next handler.

        Args:
            request: The incoming Starlette request.
            call_next: The next middleware or route handler in the chain.

        Returns:
            The response with ``X-Request-ID`` header appended.
        """
        incoming: str | None = request.headers.get(REQUEST_ID_HEADER)
        if incoming:
            rid = incoming
            generated = False
        else:
            rid = str(uuid.uuid4())
            generated = True

        # Set ContextVar BEFORE emitting any log line so the auto-generated
        # warning is itself stamped with the rid (preserves the audit invariant
        # that every log line in a request scope carries a request_id).
        token = _request_id_var.set(rid)
        try:
            if generated and _should_warn_on_missing_rid(request.url.path):
                _logger.warning(
                    "X-Request-ID header absent on %s %s — generated %s",
                    request.method,
                    request.url.path,
                    rid,
                )
            response: Response = await call_next(request)
        finally:
            _request_id_var.reset(token)

        response.headers[REQUEST_ID_HEADER] = rid
        return response


# ── Log filter ────────────────────────────────────────────────────────────────


class CorrelationIdLogFilter(logging.Filter):
    """Injects ``request_id`` into every log record produced within a request scope.

    Adds the attribute ``record.request_id`` so that log formatters using
    ``%(request_id)s`` (or equivalent JSON fields) automatically include the
    correlation id without any per-call changes to logging statements.

    If called outside a request scope (e.g., a background task, startup log),
    ``record.request_id`` is set to ``"-"`` so formatter strings remain valid.

    Usage::

        handler = logging.StreamHandler()
        handler.addFilter(CorrelationIdLogFilter())
        logging.getLogger().addHandler(handler)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Stamp ``record.request_id`` and return True (never drops records).

        Args:
            record: The log record to mutate.

        Returns:
            Always ``True`` — this filter annotates but never suppresses records.
        """
        record.request_id = _request_id_var.get() or "-"  # type: ignore[attr-defined]
        return True


# ── Root logger configuration ─────────────────────────────────────────────────

_LOGGING_INSTALLED: bool = False
_LOG_FORMAT: str = "%(asctime)s %(levelname)s %(request_id)s %(name)s %(message)s"


def install_logging(level: str = "INFO") -> None:
    """Configure the root logger with a request_id-aware format.

    Format::

        {ts} {level} {request_id} {logger_name} {message}

    The ``request_id`` field is populated by ``CorrelationIdLogFilter``.
    Outside a request scope it renders as ``-``.

    Idempotent: calling this function multiple times has no additional effect
    beyond the first call; existing handlers are not duplicated.

    Args:
        level: Root logging level string (e.g. ``"INFO"``, ``"DEBUG"``).
               Passed to ``logging.basicConfig`` only on the first call.
    """
    global _LOGGING_INSTALLED  # noqa: PLW0603

    if _LOGGING_INSTALLED:
        return

    root = logging.getLogger()
    # Remove any existing handlers to avoid duplicate output when basicConfig
    # was already called implicitly (e.g., by a third-party library import).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.addFilter(CorrelationIdLogFilter())
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    _LOGGING_INSTALLED = True
