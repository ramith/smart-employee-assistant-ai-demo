"""Depth-1 act chain validation against a peer-agent allowlist.

Used by every specialist (hr_agent, it_agent) to enforce that only known peer agents
appear in the actor chain of inbound A2A tokens.

Architecture (v4) note — depth-1 only:
    In v4 every token-A produced by Pattern C login carries a depth-1 act claim:
    ``{ "sub": "<user>", "act": { "sub": "<orchestrator-agent-uuid>" } }``.
    Depth-2 nested act chains are NOT produced by WSO2 IS 7.2 for this flow and are
    NOT expected by specialists. ``validate_chain`` raises ``PeerTrustError`` when the
    chain depth exceeds ``max_depth`` (default 1) to surface accidental depth-2 tokens
    rather than silently accepting them.

Per RFC 8693 §4.1, the ``act`` claim may in principle be nested:
    ``{ "sub": "user", "act": { "sub": "hr_agent", "act": { "sub": "orchestrator-agent" } } }``

The chain is walked outermost-first (current/immediate actor first, oldest delegator last).
That ordering matches ``JWTClaims.act_chain()`` in ``common.auth.models``.

Threat model: addresses T4 (orchestrator impersonation) and N-tests N4, N7.
"""
from __future__ import annotations

from .errors import PeerTrustError
from .models import JWTClaims


def extract_chain(claims: JWTClaims) -> list[str]:
    """Walk ``claims.act`` and return the actor sub values, outermost-first.

    Delegates entirely to ``JWTClaims.act_chain()``; this function exists as a
    single stable call-site so callers do not need to import ``models`` directly.

    Args:
        claims: Decoded (and verified) JWT payload as a ``JWTClaims`` instance.

    Returns:
        A list of ``sub`` strings from the nested ``act`` chain, outermost actor
        first.  Returns an empty list when the ``act`` claim is absent or ``None``.

    Examples:
        >>> extract_chain(claims_with_no_act)
        []
        >>> extract_chain(claims_with_depth_1_act)          # act.sub = "agent-a"
        ['agent-a']
        >>> extract_chain(claims_with_depth_2_act)          # act.sub = "b", act.act.sub = "a"
        ['b', 'a']
    """
    return claims.act_chain()


def validate_chain(
    claims: JWTClaims,
    *,
    allowed_peers: set[str] | frozenset[str],
    require_non_empty: bool = True,
    max_depth: int = 1,
) -> None:
    """Validate the ``act`` chain in *claims* against an allowlist of trusted peers.

    Raises ``PeerTrustError`` if any of the following conditions hold:

    - The chain is empty and ``require_non_empty`` is ``True``.
    - The chain length exceeds ``max_depth``.
    - Any actor ``sub`` in the chain is not present in ``allowed_peers``.

    On success returns ``None``.  Callers MUST NOT catch bare ``Exception`` or
    ``BaseException`` here — only catch ``PeerTrustError`` (see sprint-1-fixes.md F-10).

    Args:
        claims: Decoded, verified ``JWTClaims``; its ``act`` field is walked by
            ``extract_chain``.
        allowed_peers: Set (or frozenset) of trusted peer agent identifiers.  Pass
            Asgardeo Agent identity UUIDs — ``act.sub`` in Pattern C / CIBA tokens
            is the UUID, **not** the display name.  See
            ``<agent>/.env`` ``HR_TRUSTED_PEER_AGENTS`` / ``IT_TRUSTED_PEER_AGENTS``.
        require_non_empty: When ``True`` (the default), a missing or empty ``act``
            chain raises ``PeerTrustError``.  Set to ``False`` only for contexts where
            a direct (non-delegated) call is legitimate.
        max_depth: Maximum number of actors permitted in the chain.  Defaults to ``1``
            (v4 depth-1 only).  Raise ``PeerTrustError`` when the actual chain is
            longer than this value.

    Returns:
        ``None`` — callers that previously relied on the return value being the
        validated chain (Sprint 0 API) must call ``extract_chain(claims)`` separately.

    Raises:
        PeerTrustError: On empty chain (when required), depth exceeded, or untrusted
            actor.  ``PeerTrustError.details`` always contains::

                {
                    "chain":   [...],          # the full chain that was evaluated
                    "allowed": [...],          # sorted list of allowed_peers
                    "actor":   "<bad-sub>",    # only present when an actor is rejected
                }

    Examples:
        >>> validate_chain(claims, allowed_peers={"orch-uuid"})          # depth-1, trusted
        >>> validate_chain(claims, allowed_peers=set(), require_non_empty=False)  # depth-0 OK
    """
    chain: list[str] = extract_chain(claims)
    allowed_sorted: list[str] = sorted(allowed_peers)

    # ── Empty chain check ─────────────────────────────────────────────────────
    if not chain:
        if require_non_empty:
            raise PeerTrustError(
                "token has no `act` claim; delegation chain required",
                details={
                    "chain": chain,
                    "allowed": allowed_sorted,
                },
            )
        return

    # ── Depth guard ───────────────────────────────────────────────────────────
    if len(chain) > max_depth:
        raise PeerTrustError(
            f"act chain depth {len(chain)} exceeds max_depth={max_depth}",
            details={
                "chain": chain,
                "allowed": allowed_sorted,
            },
        )

    # ── Per-actor allowlist check ─────────────────────────────────────────────
    for actor in chain:
        if actor not in allowed_peers:
            raise PeerTrustError(
                f"peer agent '{actor}' is not in the trusted-peer allowlist",
                details={
                    "actor": actor,
                    "allowed": allowed_sorted,
                    "chain": chain,
                },
            )
