"""common.logging.redaction — Regex-based log record sanitisation.

Strips JWT-shaped strings, Bearer tokens, auth_req_id values, actor_token
values, client secrets, and password fields from log records before they
reach any handler (file, stdout, SIEM).

Threat-model reference: §4 T1 — actor_token theft via logs.
Sprint task: S1.11(c) — log-redaction baseline.
F-11 compliance: RedactionFilter rebuilds record.args as a NEW tuple; it
never attempts in-place mutation (tuples are immutable; that would raise
TypeError and silently swallow the log record).

Usage::

    import logging
    from common.logging.redaction import RedactionFilter, install_redaction_filter

    # Attach to a specific logger:
    logger = logging.getLogger("hr_agent")
    logger.addFilter(RedactionFilter())

    # Or install globally on the root logger (recommended at app startup):
    install_redaction_filter()
"""

from __future__ import annotations

import logging
import re
from typing import Union

# ---------------------------------------------------------------------------
# Redaction patterns
# Each entry is (compiled_pattern, replacement_string).
# Patterns are applied left-to-right; the first match wins for each
# non-overlapping segment — re.sub handles all non-overlapping occurrences.
# ---------------------------------------------------------------------------

# Catches compact JWT-shaped strings:  eyJ<header>.<payload>.<signature>
# The header segment must be ≥10 chars to avoid false-positives on short
# base64 blobs that happen to start with "eyJ" (e.g. debug echoes of raw
# bytes).  All three segments use the URL-safe base64 alphabet.
_JWT_PATTERN = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
)

# Catches "Bearer <token>" regardless of token shape.  The JWT pattern above
# would redact the token body, but Bearer tokens may also be opaque strings
# (e.g. IS-issued opaque access tokens) — this pattern covers both.
_BEARER_PATTERN = re.compile(
    r"(?i)Bearer\s+[A-Za-z0-9._\-]+"
)

# Catches auth_req_id assignments/values in log strings.
# Examples:  auth_req_id=abc-123-long, "auth_req_id": "abc-123-long"
#            auth_req_id=urn:openid:ciba:abc123... (colon in value)
# The value must be ≥20 chars to avoid redacting very short debug labels.
# The value character class includes `:` to cover URN-style auth_req_id values
# as issued by some CIBA implementations.
_AUTH_REQ_ID_PATTERN = re.compile(
    r"(?i)(auth_req_id[\"'=:\s]+)[A-Za-z0-9:\-]{20,}"
)

# Catches actor_token assignments in form-data or log key-value pairs.
# Examples:  actor_token=eyJ..., actor_token: "eyJ..."
_ACTOR_TOKEN_PATTERN = re.compile(
    r"(?i)(actor_token[\"'=:\s]+)[A-Za-z0-9._\-]+"
)

# Catches client_secret assignments in log key-value pairs or form data.
# Examples:  client_secret=abc123, "client_secret":"abc123"
_CLIENT_SECRET_PATTERN = re.compile(
    r"(?i)(client_secret[\"'=:\s]+)[A-Za-z0-9._\-]+"
)

# Catches password assignments.  Uses \S+ because passwords may contain any
# non-whitespace character.  Stops at the first whitespace so structured log
# tokens after the value are not consumed.
# Examples:  password=hunter2, password: "hunter2"
_PASSWORD_PATTERN = re.compile(
    r"(?i)(password[\"'=:\s]+)\S+"
)

# Sprint 3 hardening (security retro): defensive add for the
# ``X-Internal-Auth`` header used to authenticate orchestrator → receiver
# fan-out (common/revocation/internal_events.py). Today no log line emits
# the headers dict directly, but if a future debug-log change does, this
# pattern catches it. Same key=value shape as the actor_token pattern.
_INTERNAL_AUTH_PATTERN = re.compile(
    r"(?i)(x-internal-auth[\"'=:\s]+)\S+"
)

# Master list — evaluated in the order below.
#
# Ordering rationale:
# 1. Bearer MUST fire FIRST.  If JWT fires before Bearer, "Bearer eyJ..."
#    becomes "Bearer <JWT>" and the Bearer pattern no longer matches (it
#    expects word-chars, not angle brackets).
# 2. Key=value patterns (actor_token, auth_req_id, client_secret, password)
#    fire BEFORE the bare JWT pattern.  This ensures that when the value IS
#    a JWT (e.g. actor_token=eyJ...) the whole key=value pair is redacted as
#    "actor_token=<REDACTED>" rather than just the JWT body becoming <JWT>
#    while the key name leaks.
# 3. JWT fires last as a catch-all for any remaining bare JWT strings not
#    already consumed by the higher-priority patterns.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_BEARER_PATTERN,        "Bearer <REDACTED>"),
    (_AUTH_REQ_ID_PATTERN,   r"\1<REDACTED>"),
    (_ACTOR_TOKEN_PATTERN,   r"\1<REDACTED>"),
    (_CLIENT_SECRET_PATTERN, r"\1<REDACTED>"),
    (_INTERNAL_AUTH_PATTERN, r"\1<REDACTED>"),
    (_PASSWORD_PATTERN,      r"\1<REDACTED>"),
    (_JWT_PATTERN,           "<JWT>"),
]


def redact(text: str) -> str:
    """Apply every redaction pattern to *text* and return a sanitised copy.

    Args:
        text: The raw string to sanitise (e.g. a log message or argument).

    Returns:
        A new string with all recognised secret-shaped tokens replaced by
        their placeholder (``<JWT>``, ``<REDACTED>``, etc.).  The original
        string is never mutated.
    """
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactionFilter(logging.Filter):
    """Strips JWT-shaped strings, auth_req_id, actor_token, and known secret
    patterns from :class:`logging.LogRecord` objects before they reach any
    handler.

    Per **F-11**: this filter rebuilds ``record.args`` as a **new tuple**
    because tuples are immutable — in-place index assignment raises
    ``TypeError``.  Any code that writes ``record.args[i] = ...`` would
    silently fail (or raise) and leave secrets in the log stream.

    The filter always returns ``True`` (it never drops records).

    Example::

        handler = logging.StreamHandler()
        handler.addFilter(RedactionFilter())

    Or attach to a logger::

        logging.getLogger("orchestrator").addFilter(RedactionFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact *record.msg* and rebuild *record.args* with secrets removed.

        Args:
            record: The log record produced by a ``Logger.log()`` call.

        Returns:
            Always ``True`` — this filter sanitises; it never drops records.
        """
        # Redact the format string / plain message.
        record.msg = redact(str(record.msg))

        # Redact positional arguments.
        # record.args may be:
        #   - a tuple  (logger.info("x=%s", value))
        #   - a dict   (logger.info("%(key)s", {"key": value}))  — rare
        #   - None / falsy  (no args)
        #
        # F-11: NEVER mutate in place.  Tuples are immutable; always build
        # a new tuple.  For dict args we build a new dict.
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(str(v)) for k, v in record.args.items()
                }
            else:
                # Covers tuple and any other sequence-like args shape.
                record.args = tuple(redact(str(a)) for a in record.args)

        return True


def install_redaction_filter(logger_name: str = "") -> None:
    """Attach a :class:`RedactionFilter` to the named logger.

    Args:
        logger_name: The logger to attach to.  Defaults to ``""`` (the root
            logger), which causes all child loggers to inherit redaction
            unless they propagate to a separate handler chain.

    Example::

        # In your FastAPI lifespan startup:
        install_redaction_filter()          # root — covers everything
        install_redaction_filter("uvicorn") # or a specific logger
    """
    target_logger = logging.getLogger(logger_name)
    target_logger.addFilter(RedactionFilter())
