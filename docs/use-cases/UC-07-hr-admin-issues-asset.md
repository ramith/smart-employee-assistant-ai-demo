# UC-07 — HR Admin issues IT asset (write-scope demo)

**Sprint:** 2 (depends on `issue_asset` tool landing in it_server, and `it_assets_write_rest` scope wired; `hr.admin` user configured in Sprint 1 per `wso2-is-setup.md` §5.5)
**Priority:** High (demonstrates HR Admin role + cross-domain write scope; strongest "identity-first governance" narrative)
**Maps to N-tests:** N32, N33, N34 (issue_asset happy path + cross-scope replay block + Employee-denied-write-scope)
**Maps to scenarios:** [`user-experience.md`](../user-experience.md) Scenario B (HR Admin variant)

## Actors
- **Primary:** HR Admin user (`hr.admin` in dev)
- **Secondary:** SPA, Orchestrator, IT Agent, IT Server (MCP), WSO2 IS

## Preconditions
- UC-01 has succeeded with `hr.admin` as the signed-in user.
- `hr.admin` has the `HR Admin` role in IS, granting `hr_read_rest`, `hr_approve_rest`, **`it_assets_write_rest`**.
- `it_server` has the `issue_asset` MCP tool registered (Sprint 2 build).
- IT Agent's auto-created OAuth App is subscribed to IT API with `it_assets_read_rest` AND `it_assets_write_rest` scopes.

## Trigger
`hr.admin` types: *"Issue a laptop to Alice (employee ID emp-001)."*

## Main flow
1. Orchestrator's LLM (or keyword fallback) routes to IT Agent with tool `issue_asset`, args `{asset_type: "laptop", employee_id: "emp-001"}`.
2. Orchestrator emits SSE: `{type: "routing", agent: "it_agent"}`.
3. Orchestrator A2A `POST it_agent/a2a/message/send` with token-A (`sub=hr.admin, act.sub=orchestrator-agent`).
4. IT Agent validates token-A; extracts `user_sub = hr.admin's UUID`.
5. IT Agent initiates CIBA: `scope=openid it_assets_write_rest`, `login_hint=<hr.admin UUID>`, `binding_message="IT Agent wants to issue a laptop to Alice for request <corr-id>"`.
6. IS evaluates `hr.admin`'s role — `HR Admin` grants `it_assets_write_rest`. IS returns `{auth_req_id, auth_url}`.
7. IT Agent returns A2A response `{type: "consent_required", auth_url, ...}`.
8. SPA renders Consent Widget: action text `Issue IT assets to employees` (from updated copy-deck §5.A map for `it_assets_write_rest`).
9. `hr.admin` clicks Approve; IS consent screen shows the binding code; `hr.admin` confirms.
10. IT Agent polls `/oauth2/token`; receives token-C: `{sub=hr.admin UUID, act.sub=it_agent, scope=openid it_assets_write_rest}`.
11. IT Agent calls `it_server` MCP tool `issue_asset` with `Authorization: Bearer <token-C>`.
12. IT Server validates token-C: `aud`, `act.sub`, `scope` contains `it_assets_write_rest`. Executes the issue.
13. IT Server returns `{success: true, asset_id: "LAP-042", assigned_to: "emp-001", issued_by: "hr.admin", issued_at: "<ts>"}`.
14. IT Agent returns A2A result; orchestrator LLM composes: *"Laptop LAP-042 has been issued to Alice."*

## Exception flows

### EX-1 — Employee (`probe.user`) attempts the same query
1. CIBA initiated with `scope=openid it_assets_write_rest`, `login_hint=probe.user UUID`.
2. IS evaluates role: Employee does not have `it_assets_write_rest`.
3. IS returns `invalid_scope` on `/oauth2/ciba` initiation OR `access_denied` at consent screen (TBD per Sprint 2 probe).
4. IT Agent returns A2A error; orchestrator surfaces ERR-CIBA-003 message: *"IT Agent can't request the access it needs right now."*
5. **N31** verifies. See **UC-08** for full denial flow.

### EX-2 — HR Admin denies the consent
- Same as UC-04 EX-1 treatment; ERR-CIBA-005 emitted; widget transitions to DENIED.

### EX-3 — `issue_asset` rejects a malformed employee ID
- IT Server returns `{error: "employee_not_found"}`.
- IT Agent surfaces as ERR-MCP-004 equivalent.
- Orchestrator: *"I could not find employee emp-001 in the IT system."*

### EX-4 — Asset already assigned
- IT Server returns `{error: "asset_already_assigned", current_holder: "emp-002"}`.
- Orchestrator: *"Laptop LAP-042 is currently assigned to another employee. Reclaim it first or pick a different asset."*

## Postconditions
- **Success:** asset record updated; token-C in session map `(session_id, it_agent, jti, exp, scope=it_assets_write_rest)`; audit log shows the CIBA event with `sub=hr.admin, act.sub=it_agent, scope=it_assets_write_rest`.
- **Denial:** no asset issued; ERR-CIBA-003 or ERR-CIBA-005 logged; `hr.admin` can retry.

## Design notes for downstream stages

### UX (Stage 3)
- Consent Widget action text: `Issue IT assets to employees` (from copy-deck §5.A map).
- Gerund: `issuing the IT asset` (from §5.C map).
- The write action is visually distinct — consider an **amber tint on the "Wants to:" action line** to signal a state-changing operation (Stage 3 Sprint 2 decision). Read scopes use neutral color; write scopes use amber.

### Architecture (Stage 4)
- `it_server` needs a new MCP tool `issue_asset` with scope guard: require `it_assets_write_rest` in token scope (returns `ERR-MCP-003` if missing).
- `it_service.py` needs a new function `issue_asset_to_employee(asset_id, employee_sub, issued_by_sub)`. Updates `store.py`'s `assets` table to set `assigned_to=employee_sub` and append an issuance log entry.
- IT Agent's `IT_CIBA_SCOPE` env var becomes per-tool at call-time, not a single env-wide default:
  - `list_available_assets` / `get_my_assets` → `openid it_assets_read_rest`
  - `issue_asset` → `openid it_assets_write_rest`
- Orchestrator's LLM tool-routing: `issue_asset` only in the tool plan when user query implies assignment/issue. Keyword fallback: `"issue"`/`"assign"`/`"give X to Y"` → `it_agent.issue_asset`.

### Testing (Stages 7–8)
- **N32** automated: hr.admin → approve_leave_request happy path (parallel HR-write test).
- **N33** automated: hr.admin → issue_asset happy path.
- **N34** automated: cross-scope replay — token-C with `scope=it_assets_write_rest` presented to `hr_server` → 401 ERR-MCP-001 (aud mismatch).
- **Manual:** sign in as hr.admin, run the issue query, verify IS audit log shows the CIBA event with `scope=it_assets_write_rest` and `sub=hr.admin`.
