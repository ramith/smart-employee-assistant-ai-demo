"""Canonical binding-message templates and renderer (sprint-1-fixes.md F-05).

Single source of truth for the user-facing consent strings rendered on the IS
consent screen and in the SPA Consent Widget.  All callers MUST use
:func:`render` — never construct strings inline.

Templates follow ``docs/ux/copy-deck.md`` §5 / §6.
"""

from __future__ import annotations

__all__ = ["FRESH", "REFRESH", "render"]

FRESH = "{agent_label} wants to {action} on your behalf — request {request_id_short}"
REFRESH = (
    "{agent_label}'s previous access has expired — re-approve to {action} on your behalf"
    " — request {request_id_short}"
)


def render(template: str, *, agent_label: str, action: str, request_id: str) -> str:
    """Render a binding-message template with concrete values.

    Args:
        template: One of :data:`FRESH` or :data:`REFRESH`.
        agent_label: Human-readable agent name, e.g. ``"HR Agent"``.
        action: Plain-language description of the operation,
            e.g. ``"View your leave balance"``.
        request_id: Full correlation request ID; only the first 8 characters
            are included in the rendered string.

    Returns:
        The fully rendered binding-message string.

    Example::

        msg = render(FRESH, agent_label="HR Agent",
                     action="View your leave balance",
                     request_id="abc12345-long-id")
        # "HR Agent wants to View your leave balance on your behalf — request abc12345"
    """
    return template.format(
        agent_label=agent_label,
        action=action,
        request_id_short=request_id[:8],
    )
