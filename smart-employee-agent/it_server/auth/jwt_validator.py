"""IT Server JWT validator — Sprint 0 stub.

Mirrors hr_server/auth/jwt_validator.py in shape. Sprint 1 implementation
delegates to common.auth.jwt_validator.validate() with IT-specific config:
- aud: exact match against config.EXPECTED_AUD (verified by P14)
- scope: it_assets_read_mcp
- nested act allowlist: config.TRUSTED_PEER_AGENTS (it_agent + orchestrator-agent)
"""
import logging

logger = logging.getLogger(__name__)


class TokenError(Exception):
    """Raised when token validation fails."""


class JWTValidator:
    """Sprint 0 placeholder. Sprint 1 wires through common/auth/."""

    def __init__(self, jwks_url: str, issuer: str, expected_aud: str):
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.expected_aud = expected_aud

    def validate(self, token: str) -> dict:
        raise NotImplementedError(
            "it_server.auth.jwt_validator.JWTValidator.validate — "
            "Sprint 1 wires this through common/auth/jwt_validator.validate()"
        )
