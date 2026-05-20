# Agent Card Schema (v3-custom)

**Format:** JSON document served at `GET /.well-known/agent-card.json` on each specialist's HTTP origin.
**Codified in:** `common/a2a/agent_card.py` (Pydantic model).
**Loaded by:** the orchestrator at startup from `AGENT_CARDS_DIR` (default `tests/fixtures/agent_cards/*.json`); each `*.json` is validated against the model. LLM sees a *redacted* projection (no `url`, no `oauth_client_id`, no `auth`).

## 1. Full schema

```jsonc
{
  "schema_version": "v3-custom",                       // string; orchestrator validates and falls back if unknown
  "name": "HR Agent",                                  // human-readable display name
  "description": "Handles HR queries: leave, time-off, employee info.",
  "id": "hr_agent",                                    // opaque slug; stable agent_id key used by orchestrator
  "url": "https://hr.smart-employee.local",            // canonical origin; no path, no trailing slash (regex ^https?://[^/]+$)
  "oauth_client_id": "hr-agent-oauth-client-id-...",   // the HR Agent App OAuth Client ID (N28 collision detection)
  "api_version": "1.0.0",                              // semver of specialist's A2A surface; major bump = breaking
  "skills": [
    {
      "id": "hr.approve_leave",                        // namespaced (<agent>.<verb>); collisions impossible
      "name": "Approve leave request",                 // human-readable
      "description": "Approve or reject a leave request by id.",  // shown to LLM via discover_agents
      "scope": "hr_approve_rest",                      // single-tier scope — same name requested at CIBA, embedded in OBO token, and validated at hr_server. Documentation only; enforcement at the JWT validator.
      "required_scopes": ["hr_approve_rest"],          // legacy list form; kept for backward compat with card fixtures
      "args": ["leave_id"]                             // arg names the LLM router extracts; matches the agent dispatcher 1:1
    }
  ],
  "capabilities": {
    "streaming": false,                                // true once message/stream is implemented (out of POC scope)
    "pushNotifications": false                         // out of POC scope
  },
  "auth": {
    "scheme": "oauth2",                                // ADVISORY ONLY — never used at runtime
    "issuer": "https://<wso2-is-host>:9443/oauth2/token", // ADVISORY ONLY — orchestrator hardcodes the issuer from its own env
    "audience": "https://hr.smart-employee.local"      // ADVISORY ONLY — orchestrator uses the allowlisted fetch URL
  }
}
```

## 2. Field semantics

| Field | Required | Type | Notes |
|---|---|---|---|
| `schema_version` | yes | string | `"v3-custom"` for this POC. Unknown values → orchestrator logs and ignores the card. |
| `id` | yes | string | Opaque slug (e.g. `"hr_agent"`); the orchestrator's stable agent_id key. |
| `name` | yes | string | Shown to humans (e.g., logs); also exposed to LLM via `discover_agents`. |
| `description` | yes | string | Shown to LLM. Keep concise — this influences routing. |
| `url` | yes | string (URI) | Canonical specialist origin. Must match `^https?://[^/]+$` (no path, no trailing slash). **Stripped from LLM trace** by surgical redactor. |
| `oauth_client_id` | yes | string | The specialist's OAuth Application client_id; used for N28 client-id collision detection at boot. **Stripped from LLM trace.** |
| `api_version` | yes | string (semver) | Major-version drift triggers warning + skill list re-fetch in orchestrator. |
| `skills[]` | yes | array | At least one skill required. (An empty list is also accepted by the model.) |
| `skills[].id` | yes | string | Namespaced `<agent>.<verb>`. Cross-agent collisions impossible by construction. |
| `skills[].name` | yes | string | Human-readable. |
| `skills[].description` | yes | string | Shown to LLM. |
| `skills[].scope` | no | string | The single-tier scope the skill requests via CIBA (e.g. `hr_approve_rest`). Same name flows through OBO token and MCP validation. **Documentation only**; enforcement is at the validator. |
| `skills[].required_scopes` | no | array of string | Legacy list form; prefer `scope`. Kept for backward compat with card fixtures. **Documentation only.** |
| `skills[].args` | no | array of string | Arg names the LLM router extracts; must match the agent dispatcher's `_TOOL_REGISTRY` kwargs 1:1. Empty for parameter-less tools. |
| `capabilities.streaming` | no | bool | `false` for the POC (no `message/stream`). Defaults to `false`. |
| `capabilities.pushNotifications` | no | bool | `false`. Out of POC scope. Defaults to `false`. |
| `auth.scheme` | yes | string | `"oauth2"`. Advisory. |
| `auth.issuer` | yes | string | **Advisory only.** The validator hardcodes the WSO2 IS issuer from its own env. If a card's `auth.issuer` ≠ configured, log + refuse to load (N8b). |
| `auth.audience` | yes | string | **Advisory only.** Orchestrator uses the **allowlisted fetch URL** — never `auth.audience`. |

## 3. Trust model (security-critical)

**The agent card is NOT a trust anchor.** Tokens are.

- **URL allowlist** (`ORCHESTRATOR_AGENT_CARD_URLS` env var on orchestrator) is the only mechanism by which a card is trusted enough to be parsed.
- The card body's `auth.issuer` and `auth.audience` are **advisory metadata** — handy for debugging, never used at runtime to set up JWKS or token-exchange parameters.
- Negative tests N8 (spoofed URL) and N8b (mismatched `auth.issuer`) prove this contract holds.

## 4. LLM projection

When the orchestrator's `discover_agents` tool returns cards to the LLM, the projection is:

```jsonc
{
  "id": "hr_agent",                                    // the card's opaque slug
  "label": "HR Agent",                                 // the card's display name (from `name`)
  "skills": [
    {
      "tool_id": "hr.approve_leave",
      "label": "Approve leave request",
      "description": "...",
      "scope": "hr_approve_rest",                      // included so the router can pre-validate, but never used as a trust input
      "args": ["leave_id"]
    }
  ]
  // NO url, NO oauth_client_id, NO auth, NO schema_version, NO api_version, NO capabilities
}
```

This mirrors `common/a2a/agent_card.py:llm_projection()`. The orchestrator's surgical LangSmith redactor strips `url` and `auth` keys from any agent-card-shaped object before traces are uploaded.

## 5. Versioning policy

- `schema_version: "v3-custom"` is this POC's bespoke format. When the official A2A SDK schema stabilizes, migrate by incrementing this field and adding a translator in `common/a2a/agent_card.py`.
- `api_version` semver tracks the specialist's A2A surface. The orchestrator caches it; on major-version drift detected via `tools/list`-equivalent failure, force-refresh.

## 6. Reference

- Plan: [milestone-plan.md](milestone-plan.md) §2.5, §3.4 task 11, §5.2 threat model.
- Pydantic model: `common/a2a/agent_card.py` (Sprint 0 deliverable).
- A2A v0.3 spec for future migration: https://a2a-protocol.org/latest/specification/
