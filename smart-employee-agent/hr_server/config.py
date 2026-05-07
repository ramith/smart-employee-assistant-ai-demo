"""HR-server service configuration.

Single source of truth for all environment variables consumed by hr_server.
Replaces the Sprint 0 module-level globals stub with a frozen dataclass per F-09.

F-04: ``expected_aud`` is the HR-agent's OAuth Client ID; the validator enforces
``aud == expected_aud`` on every inbound token-B.

F-15 / N28: ``expected_aud`` is logged at INFO level during startup so operators
can immediately detect misconfigured audience values without inspecting the token.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

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
class HRServerConfig:
    """Immutable configuration for the hr_server MCP service.

    Attributes:
        is_base_url: WSO2 IS root URL (no trailing slash).
        is_insecure_tls: Disable TLS verification (dev only).
        is_issuer: Expected ``iss`` claim value in inbound tokens.
        is_jwks_url: JWKS endpoint for token-B signature verification.
        expected_aud: The HR-agent's OAuth Client ID — every inbound token-B must
            carry this value as ``aud`` (F-04).  Logged at INFO on startup (N28).
        trusted_act_subs: Frozenset of HR-agent UUIDs that may appear as ``act.sub``
            in token-B (F-04).  Usually a single element; supports multi-agent
            shared-MCP scenarios.
        required_scopes: Default frozenset of scopes required on every tool call.
        allowed_origins: CORS allowed origins.
        host: Bind address.
        port: Bind port.
        introspect_enabled: Whether token introspection is enabled (Sprint 2 hook).
        introspect_url: IS introspection endpoint (Sprint 2 hook; may be empty).
    """

    # IS connectivity
    is_base_url: str
    is_insecure_tls: bool
    is_issuer: str
    is_jwks_url: str

    # F-04 / N28 — token-B audience + act.sub enforcement
    expected_aud: str
    trusted_act_subs: frozenset[str]
    required_scopes: frozenset[str]

    # CORS
    allowed_origins: frozenset[str]

    # Server bind
    host: str = "0.0.0.0"
    port: int = 8000

    # Sprint 2 hooks (read now, not enforced until Sprint 2)
    introspect_enabled: bool = True
    introspect_url: str = ""

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
    ) -> "HRServerConfig":
        """Read environment variables and return a validated, frozen config instance.

        Logs ``expected_aud`` and ``trusted_act_subs`` at INFO on success so that
        N28 startup verification is captured in the service log without any extra
        instrumentation in the caller.

        Args:
            environ: Mapping of env vars.  Defaults to ``os.environ`` when ``None``.

        Returns:
            A fully validated :class:`HRServerConfig`.

        Raises:
            ValueError: If a required variable is missing, a URL is malformed,
                or a port is non-integer.
        """
        env = environ if environ is not None else dict(os.environ)

        # IS connectivity
        is_base_url = _validate_url(_require(env, "WSO2_IS_BASE_URL"), "WSO2_IS_BASE_URL")
        is_insecure_tls = _parse_bool(env.get("DISABLE_SSL_VERIFY", "false"))
        is_issuer = env.get("AUTH_ISSUER", "").strip() or f"{is_base_url}/oauth2/token"
        is_jwks_url = env.get("JWKS_URL", "").strip() or f"{is_base_url}/oauth2/jwks"

        # F-04 audience + trust
        expected_aud = _require(env, "HR_SERVER_EXPECTED_AUD")
        trusted_act_subs = _parse_frozenset(env.get("HR_SERVER_TRUSTED_PEER_AGENTS", ""))
        required_scopes = _parse_frozenset(env.get("HR_SERVER_REQUIRED_SCOPES", "hr_self_rest"))

        # CORS
        allowed_origins = _parse_frozenset(
            env.get("ALLOWED_ORIGINS", "http://localhost:3001,http://127.0.0.1:3001")
        )

        # Server bind
        host = env.get("HR_SERVER_HOST", "0.0.0.0").strip()
        port = _parse_port(env.get("HR_SERVER_PORT", "8000"), "HR_SERVER_PORT")

        # Sprint 2 hooks
        introspect_enabled = _parse_bool(
            env.get("HR_SERVER_INTROSPECT_ENABLED", "true")
        )
        introspect_url = env.get("WSO2_IS_INTROSPECT_URL", "").strip()

        # N28 startup log — expected_aud MUST appear here for detectability
        logger.info(
            "[hr_server] token enforcement active | expected_aud=%s trusted_act_subs=%s "
            "required_scopes=%s port=%d",
            expected_aud,
            trusted_act_subs,
            required_scopes,
            port,
        )

        return cls(
            is_base_url=is_base_url,
            is_insecure_tls=is_insecure_tls,
            is_issuer=is_issuer,
            is_jwks_url=is_jwks_url,
            expected_aud=expected_aud,
            trusted_act_subs=trusted_act_subs,
            required_scopes=required_scopes,
            allowed_origins=allowed_origins,
            host=host,
            port=port,
            introspect_enabled=introspect_enabled,
            introspect_url=introspect_url,
        )


def load() -> HRServerConfig:
    """Module-level factory: read ``os.environ`` and return a validated config.

    Raises:
        ValueError: Propagated from :meth:`HRServerConfig.from_env`.
    """
    return HRServerConfig.from_env()
