"""Tests for common/auth/peer_trust.py — Wave 2, Sprint 1.

Covers (≥10 tests):
- extract_chain: no-act, depth-1, depth-2
- validate_chain: depth-0 + require_non_empty=True  → PeerTrustError
- validate_chain: depth-0 + require_non_empty=False → no error
- validate_chain: depth-1, actor in allowed_peers   → no error
- validate_chain: depth-1, actor NOT in allowed_peers → PeerTrustError (details has actor)
- validate_chain: depth-2, max_depth=1              → PeerTrustError
- validate_chain: depth-2, max_depth=2, both trusted  → no error
- validate_chain: depth-2, max_depth=2, outer not trusted → PeerTrustError
- allowed_peers accepts set and frozenset
- N4 / N7 threat-model integration scenario

N-test references:
    N4 — Specialist validates token-A's act.sub allowlist.
    N7 — Specialist refuses inbound A2A with act.sub not in trusted-peer set.
"""

from __future__ import annotations

import pytest

from common.auth.errors import PeerTrustError
from common.auth.models import JWTClaims
from common.auth.peer_trust import extract_chain, validate_chain

# ── Helpers ───────────────────────────────────────────────────────────────────

_ORCH_UUID = "00000000-0000-0000-0000-000000000001"
_ROGUE_UUID = "deadbeef-dead-beef-dead-beefdeadbeef"


def _claims(act: dict | None = None) -> JWTClaims:
    """Build a minimal JWTClaims instance with a customisable act claim."""
    return JWTClaims(
        sub="user-uuid",
        iss="https://is.example.com/oauth2/token",
        aud="orchestrator-client-id",
        exp=9_999_999_999,
        iat=1_700_000_000,
        jti="jti-test",
        act=act,
        scope="openid orchestrate",
        aut="APPLICATION_USER",
    )


def _depth_1(actor: str = _ORCH_UUID) -> JWTClaims:
    """Claims with a depth-1 act (single actor)."""
    return _claims(act={"sub": actor})


def _depth_2(outer: str = "agent-b", inner: str = "agent-a") -> JWTClaims:
    """Claims with a depth-2 nested act (outer is the immediate caller)."""
    return _claims(act={"sub": outer, "act": {"sub": inner}})


# ── extract_chain tests ───────────────────────────────────────────────────────


class TestExtractChain:
    """extract_chain returns [] for no-act, single-element for depth-1, two-element for depth-2."""

    def test_no_act_returns_empty_list(self) -> None:
        """extract_chain must return [] when act is None."""
        assert extract_chain(_claims(act=None)) == []

    def test_depth_1_returns_single_actor(self) -> None:
        """extract_chain must return ['agent-a'] for a depth-1 act claim."""
        assert extract_chain(_claims(act={"sub": "agent-a"})) == ["agent-a"]

    def test_depth_2_returns_two_actors_outermost_first(self) -> None:
        """extract_chain must return ['b', 'a'] for depth-2 act (outer='b', inner='a')."""
        assert extract_chain(_depth_2(outer="b", inner="a")) == ["b", "a"]


# ── validate_chain — depth-0 behaviour ───────────────────────────────────────


class TestValidateChainDepthZero:
    """validate_chain with an empty act chain."""

    def test_depth_0_require_non_empty_raises(self) -> None:
        """Empty chain with require_non_empty=True must raise PeerTrustError."""
        with pytest.raises(PeerTrustError):
            validate_chain(_claims(act=None), allowed_peers={_ORCH_UUID})

    def test_depth_0_require_non_empty_false_no_error(self) -> None:
        """Empty chain with require_non_empty=False must succeed silently."""
        validate_chain(
            _claims(act=None),
            allowed_peers={_ORCH_UUID},
            require_non_empty=False,
        )

    def test_depth_0_error_details_do_not_include_actor(self) -> None:
        """PeerTrustError for empty chain must not have an 'actor' key in details."""
        with pytest.raises(PeerTrustError) as exc_info:
            validate_chain(_claims(act=None), allowed_peers={_ORCH_UUID})
        assert "actor" not in exc_info.value.details


# ── validate_chain — depth-1 behaviour ───────────────────────────────────────


class TestValidateChainDepthOne:
    """validate_chain with a depth-1 act chain (the normal v4 case)."""

    def test_trusted_actor_no_error(self) -> None:
        """Depth-1 chain whose actor is in allowed_peers must succeed. (N4)"""
        validate_chain(_depth_1(_ORCH_UUID), allowed_peers={_ORCH_UUID})

    def test_untrusted_actor_raises_peer_trust_error(self) -> None:
        """Depth-1 chain with actor not in allowed_peers must raise PeerTrustError. (N7)"""
        with pytest.raises(PeerTrustError):
            validate_chain(_depth_1(_ROGUE_UUID), allowed_peers={_ORCH_UUID})

    def test_untrusted_actor_details_contains_actor(self) -> None:
        """PeerTrustError.details for untrusted actor must include the bad actor sub."""
        with pytest.raises(PeerTrustError) as exc_info:
            validate_chain(_depth_1(_ROGUE_UUID), allowed_peers={_ORCH_UUID})
        assert exc_info.value.details["actor"] == _ROGUE_UUID

    def test_untrusted_actor_details_contains_allowed_and_chain(self) -> None:
        """PeerTrustError.details must expose both 'allowed' and 'chain' fields."""
        with pytest.raises(PeerTrustError) as exc_info:
            validate_chain(_depth_1(_ROGUE_UUID), allowed_peers={_ORCH_UUID})
        details = exc_info.value.details
        assert "allowed" in details
        assert "chain" in details
        assert _ROGUE_UUID in details["chain"]

    def test_returns_none_on_success(self) -> None:
        """validate_chain must return None (not the chain list) on success."""
        result = validate_chain(_depth_1(_ORCH_UUID), allowed_peers={_ORCH_UUID})
        assert result is None


# ── validate_chain — max_depth guard ─────────────────────────────────────────


class TestValidateChainMaxDepth:
    """validate_chain enforces the max_depth limit."""

    def test_depth_2_exceeds_default_max_depth_1(self) -> None:
        """Depth-2 chain must raise PeerTrustError when max_depth=1 (v4 default)."""
        with pytest.raises(PeerTrustError):
            validate_chain(
                _depth_2("agent-b", "agent-a"),
                allowed_peers={"agent-b", "agent-a"},
                max_depth=1,
            )

    def test_depth_2_within_max_depth_2_both_trusted(self) -> None:
        """Depth-2 chain with max_depth=2 and both actors trusted must succeed."""
        validate_chain(
            _depth_2("agent-b", "agent-a"),
            allowed_peers={"agent-b", "agent-a"},
            max_depth=2,
        )

    def test_depth_2_within_max_depth_2_outer_not_trusted(self) -> None:
        """Depth-2, max_depth=2: outer actor not in allowed_peers must raise PeerTrustError."""
        with pytest.raises(PeerTrustError) as exc_info:
            validate_chain(
                _depth_2(outer=_ROGUE_UUID, inner="agent-a"),
                allowed_peers={"agent-a"},
                max_depth=2,
            )
        assert exc_info.value.details["actor"] == _ROGUE_UUID


# ── allowed_peers type compatibility ─────────────────────────────────────────


class TestAllowedPeersTypes:
    """validate_chain accepts both set and frozenset for allowed_peers."""

    def test_accepts_plain_set(self) -> None:
        """allowed_peers as a plain set must work without error."""
        validate_chain(_depth_1(_ORCH_UUID), allowed_peers={_ORCH_UUID})

    def test_accepts_frozenset(self) -> None:
        """allowed_peers as a frozenset must work without error."""
        validate_chain(_depth_1(_ORCH_UUID), allowed_peers=frozenset({_ORCH_UUID}))


# ── N4 / N7 scenario test ─────────────────────────────────────────────────────


class TestThreatModelScenarios:
    """Integration-style scenarios for N4 and N7.

    N4 — Specialist validates token-A's act.sub allowlist.
    N7 — Specialist refuses inbound A2A with act.sub not in trusted-peer set.
    """

    def test_n4_orchestrator_token_accepted_by_specialist(self) -> None:
        """N4: a token-A with act.sub=<orchestrator-agent-uuid> passes HR specialist check."""
        # HR specialist's allowlist contains exactly the orchestrator agent UUID.
        hr_trusted_peers: frozenset[str] = frozenset({_ORCH_UUID})
        token_a_claims = _depth_1(actor=_ORCH_UUID)
        # Must succeed without raising.
        validate_chain(token_a_claims, allowed_peers=hr_trusted_peers)

    def test_n7_rogue_agent_token_rejected_by_specialist(self) -> None:
        """N7: a token with act.sub=<rogue-agent-uuid> is rejected with PeerTrustError."""
        hr_trusted_peers: frozenset[str] = frozenset({_ORCH_UUID})
        rogue_claims = _depth_1(actor=_ROGUE_UUID)
        with pytest.raises(PeerTrustError) as exc_info:
            validate_chain(rogue_claims, allowed_peers=hr_trusted_peers)
        err = exc_info.value
        # error_id must match ERR-AGENT-002 from errors.py
        assert err.error_id == "ERR-AGENT-002"
        # details must name the offending actor
        assert err.details.get("actor") == _ROGUE_UUID
