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
        # Steps 1-4: signature + iss + exp + aud via common jwt_validator
        claims: JWTClaims = await validate(
            jwt_token,
            self._validator_config,
            jwks_cache=self._jwks_cache,
        )

        # Step 5: act.sub must be in trusted_act_subs (max_depth=1, depth-2 denied)
        validate_chain(
            claims,
            allowed_peers=self._config.trusted_act_subs,
            require_non_empty=True,
            max_depth=1,
        )

        # Step 6: scope subset check (raises ScopeError, not JWTValidationError)
        effective_scopes: frozenset[str] = (
            required_scopes if required_scopes is not None else self._config.required_scopes
        )
        if effective_scopes:
            token_scopes: frozenset[str] = frozenset(
                claims.scope.split() if claims.scope else []
            )
            missing: frozenset[str] = effective_scopes - token_scopes
            if missing:
                raise ScopeError(
                    f"Token missing required scopes: {sorted(missing)}",
                    details={
                        "required": sorted(effective_scopes),
                        "present": sorted(token_scopes),
                        "missing": sorted(missing),
                    },
                )

        return claims

    # ── Startup assertion (N28 / F-15) ────────────────────────────────────────

    def log_startup_assertion(self) -> None:
        """Emit one INFO log line capturing validation parameters (N28 / F-15).

        Format::

            token_validator.startup expected_aud=<...> trusted_act_subs=<...>

        This log line is emitted during service startup so that operators can
        immediately verify the loaded ``expected_aud`` matches the env var
        ``HR_AGENT_OAUTH_CLIENT_ID`` without needing to intercept a live
        token.  When the loaded value diverges from the env var the discrepancy
        is visible in the startup log before any token arrives.
        """
        logger.info(
            _STARTUP_LOG_FORMAT,
            self._config.expected_aud,
            self._config.trusted_act_subs,
        )
