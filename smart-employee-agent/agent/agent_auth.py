"""
 Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.

  Agent Token Management

  Manages the agent's own identity token using Asgardeo's App Native
  Authentication flow. The agent authenticates as a first-class identity
  (registered under Console > Agents) — not via client credentials.
  Auto-refreshes the token when it approaches expiry (30-second buffer).
"""

import os
import time
import logging

from asgardeo import AsgardeoConfig
from asgardeo_ai import AgentConfig, AgentAuthManager

logger = logging.getLogger(__name__)

REFRESH_BUFFER_SECONDS = 30


def _required_env(key: str) -> str:
    """Read an environment variable or raise if missing/empty."""
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Missing required environment variable: {key}")
    return value


class AgentAuth:
    """Manages the agent's own token via Asgardeo App Native Auth."""

    def __init__(self):
        self._asgardeo_config = AsgardeoConfig(
            base_url=_required_env("ASGARDEO_BASE_URL"),
            client_id=_required_env("ASGARDEO_CLIENT_ID"),
            redirect_uri=_required_env("OBO_REDIRECT_URI"),
        )
        self._agent_config = AgentConfig(
            agent_id=_required_env("AGENT_ID"),
            agent_secret=_required_env("AGENT_SECRET"),
        )
        self._token = None
        self._expires_at: float = 0.0

    @property
    def asgardeo_config(self) -> AsgardeoConfig:
        """Asgardeo configuration for SDK calls."""
        return self._asgardeo_config

    @property
    def agent_config(self) -> AgentConfig:
        """Agent configuration for SDK calls."""
        return self._agent_config

    async def ensure_valid_token(self):
        """Return a valid agent token, refreshing if needed."""
        if self._token and time.time() < (self._expires_at - REFRESH_BUFFER_SECONDS):
            remaining = int(self._expires_at - time.time())
            logger.debug("Reusing cached agent token (%ds remaining)", remaining)
            return self._token

        is_refresh = self._token is not None
        if is_refresh:
            logger.info("Agent token expired/expiring — refreshing via App Native Auth...")
        requested_scopes = ["openid", "hr_basic_mcp"]
        logger.info("Obtaining agent token via App Native Auth (requested scopes: %s)",
                    ", ".join(requested_scopes))
        async with AgentAuthManager(self._asgardeo_config, self._agent_config) as auth_manager:
            self._token = await auth_manager.get_agent_token(requested_scopes)

        if hasattr(self._token, "expires_in") and self._token.expires_in:
            self._expires_at = time.time() + self._token.expires_in
        else:
            self._expires_at = time.time() + 3600

        granted_scope = getattr(self._token, "scope", "") or ""
        granted_scopes = granted_scope.split() if granted_scope else []
        missing = [s for s in requested_scopes if s not in granted_scopes]
        logger.info(
            "Agent token obtained (granted scopes: %s | expires_in: %ss)",
            ", ".join(granted_scopes) if granted_scopes else "(none)",
            getattr(self._token, "expires_in", "?"),
        )
        if missing:
            logger.warning(
                "Agent token is missing requested scope(s): %s — "
                "verify the agent is assigned to a role granting these permissions in Asgardeo Console > Roles > Agents tab.",
                ", ".join(missing),
            )
        return self._token
