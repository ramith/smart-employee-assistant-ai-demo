"""A2A wire models for the Smart Employee Agent POC.

**Boundary rule (F-09):** All types in this module cross HTTP boundaries and are
Pydantic v2 ``BaseModel`` subclasses.  Runtime-only objects that hold
``asyncio.Task`` / ``asyncio.Event`` MUST use ``@dataclass`` instead (never here).

Public names
------------
MessageSendParams, ConsentRequiredPayload, ResultPayload, ErrorPayload,
A2AMessageResponse, AwaitRequest, CancelRequest, CancelResponse
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Request body for POST /a2a/message/send  (the JSON-RPC ``params`` payload)
# ---------------------------------------------------------------------------


class MessageSendParams(BaseModel):
    """JSON-RPC ``params`` payload for the ``message/send`` method.

    Attributes:
        tool: MCP tool name, e.g. ``"get_leave_balance"``.
        args: Tool-specific keyword arguments; may be empty.
        last_logout_reason: Sprint 3 3B.2 / FIX-17. Optional reason
            string set by the orchestrator when the user's previous
            session ended for a known reason (``"user_signed_out"`` or
            ``"admin_terminated"``). The specialist dispatcher passes
            this to ``binding_messages.select_template`` so the consent
            widget reflects *why* re-approval is being asked. Consumed
            once — orchestrator clears the field on the Session after
            the first A2A invocation that carries it.
    """

    tool: str
    args: dict
    last_logout_reason: str | None = None


# ---------------------------------------------------------------------------
# Discriminated-union variants for the JSON-RPC ``result`` field
# ---------------------------------------------------------------------------


class ConsentRequiredPayload(BaseModel):
    """Returned immediately after the specialist has initiated CIBA.

    The orchestrator must forward ``auth_url`` to the SPA via SSE before
    calling ``POST /a2a/await``.  The ``type`` discriminant is always
    ``"consent_required"``.

    Attributes:
        auth_req_id: Opaque identifier issued by IS ``/oauth2/ciba``.
        auth_url: IS consent URL — open in a new browser tab.
        agent_label: Display name for the Consent Widget (e.g. ``"HR Agent"``).
        action: Plain-language description (e.g. ``"View your leave balance"``).
        scope: Space-separated OAuth scopes being requested.
        binding_message: Verbatim string IS will render on the consent screen.
        expires_in: Seconds until ``auth_req_id`` expires (typically 300).
        is_refresh: ``True`` when this is a UC-06 token-expiry re-CIBA.
        prior_consent_at: Timestamp of the previous approval (UC-06 display).
    """

    model_config = ConfigDict(strict=True)

    type: Literal["consent_required"] = "consent_required"
    auth_req_id: str
    auth_url: str
    agent_label: str
    action: str
    scope: str
    binding_message: str
    expires_in: int
    is_refresh: bool = False
    prior_consent_at: datetime | None = None


class ResultPayload(BaseModel):
    """Returned when polling completes and the MCP tool call succeeds.

    ``data`` is the raw MCP tool output; the orchestrator's LLM composes the
    user-facing sentence from it.  All token timestamps are Unix epoch **integers**
    (not floats) to eliminate float-precision ambiguity at the wire boundary (F-03).

    Attributes:
        data: Tool-specific result body (e.g. ``{"leave_days": 12}``).
        token_jti: JWT ID of the OBO token used — logged in the session map (S1.11a).
        token_exp: Token expiry as Unix epoch seconds (int, not float).
        token_iat: Token issuance time as Unix epoch seconds (int, not float).
    """

    model_config = ConfigDict(strict=True)

    type: Literal["result"] = "result"
    data: dict
    token_jti: str
    token_exp: int  # Unix seconds — INT, not float (F-03 lock)
    token_iat: int  # Unix seconds — INT, not float (F-03 lock)


class ErrorPayload(BaseModel):
    """Returned on CIBA failure (denied / expired) or MCP failure.

    Attributes:
        error_id: Machine-readable error code from the ERR-CIBA-* / ERR-MCP-* /
            ERR-AGENT-* namespace (see ``docs/ux/error-catalog.md``).
        reason: Short technical description for operators — NOT user-facing.
    """

    model_config = ConfigDict(strict=True)

    type: Literal["error"] = "error"
    error_id: str  # ERR-CIBA-* | ERR-MCP-* | ERR-AGENT-*
    reason: str


# Discriminated union — parse via TypeAdapter or model_validate on the
# enclosing JSON-RPC ``result`` field.  Discriminant field is ``type``.
A2AMessageResponse = Annotated[
    ConsentRequiredPayload | ResultPayload | ErrorPayload,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Two-phase A2A endpoints  (F-01)
# ---------------------------------------------------------------------------


class AwaitRequest(BaseModel):
    """Request body for ``POST /a2a/await``.

    The orchestrator sends this after receiving a ``ConsentRequiredPayload`` so
    the specialist can long-poll its internal ``asyncio.Event`` until the CIBA
    polling task completes.  The response is the same ``A2AMessageResponse``
    discriminated union (``ResultPayload`` or ``ErrorPayload``).

    Attributes:
        auth_req_id: Matches the value returned in ``ConsentRequiredPayload``.
    """

    auth_req_id: str


# /a2a/await response reuses A2AMessageResponse — do NOT define a separate type.


class CancelRequest(BaseModel):
    """Request body for ``POST /a2a/cancel``.

    Aborts the background CIBA polling task for a pending ``auth_req_id``.

    Attributes:
        auth_req_id: Identifier of the CIBA flow to cancel.
    """

    auth_req_id: str


class CancelResponse(BaseModel):
    """Response body for ``POST /a2a/cancel``.

    Attributes:
        cancelled: ``True`` if there was a pending CIBA to cancel; ``False`` if
            the ``auth_req_id`` was not found or polling had already finished.
        reason: Optional human-readable note for operators (e.g. why not found).
    """

    cancelled: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "MessageSendParams",
    "ConsentRequiredPayload",
    "ResultPayload",
    "ErrorPayload",
    "A2AMessageResponse",
    "AwaitRequest",
    "CancelRequest",
    "CancelResponse",
]
