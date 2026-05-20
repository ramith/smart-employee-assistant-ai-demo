"""Agent-card Pydantic model + helpers (v3-custom schema).

See docs/agent-card-schema.md for the full spec.

Design notes (F-09 compliant)
------------------------------
AgentCard is a Pydantic v2 BaseModel because it crosses HTTP boundaries
(served at GET /.well-known/agent-card.json and validated by the orchestrator
on discovery). It is NOT an asyncio carrier, so Pydantic is the right choice
here per sprint-1-fixes.md §F-09.
"""
from __future__ import annotations

import logging
import re
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "v3-custom"

_BASE_URL_RE = re.compile(r"^https?://[^/]+$")
_TOOL_ID_RE = re.compile(r"^[^.]+\.[^.]+")  # at least one dot with content on both sides


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class Skill(BaseModel):
    """A single capability exposed by a specialist agent.

    Attributes:
        tool_id: Namespaced identifier of the form ``<agent>.<verb>``,
            e.g. ``hr.read_balance``.  The dot is mandatory; collisions
            across agents are impossible by construction.
        label: Human-readable display name shown in logs and LLM prompts.
        description: One-sentence description shown to the LLM to guide
            routing.  Keep concise — this directly influences tool selection.
        scope: The *_a2a OAuth scope required to invoke this skill
            (documentation only; enforcement is at the JWT validator).
        required_scopes: Legacy list form; prefer ``scope`` for new skills.
            Kept for backward compatibility with existing card fixtures.
    """

    tool_id: str = Field(
        ...,
        alias="id",
        serialization_alias="id",
        description="Namespaced tool identifier, e.g. 'hr.read_balance'.",
    )
    label: str = Field(..., alias="name", serialization_alias="name")
    description: str
    scope: str = Field(default="", alias="scope", serialization_alias="scope")
    # Legacy field preserved for existing card fixtures; not used at runtime.
    required_scopes: list[str] = Field(default_factory=list)
    # S5: names of the arguments this tool accepts — used by the LLM router to
    # know what to extract from the user message, and to strip hallucinated arg
    # keys. Empty for parameter-less tools. Must match the agent's dispatcher
    # ``_TOOL_REGISTRY`` ``kwargs_builder`` keys 1:1.
    args: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @field_validator("tool_id", mode="after")
    @classmethod
    def _validate_tool_id_namespaced(cls, v: str) -> str:
        """Reject tool_ids that lack a namespace dot, e.g. ``approve_leave``."""
        if "." not in v:
            raise ValueError(
                f"tool_id '{v}' is not namespaced — must be '<agent>.<verb>', "
                "e.g. 'hr.approve_leave'."
            )
        parts = v.split(".", 1)
        if not parts[0] or not parts[1]:
            raise ValueError(
                f"tool_id '{v}' has an empty namespace or verb segment."
            )
        return v


class Capabilities(BaseModel):
    """Feature flags for optional A2A transport extensions.

    Attributes:
        streaming: True once ``message/stream`` is implemented (out of Sprint 1
            scope).
        pushNotifications: True when push notification channel is supported
            (out of POC scope).
    """

    streaming: bool = False
    pushNotifications: bool = False


class AuthBlock(BaseModel):
    """Advisory-only auth metadata embedded in the card.

    IMPORTANT — NEVER used at runtime to configure JWKS fetching or
    token-exchange parameters.  The orchestrator hard-codes the issuer
    from its own env vars; the allowlisted fetch URL is the only trust
    anchor.  See docs/agent-card-schema.md §3.

    Attributes:
        scheme: Always ``"oauth2"`` for this POC.
        issuer: OIDC issuer URL (WSO2 IS; advisory).
        audience: Specialist's canonical URI (advisory).
    """

    scheme: str = "oauth2"
    issuer: str
    audience: str


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------


class AgentCard(BaseModel):
    """Pydantic v2 model for the agent-card JSON document.

    Served at ``GET /.well-known/agent-card.json`` on each specialist origin
    and validated by the orchestrator on discovery.

    Attributes:
        schema_version: Must be ``"v3-custom"``; unknown values cause the
            orchestrator to log and skip the card.
        id: Opaque slug identifying the specialist, e.g. ``"hr_agent"``.
            Used by the orchestrator as a stable agent_id key.
        label: Human-readable display name (maps from card's ``name`` field).
        description: One-sentence summary shown to the LLM via
            ``discover_agents``.
        base_url: Canonical origin, e.g. ``https://hr.smart-employee.local``.
            Must match ``^https?://[^/]+$`` (no path, no trailing slash).
            **Stripped from LLM projection** — never leaks to LLM traces.
        oauth_client_id: The specialist's OAuth Application client_id.  Used
            for N28 client-id collision detection at boot time (F-15).
            **Stripped from LLM projection.**
        api_version: Semver of the specialist's A2A surface.  Major-version
            drift triggers a warning in the orchestrator; a mismatch does NOT
            raise in Sprint 1 (Sprint 2 may tighten).
        skills: List of capabilities.  An empty list is valid.
        capabilities: Optional transport feature flags; defaults to all-false.
        auth: Advisory-only auth metadata.  See ``AuthBlock``.
    """

    schema_version: str = SCHEMA_VERSION
    id: str
    label: str = Field(..., alias="name", serialization_alias="name")
    description: str
    base_url: str = Field(..., alias="url", serialization_alias="url")
    oauth_client_id: str
    api_version: str
    skills: list[Skill]
    capabilities: Capabilities = Field(default_factory=Capabilities)
    auth: AuthBlock

    model_config = {"populate_by_name": True}

    @field_validator("base_url", mode="after")
    @classmethod
    def _validate_base_url(cls, v: str) -> str:
        """Reject URLs that include a path or trailing slash."""
        if not _BASE_URL_RE.match(v):
            raise ValueError(
                f"base_url '{v}' must match '^https?://[^/]+$' "
                "(no path segments, no trailing slash)."
            )
        return v

    @model_validator(mode="after")
    def _warn_on_unknown_api_version(self) -> AgentCard:
        """Emit a warning when api_version is unrecognised; does NOT raise.

        Sprint 1 tolerates any semver string.  Sprint 2 may tighten this to
        reject unknown major versions.
        """
        known_prefixes = ("1.",)
        if not any(self.api_version.startswith(p) for p in known_prefixes):
            logger.warning(
                "AgentCard for '%s' has unrecognised api_version '%s'; "
                "expected 1.x — proceeding anyway (Sprint 2 may tighten).",
                self.id,
                self.api_version,
            )
        return self


# ---------------------------------------------------------------------------
# LLM projection
# ---------------------------------------------------------------------------


def llm_projection(card: AgentCard) -> dict:
    """Return a sanitised dict safe to embed directly in an LLM prompt.

    Strips every field that could leak infrastructure details or auth
    metadata:

    - ``base_url`` — reveals internal service topology.
    - ``oauth_client_id`` — sensitive for T9 / N28 collision analysis.
    - ``auth`` — advisory block; LLM has no use for issuer/audience.
    - ``capabilities``, ``schema_version``, ``api_version`` — noise.

    Only the following are included::

        {
            "id": "<agent-slug>",
            "label": "<display name>",
            "skills": [
                {
                    "tool_id": "<ns.verb>",
                    "label": "<human name>",
                    "description": "<one sentence>",
                    "scope": "<a2a scope string>",
                }
            ]
        }

    Args:
        card: A validated :class:`AgentCard` instance.

    Returns:
        A plain ``dict`` with no secrets, ready for JSON serialisation into
        an LLM system-prompt or tool-call context.
    """
    return {
        "id": card.id,
        "label": card.label,
        "skills": [
            {
                "tool_id": skill.tool_id,
                "label": skill.label,
                "description": skill.description,
                "scope": skill.scope,
                "args": list(skill.args),
            }
            for skill in card.skills
        ],
    }
