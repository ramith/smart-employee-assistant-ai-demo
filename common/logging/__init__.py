"""common.logging — shared logging utilities for the Smart Employee Agent.

Exports:
    CorrelationIdMiddleware: Starlette middleware that reads/generates X-Request-ID.
    CorrelationIdLogFilter: logging.Filter that stamps request_id on every record.
    get_request_id: Returns the current request_id from ContextVar.
    set_request_id: Sets the request_id on the current ContextVar.
    install_logging: Configures root logger with request_id-aware format.
    REQUEST_ID_HEADER: The canonical header name constant.
"""

from common.logging.correlation import (
    REQUEST_ID_HEADER,
    CorrelationIdLogFilter,
    CorrelationIdMiddleware,
    get_request_id,
    install_logging,
    set_request_id,
)

__all__ = [
    "REQUEST_ID_HEADER",
    "CorrelationIdLogFilter",
    "CorrelationIdMiddleware",
    "get_request_id",
    "install_logging",
    "set_request_id",
]
