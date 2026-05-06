"""Nested `act` chain validation against a peer-agent allowlist.

Used by every specialist and backend to enforce that only known peer agents
appear in the actor chain. Walks the chain end-to-end.

Per RFC 8693 §4.1, the `act` claim may be nested:
  { "sub": "user", "act": { "sub": "hr-agent", "act": { "sub": "orchestrator-agent" } } }

The chain walked outermost-first (current actor first, oldest delegator last).
"""
from __future__ import annotations

from .errors import AuthError, ErrorEnvelope, ERR_PEER_NOT_TRUSTED


def extract_chain(claims: dict) -> list[str]:
    """Return outermost-to-innermost list of actor sub claims.

    Empty list if `act` is absent.
    """
    chain: list[str] = []
    cur = claims.get("act")
    while isinstance(cur, dict) and "sub" in cur:
        chain.append(cur["sub"])
        cur = cur.get("act")
    return chain


def validate_chain(claims: dict, allowed_peers: set[str], require_non_empty: bool = True) -> list[str]:
    """Walk `act` chain; every link must be in `allowed_peers`.

    Args:
        claims: decoded JWT payload.
        allowed_peers: set of permitted peer-agent client_ids (e.g., {"orchestrator-agent"}).
        require_non_empty: if True, an absent `act` is rejected.

    Returns:
        The validated chain (list of `sub` values, outermost-first).

    Raises:
        AuthError: if chain is empty and required, or any link is unknown.
    """
    chain = extract_chain(claims)
    if not chain and require_non_empty:
        raise AuthError(
            ErrorEnvelope(
                error="invalid_token",
                message="token has no `act` claim; delegation chain required",
            ),
            http_status=401,
        )
    for link in chain:
        if link not in allowed_peers:
            raise AuthError(
                ErrorEnvelope(
                    error=ERR_PEER_NOT_TRUSTED,
                    message=f"peer agent '{link}' not in allowlist",
                ),
                http_status=403,
            )
    return chain
