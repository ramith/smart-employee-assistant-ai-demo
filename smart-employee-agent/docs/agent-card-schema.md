# Agent Card Schema (v3-custom)

**Format:** JSON document served at `GET /.well-known/agent-card.json` on each specialist's HTTP origin.
**Codified in:** `common/a2a/agent_card.py` (Pydantic model).
**Used by:** `orchestrator/` for discovery; LLM sees a *redacted* projection (no `url`, no `auth`).

## 1. Full schema

```jsonc
{
  "schema_version": "v3-custom",                       // string; orchestrator validates and falls back if unknown
  "name": "HR Agent",                                  // human-readable display name
  "description": "Handles HR queries: leave, time-off, employee info.",
  "url": "https://hr.smart-employee.local/a2a",        // canonical specialist URI; matches token aud
  "api_version": "1.0.0",                              // semver of specialist's A2A surface; major bump = breaking
  "skills": [
    {
      "id": "hr.approve_leave",                        // namespaced (<agent>.<verb>); collisions impossible
      "name": "Approve leave request",                 // human-readable
      "description": "Approve or reject a leave request by id.",  // shown to LLM via discover_agents
      "required_scopes": ["hr_approve_mcp"]            // documentation only; enforcement at JWT validation
    }
  ],
  "capabilities": {
    "streaming": false,                                // true once message/stream is implemented (out of POC scope)
    "pushNotifications": false                         // out of POC scope
  },
  "auth": {
    "scheme": "oauth2",                                // ADVISORY ONLY — never used at runtime
    "issuer": "<asgardeo_issuer>",                     // ADVISORY ONLY — orchestrator hardcodes
    "audience": "https://hr.smart-employee.local/a2a"  // ADVISORY ONLY — orchestrator uses the allowlisted URL
  }
}
```

## 2. Field semantics

| Field | Required | Type | Notes |
|---|---|---|---|
| `schema_version` | yes | string | `"v3-custom"` for this POC. Unknown values → orchestrator logs and ignores the card. |
| `name` | yes | string | Shown to humans (e.g., logs); also exposed to LLM via `discover_agents`. |
| `description` | yes | string | Shown to LLM. Keep concise — this influences routing. |
| `url` | yes | string (URI) | Canonical specialist URI. **Must match exactly** the API resource audience in Asgardeo. **Stripped from LLM trace** by surgical redactor. |
| `api_version` | yes | string (semver) | Major-version drift triggers warning + skill list re-fetch in orchestrator. |
| `skills[]` | yes | array | At least one skill required. |
| `skills[].id` | yes | string | Namespaced `<agent>.<verb>`. Cross-agent collisions impossible by construction. |
| `skills[].name` | yes | string | Human-readable. |
| `skills[].description` | yes | string | Shown to LLM. |
| `skills[].required_scopes` | yes | array of string | **Documentation only.** Enforcement is at the validator. |
| `capabilities.streaming` | yes | bool | `false` for Sprint 1 (no `message/stream`). |
| `capabilities.pushNotifications` | yes | bool | `false`. Out of POC scope. |
| `auth.scheme` | yes | string | `"oauth2"`. Advisory. |
| `auth.issuer` | yes | string | **Advisory only.** Validator hardcodes the Asgardeo issuer. If a card's `auth.issuer` ≠ configured, log + refuse to load (N8b). |
| `auth.audience` | yes | string | **Advisory only.** Orchestrator uses the **allowlisted fetch URL** as the token-exchange `resource` — never `auth.audience`. |

## 3. Trust model (security-critical)

**The agent card is NOT a trust anchor.** Tokens are.

- **URL allowlist** (`ORCHESTRATOR_AGENT_CARD_URLS` env var on orchestrator) is the only mechanism by which a card is trusted enough to be parsed.
- The card body's `auth.issuer` and `auth.audience` are **advisory metadata** — handy for debugging, never used at runtime to set up JWKS or token-exchange parameters.
- Negative tests N8 (spoofed URL) and N8b (mismatched `auth.issuer`) prove this contract holds.

## 4. LLM projection

When the orchestrator's `discover_agents` tool returns cards to the LLM, the projection is:

```jsonc
{
  "agent_id": "hr-agent",                              // server-assigned opaque enum
  "name": "HR Agent",
  "description": "...",
  "skills": [
    { "id": "hr.approve_leave", "name": "...", "description": "..." }
    // NO required_scopes (LLM doesn't need to know)
  ]
  // NO url, NO auth, NO schema_version, NO api_version, NO capabilities
}
```

The orchestrator's surgical LangSmith redactor strips `url` and `auth` keys from any agent-card-shaped object before traces are uploaded.

## 5. Versioning policy

- `schema_version: "v3-custom"` is this POC's bespoke format. When the official A2A SDK schema stabilizes, migrate by incrementing this field and adding a translator in `common/a2a/agent_card.py`.
- `api_version` semver tracks the specialist's A2A surface. The orchestrator caches it; on major-version drift detected via `tools/list`-equivalent failure, force-refresh.

## 6. Reference

- Plan: [milestone-plan.md](milestone-plan.md) §2.5, §3.4 task 11, §5.2 threat model.
- Pydantic model: `common/a2a/agent_card.py` (Sprint 0 deliverable).
- A2A v0.3 spec for future migration: https://a2a-protocol.org/latest/specification/
