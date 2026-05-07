# Scope Naming Policy

**One-page reference.** When in doubt, follow the existing `hr_*_mcp` pattern; when adding new scopes, name them per §1; if you find yourself wanting to deviate, update this doc first.

## 1. Naming convention

`<resource>_<action>_<transport>`

- **`<resource>`** — short noun for the domain (`hr`, `it`).
- **`<action>`** — verb describing what the scope authorizes (`basic`, `self`, `read`, `approve`, `assets_read`, `assets_write`).
- **`<transport>`** — the wire protocol the scope gates:
  - **`a2a`** — A2A JSON-RPC (validated at `hr_agent`, `it_agent`).
  - **`mcp`** — MCP / FastMCP (validated at `hr_server`, `it_server`).

The suffix names a transport, not a tier — but in the v3 architecture the two map cleanly: A2A is what specialists speak (agent-tier), MCP is what backends speak (backend-tier). This keeps consistency with the existing tenant's `_mcp` scopes on hr_server.

**Why distinct scopes per transport?** Asgardeo enforces *organization-wide scope-name uniqueness* — a scope name binds to exactly one API resource. The token-exchange transformation between Hop 3a/3b and Hop 4/5 then has *explicit* semantics: "the user's consented A2A operation becomes an MCP operation against the backend, with the agent layer named in the actor chain." Each tier validates its own slice independently. This is a feature, not a workaround.

Special umbrella scope: **`agent_access`** — single flat scope; gates whether a user can talk to the orchestrator at all. Not domain-specific. The only legitimate exception to the naming convention.

## 2. Current scope inventory

| Scope | API Resource | Granted to roles | Gates |
|---|---|---|---|
| `agent_access` | (umbrella, on every agent-tier API resource by convention — see §3) | `employee`, `hr_admin` | Sign-in to orchestrator |
| **Agent-tier (`_a2a` — validated at hr_agent / it_agent):** | | | |
| `hr_basic_a2a` | hr_agent-api | `employee`, `hr_admin` | Ask hr_agent for HR holiday/policy info |
| `hr_self_a2a` | hr_agent-api | `employee`, `hr_admin` | Ask hr_agent for user's own leave/profile |
| `hr_read_a2a` | hr_agent-api | `hr_admin` | Ask hr_agent to read any employee's HR data |
| `hr_approve_a2a` | hr_agent-api | `hr_admin` | Ask hr_agent to approve/reject leave |
| `it_assets_read_a2a` | it_agent-api | `employee`, `hr_admin` | Ask it_agent to read assets |
| **Backend-tier (`_mcp` — validated at hr_server / it_server):** | | | |
| `hr_basic_mcp` | hr_server-api | `employee`, `hr_admin` | Call hr_server MCP for HR holiday/policy |
| `hr_self_mcp` | hr_server-api | `employee`, `hr_admin` | Call hr_server MCP for user's own leave/profile |
| `hr_read_mcp` | hr_server-api | `hr_admin` | Call hr_server MCP to read any employee's HR data |
| `hr_approve_mcp` | hr_server-api | `hr_admin` | Call hr_server MCP to approve/reject leave |
| `it_assets_read_mcp` | it_server-api | `employee`, `hr_admin` | Call it_server MCP to read assets |

## 3. Rules

1. **Scope names are unique per Asgardeo organization** — a scope binds to exactly one API resource. The `_a2a`/`_mcp` split is a direct consequence.
2. **Token-exchange transforms scopes between hops.** At Hop 4 (hr_agent → hr_server), the orchestrator requests `scope=hr_*_mcp` with `subject_token` carrying `hr_*_a2a`. Asgardeo grants the new scope set if the requesting client is subscribed to both API resources. This substitution is the security boundary between agent-tier and backend-tier.
3. **Both tiers validate independently.** hr_agent checks `_a2a`; hr_server checks `_mcp`. Neither trusts the other — the chain (`act` claim) and the audience are the only cross-tier trust mechanism.
4. **Skill IDs in agent cards are namespaced** — `<agent>.<verb>` (e.g., `hr.approve_leave`). Skill IDs are NOT scopes; they're routing labels visible to the LLM.
5. **Each skill's `required_scopes` field in the agent card** is documentation. Actual enforcement is at JWT validation (`common/auth/jwt_validator.py` + `common/auth/peer_trust.py`).
6. **Renaming a scope is a breaking change.** Every issued token, role assignment, and validator config changes. Add new scopes; deprecate old ones with a migration window.
7. **Subscription requirement.** For an OAuth client (`orchestrator-app` SPA, plus the three agent identities) to request a scope, the client must be subscribed to the API resource that owns the scope. This is configured per-app/agent in Asgardeo (Step 5 of `asgardeo-setup.md`).

## 4. Why this is the right shape

The same operation appears at two tiers, with two different scope names, because **they're different operations**:

- `hr_read_a2a` = "the user authorized the orchestrator to ask hr_agent to read HR data."
- `hr_read_mcp` = "the user authorized hr_agent to call hr_server's read tool."

The token-exchange step at Hop 4 is the moment the agent-tier promise is converted into a backend-tier capability — that's where the `act` chain grows by one (depth 2 — orchestrator-agent inside hr_agent). The two scope names make this visible in audit logs.

Alternative considered and rejected:
- **One scope per logical operation, used at every tier** — fails Asgardeo's uniqueness rule.
- **Backend-only scopes, agent-tier audience-only** — collapses the agent layer's authorization story; loses scope-level audit at the agent boundary.
- **Single `hr-api` resource covering both audiences** — Asgardeo doesn't support multiple audiences per resource cleanly, and it would defeat the audience-narrowing goal.

## 5. Reference

- Plan: [milestone-plan.md](milestone-plan.md) §2.3.
- Asgardeo setup: [asgardeo-setup.md](asgardeo-setup.md) §5 (role + scope assignment).
