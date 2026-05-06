"""Agent-card Pydantic model + helpers (v3-custom schema).

See docs/agent-card-schema.md for the full spec.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


SCHEMA_VERSION = "v3-custom"


class Skill(BaseModel):
    id: str  # namespaced, e.g. "hr.approve_leave"
    name: str
    description: str
    required_scopes: list[str] = Field(default_factory=list)  # documentation only


class Capabilities(BaseModel):
    streaming: bool = False
    pushNotifications: bool = False


class AuthBlock(BaseModel):
    """ADVISORY ONLY — never used at runtime to set up JWKS or token-exchange.

    Orchestrator hardcodes the issuer; uses the allowlisted fetch URL as
    the token-exchange `resource`. See docs/agent-card-schema.md §3.
    """

    scheme: str = "oauth2"
    issuer: str
    audience: str


class AgentCard(BaseModel):
    schema_version: str = SCHEMA_VERSION
    name: str
    description: str
    url: str  # canonical specialist URI; matches token aud
    api_version: str  # semver
    skills: list[Skill]
    capabilities: Capabilities = Field(default_factory=Capabilities)
    auth: AuthBlock


def llm_projection(card: AgentCard, agent_id: str) -> dict:
    """Strip url/auth/schema/version/capabilities before exposing to LLM.

    Per milestone-plan §3.4 task 11: LLM gets opaque agent_id +
    name/description/skill_id/skill_name/skill_description.
    """
    return {
        "agent_id": agent_id,
        "name": card.name,
        "description": card.description,
        "skills": [
            {"id": s.id, "name": s.name, "description": s.description}
            for s in card.skills
        ],
    }
