# Scope Naming Policy

**One-page reference.** Locked for Sprint 2 build. When in doubt, follow the existing `<resource>_<action>_rest` pattern; if you find yourself wanting to deviate, update this doc first.

This policy reflects the **per-agent CIBA** architecture (v4) deployed on WSO2 IS 7.2.0 on-prem. The previous Asgardeo-era `_a2a` / `_mcp` two-tier split is archived in [milestone-plan-v3-rfc8693-archived.md](milestone-plan-v3-rfc8693-archived.md) and is no longer in force.

## 1. Naming convention

`<resource>_<action>_rest`

- **`<resource>`** — short noun for the domain (`hr`, `it`).
- **`<action>`** — verb describing what the scope authorizes (`basic`, `self`, `read`, `approve`, `assets_read`, `assets_write`).
- **`_rest` suffix** — fixed; signals "REST/MCP API resource scope" and disambiguates from OIDC standard scopes (`openid`, `profile`, `email`).

There is **one tier**: the same scope name is requested at CIBA initiation by the agent, embedded in the OBO token issued by IS, and validated by the MCP server. The audience claim (`aud`) and the actor chain (`act.sub`) handle cross-tier trust, not separate scope names.

**Why one tier (was two on Asgardeo):**
- Asgardeo enforced organization-wide scope-name uniqueness, forcing a `_a2a` / `_mcp` split per API resource. WSO2 IS 7.2 does not have this constraint — the same scope can live on multiple API resources.
- Per-agent CIBA replaces RFC 8693 token-exchange (per F1–F4 in the M0 spike memo). There is no scope-substitution step between Hop 4a and Hop 4b; the agent and server validate the same scope.
- Audit clarity: a single scope name flows through the call chain. The depth-1 `act` claim (`{sub: user, act: {sub: agent}}`) names the agent that acted; the audience names the server. No ambiguity.

## 2. Current scope inventory (deployed)

| Scope | API Resource | Granted to roles | Gates |
|---|---|---|---|
| `hr_basic_rest` | hr_server-api | `Employee`, `HR Admin` | hr_server holiday/policy reads (general info) |
| `hr_self_rest` | hr_server-api | `Employee`, `HR Admin` | hr_server reads of the calling user's own leave/profile |
| `hr_read_rest` | hr_server-api | `HR Admin` | hr_server reads of *any* employee's HR data — `get_cubicle_summary`, `get_vacant_cubicles_on_floor`, `lookup_employee` (cubicle/seat allocation flow) |
| `hr_approve_rest` | hr_server-api | `HR Admin` | hr_server `approve_leave` + `reject_leave` (write) + `get_all_leave_requests` (`hr.read_all_leaves` chat skill) |
| `hr_assets_write_rest` | hr_server-api | `HR Admin` | hr_server `assign_cubicle` (write — cubicle/seat assignments). NOTE: this scope must exist on the `hr_server-api` API Resource in IS and be authorised on the `hr-agent` OAuth app; verify with `scripts/check-is-config.py`. |
| `it_assets_read_rest` | it_server-api | `Employee`, `HR Admin` | it_server asset list / lookup |
| `it_assets_self_rest` | it_server-api | `Employee`, `HR Admin` | it_server reads of the calling user's own assigned assets |
| `it_assets_write_rest` | it_server-api | `HR Admin` | it_server `issue_asset` (write — assigns assets) |

**No umbrella `agent_access` scope.** The legacy v3 agent gate is not part of the per-agent CIBA architecture. The pre-v4 standalone `agent`/`client` services that referenced it have been removed from the repo; the live fleet is the five services in `docker-compose.yml`.

## 3. Rules

1. **Same scope, every hop.** The agent requests `hr_self_rest` on CIBA init; IS issues an OBO token with `scope=hr_self_rest`; hr_server validates `hr_self_rest`. No transformation.
2. **Per-tool scope dispatch.** Each MCP tool maps to exactly one scope. Agents look up the tool's required scope at A2A receive time (see `hr_agent/ciba/orchestrator.py:_TOOL_REGISTRY` and the IT counterpart). The agent's env-default `*_CIBA_SCOPE` is the fallback for tools that don't override.
3. **Server-side scope guard is authoritative.** Even if IS misconfiguration issues a token with the wrong scope, the MCP server's `required_scopes` in `tools.py` rejects it with `ERR-MCP-003`. This is N34's invariant.
4. **Skill IDs in agent cards are namespaced** — `<agent>.<verb>` (e.g., `hr.approve_leave`). Skill IDs are routing labels, not scopes.
5. **Renaming a scope is a breaking change.** Every issued token, role assignment, and validator config changes. Add new scopes; deprecate old ones with a migration window.
6. **Subscription requirement.** For an agent's auto-created OAuth App to request a scope, the App must be subscribed to the API resource that owns the scope. Configured per-agent in IS Console → Applications → `<agent>-app` → API Authorization.
7. **Role-to-scope binding is at IS, not in the agent.** The role's "Permissions" tab in IS Console determines which scopes can be released to a user under that role. UC-08 demonstrates: `Employee` lacks `it_assets_write_rest`; IS rejects CIBA initiation (or the consent screen) for that scope. The agent only sees an error response.

## 4. How a request flows (deployed)

```
1. Employee asks orchestrator: "what laptops are available?"
2. Orchestrator keyword-router → ToolCall(agent=it_agent, tool=it.list_available_assets)
3. Orchestrator → it_agent /a2a/message/send (with token-A: orchestrator's user-delegated token)
4. it_agent looks up tool in _TOOL_REGISTRY → required scope `it_assets_read_rest`
5. it_agent → IS /oauth2/ciba (login_hint=user, actor_token=it_agent's I4 token,
   scope=openid it_assets_read_rest)
6. IS returns auth_req_id + auth_url; orchestrator pushes auth_url to SPA via SSE
7. User clicks, approves at IS
8. it_agent polls /oauth2/token; IS issues OBO token-B with scope=it_assets_read_rest,
   sub=user, act.sub=it_agent's UUID, aud=it_agent's OAuth client_id
9. it_agent calls it_server /mcp/tools/list_available_assets with Bearer token-B
10. it_server validates: aud == self, act.sub trusted, scope contains it_assets_read_rest
11. Result returns up the chain
```

For UC-08 (Employee asks for `issue_asset`):
- Step 4 finds `it.issue_asset` → required scope `it_assets_write_rest`
- Step 5 CIBA call to IS for `it_assets_write_rest`
- IS checks role-to-scope binding: Employee role does NOT grant `it_assets_write_rest`
- IS returns `invalid_scope` (init-time) or `access_denied` (consent-time) — empirically determined by the C11 probe (Sprint 2 Day 1)
- Agent surfaces error → orchestrator renders denial copy from `copy-deck.md` §7.17
- **No token is ever issued.**

## 5. Why one tier is the right shape (post-CIBA)

The Asgardeo two-tier doc rationalized scope-substitution as a security boundary. With per-agent CIBA, that boundary exists implicitly:

- The agent only requests scopes in its own `*_CIBA_SCOPE` allowlist.
- The user's role at IS gates which scopes can be issued.
- The OBO token's audience binds it to one server.
- The act chain identifies which agent acted.

A second scope name per tier added audit-log readability (Asgardeo era) at the cost of token-exchange complexity. CIBA produces depth-1 act tokens directly with the same scope name; the audit story uses `act.sub` to identify the agent rather than scope-suffix.

## 6. Reference

- Plan: [milestone-plan.md](milestone-plan.md) §2.3 and §3.4 (D2.7..D2.11).
- WSO2 IS setup: [wso2-is-setup.md](wso2-is-setup.md) (role + scope assignment).
- Spike memo: [spikes/wso2-is-capability-memo.md](spikes/wso2-is-capability-memo.md) (F1–F7 findings).
- Archived v3 policy: [milestone-plan-v3-rfc8693-archived.md](milestone-plan-v3-rfc8693-archived.md).
