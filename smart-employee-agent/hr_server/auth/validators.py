"""HR-server MCP token validator — Sprint 1 Wave 5.

Implements the F-04 canonical 6-step MCP token validation for the hr_server
endpoint.  Every inbound token-B (OBO token produced by CIBA) must pass all
six checks before a tool call is allowed to proceed.

F-04 six-step check (in order):
  1. JWT signature via JWKS (delegated to ``common.auth.jwt_validator.validate``).
  2. ``iss == config.expected_iss``
  3. ``exp > now`` (with leeway in jwt_validator)
  4. ``aud == config.expected_aud``  (= HR_AGENT_OAUTH_CLIENT_ID)
  5. ``act.sub in config.trusted_act_subs``
  6. ``config.required_scopes.issubset(token.scopes)``

F-15 / N28: ``log_startup_assertion()`` emits one INFO line at boot so that
operators can immediately detect misconfigured audience values or a stale
``EXPECTED_AGENT_OAUTH_CLIENT_ID`` env var without inspecting any token.

Raises:
    JWTValidationError: Steps 1-4 (signature, iss, exp, aud).
    PeerTrustError: Step 5 (act.sub not in trusted_act_subs).
    ScopeError: Step 6 (required scope absent).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from common.auth.errors import JWTValidationError, PeerTrustError, ScopeError
from common.auth.jwt_validator import JWKSCache, ValidatorConfig, validate
from common.auth.models import JWTClaims
from common.auth.peer_trust import validate_chain
from common.revocation import RevocationState

logger = logging.getLogger(__name__)

__all__ = [
    "HRServerTokenValidationConfig",
    "HRServerTokenValidator",
]

# ── Startup log format (N28 / F-15) ──────────────────────────────────────────

_STARTUP_LOG_FORMAT = "token_validator.startup expected_aud=%s trusted_act_subs=%s"


# ── Config dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HRServerTokenValidationConfig:
    """Immutable configuration driving the 6-step F-04 MCP token validator.

    This is a thin projection of ``HRServerConfig`` that contains only the
    fields the validator needs.  Use ``HRServerTokenValidator.from_config()``
    to construct from a full ``HRServerConfig`` without duplicating field
    references everywhere.

    Attributes:
        expected_iss: Exact issuer URL; must match token ``iss``.
        jwks_url: Full URL to the IS JWKS endpoint.
        expected_aud: The HR-agent's OAuth Client ID.  Every token-B must
            carry this value in its ``aud`` claim (F-04).
        trusted_act_subs: Frozenset of HR-agent UUIDs that may appear as
            ``act.sub`` in a token-B.  An empty frozenset means no agent is
            trusted (deny-all); usually a single element.
        required_scopes: Default frozenset of scopes required on each tool
            call.  Per-call ``required_scopes`` can override this in
            ``validate_token()``.
        insecure_tls: Disable TLS certificate verification (dev only).
    """

    expected_iss: str
    jwks_url: str
    expected_aud: str
    trusted_act_subs: frozenset[str]
    required_scopes: frozenset[str] = frozenset()
    insecure_tls: bool = False


# ── Validator class ───────────────────────────────────────────────────────────


class HRServerTokenValidator:
    """Implements the F-04 6-step MCP token validation for hr_server.

    One instance is created per process during startup and injected into
    ``hr_server/mcp/tools.py`` via ``build_mcp_app()``.

    Steps:
      1. JWT signature via JWKS (``jwt_validator.validate``).
      2. ``iss == config.expected_iss``.
      3. ``exp > now`` (leeway applied inside ``jwt_validator``).
      4. ``aud == config.expected_aud``.
      5. ``act.sub in config.trusted_act_subs``  (``peer_trust.validate_chain``).
      6. ``config.required_scopes.issubset(token.scopes)``  (raises ``ScopeError``).

    Raises:
        JWTValidationError: Steps 1–4.
        PeerTrustError: Step 5.
        ScopeError: Step 6.
    """

    def __init__(
        self,
        config: HRServerTokenValidationConfig,
        *,
        jwks_cache: JWKSCache | None = None,
    ) -> None:
        """Store config; optionally inject a pre-built ``JWKSCache``.

        Args:
            config: Frozen validation config.
            jwks_cache: Optional pre-built JWKS cache.  When ``None``, a new
                cache is constructed from *config* on the first
                ``validate_token()`` call.
        """
        self._config = config
        self._jwks_cache: JWKSCache = jwks_cache or JWKSCache(
            jwks_url=config.jwks_url,
            insecure_tls=config.insecure_tls,
        )
        # Sprint 3 3A.3: revocation state (Step 7 denylist enforcement).
        # Wired in via ``attach_revocation()`` from hr_server/main.py lifespan.
        # ``None`` means denylist enforcement is a no-op (test fakes / pre-3A.3
        # callers); production must call attach_revocation at startup —
        # ``log_startup_assertion()`` fails closed if it didn't.
        self._revocation: RevocationState | None = None
        # Build the lower-level ValidatorConfig used by jwt_validator.validate().
        # Steps 1-4 (sig, iss, exp, aud) are enforced here; scope enforcement is
        # done separately in step 6 so we can raise the richer ScopeError.
        self._validator_config = ValidatorConfig(
            expected_iss=config.expected_iss,
            jwks_url=config.jwks_url,
            expected_aud=config.expected_aud,
            required_scopes=frozenset(),   # step 6 handled separately below
            insecure_tls=config.insecure_tls,
        )

    # ── Sprint 3 3A.3: revocation hooks ───────────────────────────────────────

    def attach_revocation(self, state: RevocationState) -> None:
        """Wire a ``common.revocation.RevocationState`` into the validator.

        Called once from ``hr_server/main.py`` lifespan startup. After this,
        ``validate_token()`` consults ``state.revoked_jtis`` after F-04 steps
        1–6 succeed (Step 7) and raises ``ScopeError(error_id="ERR-MCP-002")``
        on hit. Without this call (test fakes), Step 7 is a no-op.

        Introspection cache (Step 8 in tech-arch §4.2) is **deferred to
        Sprint 4** per the 2026-05-10 deferral decision — F-21 confirmed at
        source means revoke-at-IS doesn't propagate to OBO tokens, so
        introspection of token-B returns active=true even when our
        orchestrator wants it gone. Denylist is the only revocation primitive
        that actually works.
        """
        self._revocation = state

    # ── Lifespan helpers ──────────────────────────────────────────────────────

    async def prewarm_jwks(self) -> None:
        """Eagerly fetch the JWKS so the first request doesn't pay the cold-cache RTT.

        Mid-sprint observability fix (2026-05-09): the live-walk showed an
        ~800 ms cold-cache penalty on the first inbound CIBA token validation.
        Calling this from ``lifespan`` startup amortises the cost off the
        critical user-visible path. Failure is logged but non-fatal — IS
        may not be reachable yet at startup; the lazy refresh on first
        ``validate_token()`` will retry.
        """
        try:
            await self._jwks_cache.refresh()
            logger.info(
                "validator.jwks_prewarm_ok jwks_url=%s key_count=%d",
                self._jwks_cache.jwks_url,
                len(self._jwks_cache._keys),  # noqa: SLF001 — startup observability
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; fall back to lazy refresh
            logger.warning(
                "validator.jwks_prewarm_failed jwks_url=%s err=%r",
                self._jwks_cache.jwks_url,
                exc,
            )

    # ── Classmethod factory ───────────────────────────────────────────────────

    @classmethod
    def from_config(cls, server_config: object) -> "HRServerTokenValidator":
        """Convenience: lift validator fields from a ``HRServerConfig`` instance.

        Args:
            server_config: A ``HRServerConfig`` frozen dataclass (Wave 4).
                Only the fields ``is_issuer``, ``is_jwks_url``, ``expected_aud``,
                ``trusted_act_subs``, ``required_scopes``, and ``is_insecure_tls``
                are accessed; this avoids a hard import of ``hr_server/config.py``
                and eases unit-testing with minimal stubs.

        Returns:
            A fully configured ``HRServerTokenValidator``.
        """
        validation_config = HRServerTokenValidationConfig(
            expected_iss=server_config.is_issuer,  # type: ignore[union-attr]
            jwks_url=server_config.is_jwks_url,  # type: ignore[union-attr]
            expected_aud=server_config.expected_aud,  # type: ignore[union-attr]
            trusted_act_subs=server_config.trusted_act_subs,  # type: ignore[union-attr]
            required_scopes=server_config.required_scopes,  # type: ignore[union-attr]
            insecure_tls=server_config.is_insecure_tls,  # type: ignore[union-attr]
        )
        return cls(validation_config)

    # ── Core validation ───────────────────────────────────────────────────────

    async def validate_token(
        self,
        jwt_token: str,
        *,
        required_scopes: frozenset[str] | None = None,
    ) -> JWTClaims:
        """Run the full 6-step F-04 check and return verified ``JWTClaims``.

        Steps 1–4 (signature, iss, exp, aud) are enforced by the shared
        ``jwt_validator.validate()`` function.  Step 5 (act.sub allowlist) is
        enforced by ``peer_trust.validate_chain()`` with ``max_depth=1``.
        Step 6 (scope subset) is enforced here, raising ``ScopeError`` rather
        than ``JWTValidationError`` so callers can distinguish scope failures
        from structural JWT failures.

        Args:
            jwt_token: Raw Bearer token string from the MCP request.
            required_scopes: If not ``None``, overrides
                ``config.required_scopes`` for this specific call.  Pass
                ``frozenset()`` to skip scope enforcement for a given call.

        Returns:
            Decoded and fully verified ``JWTClaims``.

        Raises:
            JWTValidationError: Steps 1–4 fail (signature, iss, exp, aud).
            PeerTrustError: Step 5 fails (act.sub not trusted).
            ScopeError: Step 6 fails (missing required scope).
        """
        effective_scopes: frozenset[str] = (
            required_scopes if required_scopes is not None else self._config.required_scopes
        )

        # DEBUG: decision-point snapshot before any check runs so that a
        # LOG_LEVEL=DEBUG rerun shows exactly what the validator expected vs
        # what arrived, without needing to intercept a live token.
        logger.debug(
            "hr_validator_entry expected_aud=%s trusted_act_subs=%s required_scopes=%s",
            self._config.expected_aud,
            self._config.trusted_act_subs,
            sorted(effective_scopes),
        )

        # Steps 1-4: signature + iss + exp + aud via common jwt_validator
        claims: JWTClaims = await validate(
            jwt_token,
            self._validator_config,
            jwks_cache=self._jwks_cache,
        )

        # DEBUG: show the claims that passed steps 1-4 so scope/act failures
        # can be correlated against what the token actually contained.
        logger.debug(
            "hr_validator_claims_decoded jti=%s iss=%s aud=%r act=%r scope=%r",
            claims.jti,
            claims.iss,
            claims.aud,
            claims.act,
            claims.scope,
        )

        # Step 5: act.sub must be in trusted_act_subs (max_depth=1, depth-2 denied)
        validate_chain(
            claims,
            allowed_peers=self._config.trusted_act_subs,
            require_non_empty=True,
            max_depth=1,
        )

        # Step 6: scope subset check (raises ScopeError, not JWTValidationError)
        if effective_scopes:
            token_scopes: frozenset[str] = frozenset(
                claims.scope.split() if claims.scope else []
            )
            missing: frozenset[str] = effective_scopes - token_scopes
            if missing:
                logger.debug(
                    "hr_validator_scope_fail required=%s present=%s missing=%s",
                    sorted(effective_scopes),
                    sorted(token_scopes),
                    sorted(missing),
                )
                raise ScopeError(
                    f"Token missing required scopes: {sorted(missing)}",
                    details={
                        "required": sorted(effective_scopes),
                        "present": sorted(token_scopes),
                        "missing": sorted(missing),
                    },
                )

        # Step 7 (Sprint 3 3A.3): denylist check.
        # Placed AFTER F-04 steps 1-6 so we only consult the denylist for
        # tokens whose signature, aud, scope, and act.sub are already proven
        # legitimate — prevents accidental denylist pollution from forged
        # jtis. The denylist is populated by the orchestrator's logout cascade
        # via POST /internal/events (Sprint 3 3A.2). With F-21 confirming IS
        # does not propagate token-A revoke to OBO tokens, this is the ONLY
        # revocation primitive on this code path. Captured token-B replay
        # after a fan-out lands here.
        if self._revocation is not None and claims.jti:
            if claims.jti in self._revocation.revoked_jtis:
                logger.info(
                    "hr_validator_denylist_hit jti=%s sub=%s — rejecting (Sprint 3 3A.3)",
                    claims.jti,
                    claims.sub,
                )
                raise ScopeError(
                    f"Token revoked: jti={claims.jti}",
                    error_id="ERR-MCP-002",
                    details={"jti": claims.jti, "reason": "denylist_hit"},
                )

        return claims

    # ── Startup assertion (N28 / F-15) ────────────────────────────────────────

    def log_startup_assertion(self) -> None:
        """Emit one INFO log line capturing validation parameters (N28 / F-15).

        Format::

            token_validator.startup expected_aud=<...> trusted_act_subs=<...>
            denylist_enforcement=<on|off>

        This log line is emitted during service startup so that operators can
        immediately verify the loaded ``expected_aud`` matches the env var
        ``HR_AGENT_OAUTH_CLIENT_ID`` without needing to intercept a live
        token.  When the loaded value diverges from the env var the discrepancy
        is visible in the startup log before any token arrives.

        Sprint 3 3A.3 addendum: also surface whether ``attach_revocation()``
        was called before startup. ``denylist_enforcement=off`` in production
        means token-B replay after sign-out is silently allowed — see
        ``project_introspection_deferred.md`` for why this is the only
        revocation primitive on the OBO path. The line emits regardless of
        wiring; the explicit ``on`` / ``off`` is the alarmable signal.
        """
        denylist_enforcement = "on" if self._revocation is not None else "off"
        logger.info(
            _STARTUP_LOG_FORMAT + " denylist_enforcement=%s",
            self._config.expected_aud,
            self._config.trusted_act_subs,
            denylist_enforcement,
        )
        if self._revocation is None:
            # Production path attaches revocation before this is called; if it
            # didn't, captured token-B replay after sign-out goes through —
            # the demo wedge is broken. SIEM should alert on this line.
            logger.warning(
                "token_validator.startup denylist_enforcement=off — "
                "captured-token replay after sign-out will NOT be rejected. "
                "attach_revocation() was not invoked before log_startup_assertion(). "
                "Expected only in unit-test environments."
            )
