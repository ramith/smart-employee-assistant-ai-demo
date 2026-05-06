# Scope Naming Policy

**One-page reference.** When in doubt, follow the existing `hr_*_mcp` pattern; when adding new scopes, name them per §1; if you find yourself wanting to deviate, update this doc first.

## 1. Naming convention

`<resource>_<action>_<transport>`

- **`<resource>`** — short noun for the domain (`hr`, `it`, `agent`).
- **`<action>`** — verb describing what the scope authorizes (`basic`, `self`, `read`, `approve`, `assets_read`, `assets_write`).
- **`<transport>`** — the surface the scope gates (`mcp`, `rest`).

Special umbrella scope: **`agent_access`** — single flat scope; gates whether a user can talk to the orchestrator at all. Not domain-specific. The only legitimate exception to the naming convention.

## 2. Current scope inventory

| Scope | Resource | Granted to roles | Gates |
|---|---|---|---|
| `agent_access` | (umbrella) | `employee`, `hr_admin` | Sign-in to orchestrator |
| `hr_basic_mcp` | hr-agent-api / hr-server-api | `employee`, `hr_admin` | HR holiday/policy lookups (no PII) |
| `hr_self_mcp` | hr-agent-api / hr-server-api | `employee`, `hr_admin` | User's own leave/profile |
| `hr_read_mcp` | hr-agent-api / hr-server-api | `hr_admin` | Read any employee's HR data |
| `hr_approve_mcp` | hr-agent-api / hr-server-api | `hr_admin` | Approve/reject leave |
| `it_assets_read_mcp` | it-agent-api / it-server-api | `employee`, `hr_admin` | Read any employee's asset list |
| `it_assets_write_mcp` | it-agent-api / it-server-api | (reserved — not used in Sprint 1) | Modify asset assignments |

## 3. Rules

1. **Scope name MUST match across API resources that share the action.** `hr_read_mcp` is the same scope at `hr-agent-api` and `hr-server-api`. The orchestrator requests it once; it propagates through Hops 3a → 4 unchanged.
2. **Skill IDs in agent cards are namespaced** — `<agent>.<verb>` (e.g., `hr.approve_leave`, `it.get_employee_assets`). Skill IDs are NOT scopes; they're routing tokens visible to the LLM. Scopes are enforced at JWT validation.
3. **Each skill in an agent card declares `required_scopes`** — but this is documentation only. Actual enforcement is at the validator (`common/auth/peer_trust.py`).
4. **Adding a new scope:**
   - Pick the name per §1.
   - Register at the relevant API resource(s) in Asgardeo.
   - Assign to roles in Asgardeo.
   - Update this doc's §2 table.
5. **Renaming a scope is a breaking change** — every issued token, role assignment, and validator code path changes. Don't rename without a migration plan; prefer adding a new scope and deprecating the old one.

## 4. Why `<resource>_<action>_<transport>` and not `<resource>.<action>`

The existing tenant uses underscore convention (`hr_basic_mcp`, etc.). Switching to dots would require renaming all currently-issued scopes — a tenant-wide breaking change for cosmetic gain. v3 keeps the existing convention.

(The agent-card *skill IDs* use dot notation — `hr.approve_leave` — because they're a different namespace, governed by the A2A protocol convention, not OAuth scopes.)

## 5. Reference

- Plan: [milestone-plan.md](milestone-plan.md) §2.3.
- Asgardeo setup: [asgardeo-setup.md](asgardeo-setup.md) §5 (role + scope assignment).
