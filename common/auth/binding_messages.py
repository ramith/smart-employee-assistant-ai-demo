"""Canonical binding-message templates and renderer (sprint-1-fixes.md F-05).

Single source of truth for the user-facing consent strings rendered on the IS
consent screen and in the SPA Consent Widget.  All callers MUST use
:func:`render` — never construct strings inline.

Templates follow ``docs/ux/copy-deck.md`` §5 / §6.

Sprint 3 3B.2 (FIX-17) — reason-branched binding messages
---------------------------------------------------------
The consent widget should reflect *why* the user is being asked to
re-approve. Three reasons land here:

* ``user_signed_out`` — user clicked Sign Out previously, has just
  signed back in. Distinct from a token expiry: the user knows they
  signed out, so the copy acknowledges that ("Your previous session
  ended.") rather than treating this as a routine refresh.
* ``admin_terminated`` — admin terminated the session in IS Console
  (UC-10). Stronger language so the user understands the change wasn't
  caused by their action.
* ``token_expired`` — UC-06 routine re-CIBA. The existing REFRESH
  copy applies.

When ``reason`` is ``None`` (first-ever consent, no logout history),
:data:`FRESH` applies; ``REFRESH`` is reserved for the token-expiry
flow which is detected by ``is_refresh`` (token cache had a prior
entry). :func:`select_template` packages the rule.
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "FRESH",
    "REFRESH",
    "POST_USER_SIGNED_OUT",
    "POST_ADMIN_TERMINATED",
    "select_template",
    "render",
]

# Original templates (Sprint 1).
FRESH = "{agent_label} wants to {action} on your behalf — request {request_id_short}"
REFRESH = (
    "{agent_label}'s previous access has expired — re-approve to {action} on your behalf"
    " — request {request_id_short}"
)

# Sprint 3 3B.2 additions.
POST_USER_SIGNED_OUT = (
    "Your previous session ended — re-approve {agent_label} to {action} on your behalf"
    " — request {request_id_short}"
)
POST_ADMIN_TERMINATED = (
    "Your previous session was ended by your administrator — re-approve {agent_label}"
    " to {action} on your behalf — request {request_id_short}"
)

# Type alias for the supported reasons. ``None`` means "no recorded reason".
LogoutReason = Literal["user_signed_out", "admin_terminated", "token_expired"]


def select_template(
    reason: str | None,
    *,
    is_refresh: bool,
) -> str:
    """Pick the right binding-message template for this CIBA invocation.

    Precedence (most-specific first):

    1. ``reason="admin_terminated"`` → :data:`POST_ADMIN_TERMINATED`.
       Beats every other signal because the user needs to know their
       session ended for an external reason.
    2. ``reason="user_signed_out"`` → :data:`POST_USER_SIGNED_OUT`.
       Acknowledges the explicit sign-out without conflating with a
       routine refresh.
    3. ``is_refresh=True`` (any reason) → :data:`REFRESH`. Routine UC-06
       token-expiry re-CIBA. The cache had a prior entry that aged out.
    4. otherwise → :data:`FRESH`. First-ever consent for this
       (user, agent, scope) tuple.

    ``reason="token_expired"`` is treated as a synonym of ``is_refresh``
    so callers that prefer to pass the reason explicitly get the same
    REFRESH copy.

    Args:
        reason: One of the catalogued reasons, or ``None`` if no recent
            logout/expiry signal applies.
        is_refresh: ``True`` when the agent's token cache had a prior
            entry for this (user, agent, scope). Detected by the agent's
            CIBA dispatcher.

    Returns:
        The template string to pass to :func:`render`.
    """
    if reason == "admin_terminated":
        return POST_ADMIN_TERMINATED
    if reason == "user_signed_out":
        return POST_USER_SIGNED_OUT
    if reason == "token_expired" or is_refresh:
        return REFRESH
    return FRESH


def render(template: str, *, agent_label: str, action: str, request_id: str) -> str:
    """Render a binding-message template with concrete values.

    Args:
        template: One of the ``*`` constants in this module, or the
            return value of :func:`select_template`.
        agent_label: Human-readable agent name, e.g. ``"HR Agent"``.
        action: Plain-language description of the operation,
            e.g. ``"View your leave balance"``.
        request_id: Full correlation request ID; only the first 8 characters
            are included in the rendered string.

    Returns:
        The fully rendered binding-message string.

    Example::

        msg = render(
            select_template("admin_terminated", is_refresh=False),
            agent_label="HR Agent",
            action="View your leave balance",
            request_id="abc12345-long-id",
        )
    """
    return template.format(
        agent_label=agent_label,
        action=action,
        request_id_short=request_id[:8],
    )
