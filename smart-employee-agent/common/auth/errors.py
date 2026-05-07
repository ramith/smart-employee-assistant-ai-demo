"""Uniform exception hierarchy for all auth and CIBA failures across the Smart Employee Agent POC.

Every exception carries an ``error_id`` (an ``ERR-*`` code from ``docs/ux/error-catalog.md``)
and an optional ``details`` mapping.  Callers — especially ``ciba_client.py`` and A2A route
handlers — catch only the specific subclasses they handle; they MUST NOT catch bare
``Exception`` or ``BaseException`` (see sprint-1-fixes.md F-10).

Hierarchy::

    AuthError (base)
    ├── JWTValidationError   ERR-AUTH-006  (signature / iss / exp / aud / jti checks)
    ├── PeerTrustError       ERR-AGENT-002 (act.sub allowlist rejected)
    ├── ScopeError           ERR-MCP-003   (required scope absent from token)
    ├── CIBAError            (base for all CIBA flow failures)
    │   ├── CIBAInitiationError  ERR-CIBA-001
    │   ├── CIBADeniedError      ERR-CIBA-005
    │   ├── CIBAExpiredError     ERR-CIBA-009
    │   ├── CIBATimeoutError     ERR-CIBA-010
    │   └── CIBAPollError        ERR-CIBA-008 (unexpected poll error, not denial/expiry)
    └── ActorTokenError      ERR-CIBA-009  (agent I4 token mint failure)

Note: ``JWTValidationError`` is the default exception for JWT-layer failures; callers
that need a finer error_id MUST pass it explicitly via the ``error_id`` constructor
keyword (e.g. ``JWTValidationError("expired", error_id="ERR-AUTH-008")``).
"""

from __future__ import annotations

__all__ = [
    "AuthError",
    "JWTValidationError",
    "PeerTrustError",
    "ScopeError",
    "CIBAError",
    "CIBAInitiationError",
    "CIBADeniedError",
    "CIBAExpiredError",
    "CIBATimeoutError",
    "CIBAPollError",
    "ActorTokenError",
]


class AuthError(Exception):
    """Base exception for all authentication and authorization failures.

    Attributes:
        error_id: An ``ERR-*`` code from ``docs/ux/error-catalog.md``.
        message: Developer-readable description (not user-facing).
        details: Optional structured context (e.g. ``{"jti": "...", "kid": "..."}``)
            for log enrichment.  Never expose to the end user.
    """

    error_id: str = "ERR-AUTH-000"

    def __init__(
        self,
        message: str = "",
        *,
        error_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.details: dict = details or {}
        # Allow callers to override the class-level error_id at construction time
        # so one subclass can represent multiple catalog entries (e.g. JWTValidationError
        # covers ERR-AUTH-006..008 and ERR-MCP-001..003 depending on which claim failed).
        if error_id is not None:
            self.error_id: str = error_id
        else:
            self.error_id = self.__class__.error_id  # type: ignore[assignment]

    def __str__(self) -> str:
        return f"{self.error_id}: {self.message}"

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"error_id={self.error_id!r}, message={self.message!r}, "
            f"details={self.details!r})"
        )


# ── JWT-layer failures ────────────────────────────────────────────────────────


class JWTValidationError(AuthError):
    """JWT signature, iss, exp, aud, or jti validation failed.

    Default error_id is ``ERR-AUTH-006`` (bad signature).  Pass ``error_id=`` at
    construction to pin a narrower catalog entry such as ``ERR-AUTH-007`` (bad issuer),
    ``ERR-AUTH-008`` (token expired), ``ERR-AUTH-010`` (missing jti),
    ``ERR-MCP-001`` (aud mismatch at MCP layer), ``ERR-MCP-002`` (act.sub mismatch),
    or ``ERR-MCP-003`` (insufficient scope at MCP layer).
    """

    error_id: str = "ERR-AUTH-006"


# ── A2A / peer-trust failures ─────────────────────────────────────────────────


class PeerTrustError(AuthError):
    """Inbound token's act.sub is not in the specialist's trusted-peer allowlist.

    Maps to ERR-AGENT-002 (untrusted peer; see also sprint-1-fixes.md F-04).
    """

    error_id: str = "ERR-AGENT-002"


# ── Scope failures ────────────────────────────────────────────────────────────


class ScopeError(AuthError):
    """Required OAuth scope is absent from the presented token.

    Maps to ERR-MCP-003 (insufficient_scope at MCP layer).
    """

    error_id: str = "ERR-MCP-003"


# ── CIBA flow failures ────────────────────────────────────────────────────────


class CIBAError(AuthError):
    """Base exception for all CIBA consent-flow failures; carries optional auth_req_id."""

    error_id: str = "ERR-CIBA-000"

    def __init__(
        self,
        message: str = "",
        *,
        error_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, error_id=error_id, details=details)


class CIBAInitiationError(CIBAError):
    """POST /oauth2/ciba returned a 4xx/5xx error response instead of auth_req_id.

    Covers ERR-CIBA-001 (unauthorized_client), ERR-CIBA-002 (invalid_request /
    notification channel), ERR-CIBA-003 (invalid_scope), and ERR-CIBA-004
    (bad login_hint).  Default is ERR-CIBA-001; pass ``error_id=`` to override.
    """

    error_id: str = "ERR-CIBA-001"


class CIBADeniedError(CIBAError):
    """User clicked Deny on the IS consent screen (error=access_denied from IS poll).

    Maps to ERR-CIBA-005 (single-specialist denial).  Multi-specialist variants
    (ERR-CIBA-006, ERR-CIBA-007, ERR-CIBA-008) are composed at the orchestrator
    layer; use ``error_id=`` to override when raising in those contexts.
    """

    error_id: str = "ERR-CIBA-005"


class CIBAExpiredError(CIBAError):
    """auth_req_id timed out at IS (error=expired_token from IS poll).

    Maps to ERR-CIBA-009 (>300 s without user approval).
    """

    error_id: str = "ERR-CIBA-009"


class CIBATimeoutError(CIBAError):
    """Local polling budget exceeded or the cancel_event was set by the caller.

    Also raised when the background asyncio.Task is cancelled (F-10 rule: the
    cancel path produces CIBATimeoutError("cancelled") so callers downstream
    receive a typed exception rather than raw asyncio.CancelledError).

    Maps to ERR-CIBA-010 (user cancelled via SPA widget or SSE disconnect).
    """

    error_id: str = "ERR-CIBA-010"


class CIBAPollError(CIBAError):
    """Unexpected error during /oauth2/token polling (not denial, expiry, or timeout).

    This covers unrecognised IS error codes and other unexpected poll failures.
    Maps to ERR-CIBA-008 (all specialists denied — used as the generic poll-error
    fallback in the error catalog for unclassified poll failures).
    """

    error_id: str = "ERR-CIBA-008"


# ── Actor token failures ──────────────────────────────────────────────────────


class ActorTokenError(AuthError):
    """Agent's I4 (identity-for-agent) token could not be minted via App-Native Auth.

    Maps to ERR-CIBA-009.  Raised by ``ActorTokenProvider.get()`` after all retry
    attempts are exhausted; causes the specialist to return an A2A ErrorPayload
    rather than attempting CIBA initiation.
    """

    error_id: str = "ERR-CIBA-009"
