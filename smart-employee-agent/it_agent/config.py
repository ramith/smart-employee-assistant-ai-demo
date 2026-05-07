"""IT-agent service configuration.

Single source of truth for all environment variables consumed by it_agent.
Uses a frozen dataclass per F-09.  Structural mirror of ``hr_agent/config.py``
with ``IT_`` prefixed variables and it-specific defaults (port 8002, it_assets_read_rest scope).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from common.auth.actor_token_provider import AgentCredentials
from common.auth.wso2_is_client import WSO2ISClientConfig

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"^https?://[^/]+(:\d+)?$")


def _require(environ: dict[str, str], name: str) -> str:
    """Return the value of *name* from *environ* or raise ``ValueError``."""
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing env var: {name}")
    return value


def _validate_url(value: str, name: str) -> str:
    """Raise ``ValueError`` if *value* is not a bare ``scheme://host[:port]`` URL."""
    if not _URL_RE.match(value):
        raise ValueError(
            f"Invalid URL for {name}={value!r} — must match scheme://host[:port] "
            "(no trailing slash, no path segment)"
        )
    return value


def _parse_bool(value: str) -> bool:
    """Return ``True`` if *value* is 'true', '1', or 'yes' (case-insensitive)."""
    return value.strip().lower() in {"true", "1", "yes"}


def _parse_frozenset(value: str) -> frozenset[str]:
    """Split a comma-separated string into a frozenset of stripped non-empty strings."""
    return frozenset(item.strip() for item in value.split(",") if item.strip())


def _parse_port(value: str, name: str) -> int:
    """Parse *value* as a TCP port number or raise ``ValueError``."""
    try:
        port = int(value)
    except ValueError:
        raise ValueError(f"Invalid integer for {name}={value!r}") from None
    if not (1 <= port <= 65535):
        raise ValueError(f"Port out of range for {name}={value!r}")
    return port


# ── Dataclass ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ITAgentConfig:
    """Immutable configuration for the it_agent service.

    Attributes:
        is_base_url: WSO2 IS root URL (no trailing slash).
        is_insecure_tls: Disable TLS verification (dev only).
        is_issuer: Token issuer claim value.
        is_jwks_url: JWKS endpoint URL.
        agent: 4-value agent identity (AgentCredentials) for App-Native Auth /
            actor_token minting.
        it_server_url: Base URL of the it_server MCP backend.
        trusted_orchestrator_subs: Frozenset of orchestrator agent UUIDs that
            are permitted to call this specialist via A2A (act.sub allowlist).
        expected_inbound_aud: The audience value expected in inbound token-A
            (typically the orchestrator MCP client_id).
        ciba_scope: OAuth scope requested on CIBA initiation.
        canonical_url: Public A2A endpoint URL for this agent (agent-card URL).
        host: Bind address.
        port: Bind port.
        max_poll_seconds: Maximum CIBA polling budget.
    """

    # IS connectivity
    is_base_url: str
    is_insecure_tls: bool
    is_issuer: str
    is_jwks_url: str

    # Agent identity (4-value tuple, drives App-Native Auth)
    agent: AgentCredentials

    # MCP backend
    it_server_url: str

    # Inbound A2A trust
    trusted_orchestrator_subs: frozenset[str]
    expected_inbound_aud: str  # orchestrator MCP client_id (aud on token-A)

    # Server bind
    host: str = "0.0.0.0"
    port: int = 8002

    # CIBA
    ciba_scope: str = "openid it_assets_read_rest"
    max_poll_seconds: int = 240

    # Self-referential canonical URL (agent-card)
    canonical_url: str = "http://it_agent:8002/a2a"

    # ── Convenience factories ──────────────────────────────────────────────────

    def is_client_config(self) -> WSO2ISClientConfig:
        """Return a ``WSO2ISClientConfig`` wired from this settings instance."""
        return WSO2ISClientConfig(
            base_url=self.is_base_url,
            insecure_tls=self.is_insecure_tls,
        )

    # ── Constructor ────────────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "ITAgentConfig":
        """Read environment variables and return a validated, frozen config instance.

        Args:
            environ: Mapping of env vars.  Defaults to ``os.environ`` when ``None``.

        Returns:
            A fully validated :class:`ITAgentConfig`.

        Raises:
            ValueError: If a required variable is missing, a URL is malformed,
                or a port is non-integer.
        """
        env = environ if environ is not None else dict(os.environ)

        # IS connectivity
        is_base_url = _validate_url(_require(env, "WSO2_IS_BASE_URL"), "WSO2_IS_BASE_URL")
        is_insecure_tls = _parse_bool(env.get("IDP_INSECURE_TLS", "false"))
        is_issuer = env.get("WSO2_IS_ISSUER", "").strip() or f"{is_base_url}/oauth2/token"
        is_jwks_url = env.get("WSO2_IS_JWKS_URL", "").strip() or f"{is_base_url}/oauth2/jwks"

        # Agent identity (4-value tuple)
        agent_id = _require(env, "IT_AGENT_ID")
        agent_secret = _require(env, "IT_AGENT_SECRET")
        agent_oauth_client_id = _require(env, "IT_AGENT_OAUTH_CLIENT_ID")
        agent_oauth_client_secret = _require(env, "IT_AGENT_OAUTH_CLIENT_SECRET")
        redirect_uri = env.get(
            "IT_AGENT_REDIRECT_URI", "http://localhost:9999/agent-callback"
        ).strip()
        agent = AgentCredentials(
            agent_id=agent_id,
            agent_secret=agent_secret,
            oauth_client_id=agent_oauth_client_id,
            oauth_client_secret=agent_oauth_client_secret,
            redirect_uri=redirect_uri,
        )

        # MCP backend URL
        it_server_url = _validate_url(_require(env, "IT_MCP_SERVER_URL"), "IT_MCP_SERVER_URL")

        # Inbound A2A trust
        trusted_orchestrator_subs = _parse_frozenset(
            env.get("IT_TRUSTED_PEER_AGENTS", "")
        )
        expected_inbound_aud = _require(env, "IT_EXPECTED_INBOUND_AUD")

        # Server bind
        host = env.get("IT_AGENT_HOST", "0.0.0.0").strip()
        port = _parse_port(env.get("IT_AGENT_PORT", "8002"), "IT_AGENT_PORT")

        # CIBA
        ciba_scope = env.get("IT_CIBA_SCOPE", "openid it_assets_read_rest").strip()
        max_poll_seconds_raw = env.get("IT_MAX_POLL_SECONDS", "240")
        try:
            max_poll_seconds = int(max_poll_seconds_raw)
        except ValueError:
            raise ValueError(
                f"Invalid integer for IT_MAX_POLL_SECONDS={max_poll_seconds_raw!r}"
            ) from None

        # Canonical URL
        canonical_url = env.get("IT_AGENT_CANONICAL_URL", "http://it_agent:8002/a2a").strip()

        logger.info(
            "it_agent_config_loaded | is_base_url=%s it_server_url=%s port=%d",
            is_base_url,
            it_server_url,
            port,
        )

        return cls(
            is_base_url=is_base_url,
            is_insecure_tls=is_insecure_tls,
            is_issuer=is_issuer,
            is_jwks_url=is_jwks_url,
            agent=agent,
            it_server_url=it_server_url,
            trusted_orchestrator_subs=trusted_orchestrator_subs,
            expected_inbound_aud=expected_inbound_aud,
            host=host,
            port=port,
            ciba_scope=ciba_scope,
            max_poll_seconds=max_poll_seconds,
            canonical_url=canonical_url,
        )


def load() -> ITAgentConfig:
    """Module-level factory: read ``os.environ`` and return a validated config.

    Raises:
        ValueError: Propagated from :meth:`ITAgentConfig.from_env`.
    """
    return ITAgentConfig.from_env()
