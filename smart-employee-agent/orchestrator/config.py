"""Orchestrator service configuration.

Single source of truth for all environment variables consumed by the orchestrator.
Uses a frozen dataclass (not Pydantic) per F-09: no asyncio types cross this boundary,
but keeping config as a plain dataclass avoids the Pydantic dependency at the config
layer and is consistent across all five services.

F-15: ``from_env()`` validates that hr_agent_oauth_client_id, it_agent_oauth_client_id,
and orchestrator_agent_oauth_client_id are all distinct at startup.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

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


def _parse_float(value: str, name: str, *, minimum: float = 0.0) -> float:
    """Parse *value* as a float >= *minimum* or raise ``ValueError``."""
    try:
        out = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid float for {name}={value!r}") from None
    if out < minimum:
        raise ValueError(f"{name}={value!r} must be >= {minimum}")
    return out


def _parse_positive_int(value: str, name: str) -> int:
    """Parse *value* as an int >= 1 or raise ``ValueError``."""
    try:
        out = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid integer for {name}={value!r}") from None
    if out < 1:
        raise ValueError(f"{name}={value!r} must be >= 1")
    return out


# ── Dataclass ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    """Immutable configuration for the orchestrator service.

    Attributes:
        is_base_url: WSO2 IS root URL (no trailing slash).
        is_insecure_tls: Disable TLS verification (dev only).
        is_issuer: Token issuer claim value (typically ``{is_base_url}/oauth2/token``).
        is_jwks_url: JWKS endpoint URL.
        mcp_client_id: Confidential MCP-client client_id (used for both
            ``/authorize`` redirect AND ``/token`` code exchange — IS
            rejects cross-client code redemption). Sprint 3 3B.3 dropped
            the legacy ``spa_client_id`` field and the
            ``ORCHESTRATOR_APP_CLIENT_ID`` env var; both were vestigial v3
            dual-client cruft. See memory
            ``project_orchestrator_app_vestigial.md``.
        mcp_client_secret: Corresponding secret.
        mcp_redirect_uri: Registered redirect URI for the MCP client.
        orchestrator_agent: 4-value agent identity for actor_token in Pattern C.
        hr_agent_url: Base URL of the hr_agent service.
        it_agent_url: Base URL of the it_agent service.
        hr_agent_oauth_client_id: HR agent OAuth client_id — F-15 collision check.
        it_agent_oauth_client_id: IT agent OAuth client_id — F-15 collision check.
        trusted_specialist_subs: Frozenset of specialist agent UUIDs (inbound A2A
            callback validation).
        allowed_origins: CORS allowed origins.
        host: Bind address.
        port: Bind port.
        session_cookie_name: Name of the browser session cookie.
        session_ttl_seconds: Session time-to-live in seconds.
        llm_fallback_mode: Routing mode — ``"keyword"`` (default) or ``"llm"``.
            In ``"llm"`` mode the orchestrator routes + composes chat replies via
            Gemini, with the keyword router / ``_render_result`` as the automatic
            fallback; if the key is missing it degrades to keyword-only.
        gemini_api_key: Gemini API key (only consulted when ``llm_fallback_mode="llm"``).
        gemini_model: Gemini model id (default ``"gemini-2.5-flash"``).
        llm_timeout_s: Per-LLM-call hard timeout in seconds (default ``8.0``);
            on timeout the orchestrator falls back.
        llm_max_output_tokens: Cap on Gemini output tokens per call (default ``512``).
        cookie_secure: Set Secure flag on session cookie (False in dev).
    """

    # IS connectivity
    is_base_url: str
    is_insecure_tls: bool
    is_issuer: str
    is_jwks_url: str

    # Confidential MCP backend client (code exchange / Pattern C +
    # /authorize redirect — same client_id required on both per IS).
    mcp_client_id: str
    mcp_client_secret: str
    mcp_redirect_uri: str

    # Orchestrator agent identity (actor_token in Pattern C)
    orchestrator_agent: AgentCredentials

    # Specialist service URLs
    hr_agent_url: str
    it_agent_url: str

    # F-15: specialist OAuth client IDs for collision detection
    hr_agent_oauth_client_id: str
    it_agent_oauth_client_id: str

    # Trusted inbound specialist UUIDs (act.sub allowlist for callbacks)
    trusted_specialist_subs: frozenset[str]

    # CORS
    allowed_origins: frozenset[str]

    # Server bind
    host: str = "0.0.0.0"
    port: int = 8080

    # Session
    session_cookie_name: str = "orch_sid"
    session_ttl_seconds: int = 28800

    # LLM (F-14: default is keyword)
    llm_fallback_mode: str = "keyword"

    # 3A.2: MCP server URLs (for the 4-receiver fan-out per Q6).
    # Defaults match compose internal DNS; override via env.
    hr_server_url: str = "http://hr_server:8000"
    it_server_url: str = "http://it_server:8004"

    # 3A.2: shared secret for /internal/events fan-out auth (BLOCK-B simple).
    # Empty string disables fan-out (test mode); production must set non-empty.
    internal_revoke_shared_secret: str = ""

    # 3A.2.2 (live-walk fix 2026-05-09): explicit post_logout_redirect_uri sent
    # to IS RP-initiated logout. WSO2 IS requires this URL to be EXACTLY
    # registered on orchestrator-mcp-client's Callback URLs (no query string,
    # no fragment). Default points at the orchestrator's own port 8090 since
    # the SPA is now served from `orchestrator/main.py` StaticFiles mount;
    # the legacy `client:3001` container is no longer the SPA host.
    post_logout_redirect_uri: str = "http://localhost:8090/"
    gemini_api_key: str | None = None
    # S5 — Gemini routing/composer knobs (only consulted when
    # llm_fallback_mode == "llm" and gemini_api_key is set).
    gemini_model: str = "gemini-2.5-flash"
    llm_timeout_s: float = 8.0
    llm_max_output_tokens: int = 512

    # Cookie (F-06)
    cookie_secure: bool = False

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
    ) -> "OrchestratorConfig":
        """Read environment variables and return a validated, frozen config instance.

        Args:
            environ: Mapping of env vars.  Defaults to ``os.environ`` when ``None``.

        Returns:
            A fully validated :class:`OrchestratorConfig`.

        Raises:
            ValueError: If a required variable is missing, a URL is malformed,
                a port is non-integer, or the F-15 OAuth-client-ID collision check
                fails.
        """
        env = environ if environ is not None else dict(os.environ)

        # IS connectivity
        is_base_url = _validate_url(_require(env, "WSO2_IS_BASE_URL"), "WSO2_IS_BASE_URL")
        is_insecure_tls = _parse_bool(env.get("IDP_INSECURE_TLS", "false"))
        is_issuer = env.get("WSO2_IS_ISSUER", "").strip() or f"{is_base_url}/oauth2/token"
        is_jwks_url = env.get("WSO2_IS_JWKS_URL", "").strip() or f"{is_base_url}/oauth2/jwks"

        # Confidential MCP backend client (also drives /authorize redirect
        # — same client_id on /authorize and /token per IS requirement).
        mcp_client_id = _require(env, "ORCHESTRATOR_MCP_CLIENT_ID")
        mcp_client_secret = _require(env, "ORCHESTRATOR_MCP_CLIENT_SECRET")
        mcp_redirect_uri = env.get(
            "ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI",
            "http://localhost:8090/agent-callback",
        ).strip()

        # Orchestrator agent identity (4-value tuple)
        agent_id = _require(env, "ORCHESTRATOR_AGENT_ID")
        agent_secret = _require(env, "ORCHESTRATOR_AGENT_SECRET")
        agent_oauth_client_id = _require(env, "ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID")
        agent_oauth_client_secret = _require(env, "ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET")
        orchestrator_agent = AgentCredentials(
            agent_id=agent_id,
            agent_secret=agent_secret,
            oauth_client_id=agent_oauth_client_id,
            oauth_client_secret=agent_oauth_client_secret,
            redirect_uri=mcp_redirect_uri,
        )

        # Specialist endpoints
        hr_agent_url = _validate_url(_require(env, "HR_AGENT_URL"), "HR_AGENT_URL")
        it_agent_url = _validate_url(_require(env, "IT_AGENT_URL"), "IT_AGENT_URL")

        # 3A.2: MCP server URLs for the fan-out (defaults match compose internal DNS).
        hr_server_url = env.get("HR_SERVER_URL", "http://hr_server:8000").strip()
        it_server_url = env.get("IT_SERVER_URL", "http://it_server:8004").strip()

        # 3A.2: fan-out shared secret. Empty string -> fan-out disabled (test fallback).
        internal_revoke_shared_secret = env.get(
            "INTERNAL_REVOKE_SHARED_SECRET", ""
        ).strip()

        # 3A.2.2: post_logout_redirect_uri (must be registered in IS Console).
        # Default = orchestrator's own URL since the SPA is served from there.
        post_logout_redirect_uri = env.get(
            "POST_LOGOUT_REDIRECT_URI", "http://localhost:8090/"
        ).strip()

        # F-15: specialist OAuth client IDs
        hr_agent_oauth_client_id = _require(env, "HR_AGENT_OAUTH_CLIENT_ID")
        it_agent_oauth_client_id = _require(env, "IT_AGENT_OAUTH_CLIENT_ID")

        # F-15 collision check — all three agent OAuth client IDs must be distinct
        all_ids = {
            agent_oauth_client_id,
            hr_agent_oauth_client_id,
            it_agent_oauth_client_id,
        }
        if len(all_ids) != 3:
            raise ValueError(
                "F-15 OAuth Client ID collision detected: ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID, "
                "HR_AGENT_OAUTH_CLIENT_ID, and IT_AGENT_OAUTH_CLIENT_ID must all be distinct. "
                f"Got: {agent_oauth_client_id!r}, {hr_agent_oauth_client_id!r}, "
                f"{it_agent_oauth_client_id!r}"
            )

        # Trust
        trusted_specialist_subs = _parse_frozenset(
            env.get("TRUSTED_SPECIALIST_SUBS", "")
        )

        # CORS
        allowed_origins = _parse_frozenset(
            env.get("ALLOWED_ORIGINS", "http://localhost:3001,http://127.0.0.1:3001")
        )

        # Server bind
        host = env.get("ORCHESTRATOR_HOST", "0.0.0.0").strip()
        port = _parse_port(env.get("ORCHESTRATOR_PORT", "8080"), "ORCHESTRATOR_PORT")

        # Session
        session_cookie_name = env.get("SESSION_COOKIE_NAME", "orch_sid").strip()
        session_ttl_seconds = _parse_port(
            env.get("SESSION_TTL_SECONDS", "28800"), "SESSION_TTL_SECONDS"
        )

        # LLM (F-14 / S5)
        llm_fallback_mode = (env.get("LLM_FALLBACK_MODE", "keyword").strip() or "keyword")
        gemini_api_key: str | None = env.get("GEMINI_API_KEY", "").strip() or None
        gemini_model = env.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
        llm_timeout_s = _parse_float(env.get("LLM_TIMEOUT_S", "8") or "8", "LLM_TIMEOUT_S", minimum=0.1)
        llm_max_output_tokens = _parse_positive_int(
            env.get("LLM_MAX_OUTPUT_TOKENS", "512") or "512", "LLM_MAX_OUTPUT_TOKENS"
        )
        if llm_fallback_mode == "llm" and not gemini_api_key:
            # Graceful degradation, not a crash (exit-criterion §6.13): main.py
            # will see no key and build llm_client=None → resolve_tool_calls /
            # compose_reply both no-op to the keyword router / _render_result.
            logger.warning(
                "LLM_FALLBACK_MODE=llm but GEMINI_API_KEY is empty — running keyword-only."
            )

        # Cookie security
        cookie_secure = _parse_bool(env.get("COOKIE_SECURE", "false"))

        logger.info(
            "orchestrator_config_loaded | is_base_url=%s hr_agent_url=%s it_agent_url=%s "
            "llm_fallback_mode=%s gemini_model=%s port=%d",
            is_base_url,
            hr_agent_url,
            it_agent_url,
            llm_fallback_mode,
            gemini_model,
            port,
        )

        return cls(
            is_base_url=is_base_url,
            is_insecure_tls=is_insecure_tls,
            is_issuer=is_issuer,
            is_jwks_url=is_jwks_url,
            mcp_client_id=mcp_client_id,
            mcp_client_secret=mcp_client_secret,
            mcp_redirect_uri=mcp_redirect_uri,
            orchestrator_agent=orchestrator_agent,
            hr_agent_url=hr_agent_url,
            it_agent_url=it_agent_url,
            hr_server_url=hr_server_url,
            it_server_url=it_server_url,
            internal_revoke_shared_secret=internal_revoke_shared_secret,
            post_logout_redirect_uri=post_logout_redirect_uri,
            hr_agent_oauth_client_id=hr_agent_oauth_client_id,
            it_agent_oauth_client_id=it_agent_oauth_client_id,
            trusted_specialist_subs=trusted_specialist_subs,
            allowed_origins=allowed_origins,
            host=host,
            port=port,
            session_cookie_name=session_cookie_name,
            session_ttl_seconds=session_ttl_seconds,
            llm_fallback_mode=llm_fallback_mode,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            llm_timeout_s=llm_timeout_s,
            llm_max_output_tokens=llm_max_output_tokens,
            cookie_secure=cookie_secure,
        )


def load() -> OrchestratorConfig:
    """Module-level factory: read ``os.environ`` and return a validated config.

    Raises:
        ValueError: Propagated from :meth:`OrchestratorConfig.from_env`.
    """
    return OrchestratorConfig.from_env()
