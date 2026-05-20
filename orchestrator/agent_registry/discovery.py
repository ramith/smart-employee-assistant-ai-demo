"""Agent-card discovery with strict URL allowlist (SSRF mitigation).

This module is Wave 4 of the Sprint-1 module graph.  It depends only on
Wave-1/2 helpers (``common/a2a/agent_card.py``) and Wave-3 transport
(``common/a2a/client.py`` is NOT used directly here — this module owns its
own lightweight ``httpx.AsyncClient`` so that the registry can be initialised
before the full ``A2AClient`` is wired).

Security model
--------------
Only URLs that appear verbatim in ``DiscoveryConfig.allowed_card_urls`` are
ever fetched.  Any caller-supplied URL that is not in the frozenset causes an
immediate ``AgentDiscoveryError`` **before** any network call is made.  This
prevents SSRF via crafted ``url`` parameters at the call site.

Content-Type is intentionally ignored: the body is always parsed by
``AgentCard.model_validate_json``.  An invalid body raises
``AgentDiscoveryError`` regardless of what the server claims its
Content-Type to be.

Usage::

    config = DiscoveryConfig(
        allowed_card_urls=frozenset([
            "http://hr_agent:8001/.well-known/agent-card.json",
            "http://it_agent:8002/.well-known/agent-card.json",
        ]),
        timeout_seconds=10.0,
    )
    discovery = AgentDiscovery(config)

    # Fetch a single card
    card = await discovery.fetch("http://hr_agent:8001/.well-known/agent-card.json")

    # Fetch all allowlisted cards; failures are logged and skipped
    cards = await discovery.fetch_all()   # dict[agent_id, AgentCard]

    await discovery.aclose()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx
from pydantic import ValidationError

from common.a2a.agent_card import AgentCard

__all__ = [
    "DiscoveryConfig",
    "AgentDiscoveryError",
    "AgentDiscovery",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    """Immutable configuration for ``AgentDiscovery``.

    Attributes:
        allowed_card_urls: Exact URL strings that ``AgentDiscovery`` is
            permitted to fetch.  Any URL not present in this frozenset will
            cause an ``AgentDiscoveryError`` before any network call is made.
        timeout_seconds: Per-request HTTP timeout passed to the underlying
            ``httpx.AsyncClient``.
        insecure_tls: When ``True`` the HTTP client skips TLS certificate
            verification.  **Dev/test only — never enable in production.**
    """

    allowed_card_urls: frozenset[str]
    timeout_seconds: float = 10.0
    insecure_tls: bool = False


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class AgentDiscoveryError(Exception):
    """Raised when card discovery fails for any reason.

    Covers three distinct failure modes:
    * The requested URL is not in ``DiscoveryConfig.allowed_card_urls``.
    * The HTTP response has a non-200 status code.
    * The response body fails ``AgentCard.model_validate_json`` validation.
    """


# ---------------------------------------------------------------------------
# Discovery client
# ---------------------------------------------------------------------------


class AgentDiscovery:
    """Fetch and parse agent cards from an explicit allowlist of URLs.

    One instance is intended per orchestrator process.  The instance optionally
    owns its ``httpx.AsyncClient``; when an external client is injected (test
    harness pattern) the caller is responsible for its lifecycle.

    Args:
        config: Immutable discovery configuration including the URL allowlist.
        http: Optional pre-built ``httpx.AsyncClient``.  When ``None`` the
            class constructs its own client using ``verify=not insecure_tls``.
            Owned clients are closed by ``aclose()``.
    """

    def __init__(
        self,
        config: DiscoveryConfig,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owned: bool = http is None
        self._http: httpx.AsyncClient = http or httpx.AsyncClient(
            verify=not config.insecure_tls
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(self, url: str) -> AgentCard:
        """Fetch and parse a single agent card from *url*.

        The URL is validated against the allowlist **before** any network
        activity so that SSRF via crafted ``url`` arguments is impossible.

        Args:
            url: Exact URL to fetch.  Must be present in
                ``config.allowed_card_urls``.

        Returns:
            A validated :class:`~common.a2a.agent_card.AgentCard` instance.

        Raises:
            AgentDiscoveryError: If the URL is not allowlisted, the HTTP
                response is non-200, or the body fails Pydantic validation.
        """
        if url not in self._config.allowed_card_urls:
            raise AgentDiscoveryError(
                f"URL not in allowlist: {url!r} — refusing to fetch (SSRF mitigation)"
            )

        _logger.debug("Fetching agent card from %s", url)

        try:
            response = await self._http.get(
                url, timeout=self._config.timeout_seconds
            )
        except httpx.HTTPError as exc:
            raise AgentDiscoveryError(
                f"HTTP transport error fetching agent card from {url!r}: {exc}"
            ) from exc

        if response.status_code != 200:
            raise AgentDiscoveryError(
                f"Non-200 response fetching agent card from {url!r}: "
                f"status={response.status_code}"
            )

        try:
            card = AgentCard.model_validate_json(response.text)
        except (ValidationError, ValueError) as exc:
            raise AgentDiscoveryError(
                f"Agent card body from {url!r} failed validation: {exc}"
            ) from exc

        _logger.info(
            "Discovered agent card id=%r label=%r from %s",
            card.id,
            card.label,
            url,
        )
        return card

    async def fetch_all(
        self, urls: list[str] | None = None
    ) -> dict[str, AgentCard]:
        """Fetch agent cards from *urls* (or all allowlisted URLs if omitted).

        Failures are logged at WARNING level and skipped; a partial result is
        returned for whichever URLs succeeded.  This method never raises.

        Args:
            urls: Explicit list of URLs to fetch.  Each element must still be
                present in the allowlist; otherwise that URL is skipped with a
                warning.  When ``None`` every URL in
                ``config.allowed_card_urls`` is fetched.

        Returns:
            ``dict`` mapping ``AgentCard.id`` → ``AgentCard`` for each
            URL that was fetched and parsed successfully.
        """
        targets: list[str] = (
            list(urls) if urls is not None else list(self._config.allowed_card_urls)
        )

        result: dict[str, AgentCard] = {}
        for url in targets:
            try:
                card = await self.fetch(url)
            except AgentDiscoveryError as exc:
                _logger.warning(
                    "Skipping agent card from %s: %s", url, exc
                )
                continue
            result[card.id] = card

        return result

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created by this instance.

        Injected clients (``http=`` constructor argument) are NOT closed — their
        lifecycle is the caller's responsibility.
        """
        if self._owned:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "AgentDiscovery":
        """Return self for use as an async context manager."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the client on context-manager exit."""
        await self.aclose()
