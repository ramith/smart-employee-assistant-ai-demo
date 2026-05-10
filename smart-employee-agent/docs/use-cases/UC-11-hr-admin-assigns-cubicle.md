# UC-11 — HR Admin assigns cubicle to new hire (multi-turn)

**Sprint:** 4
**Priority:** Critical
**Build status:** NEW (data model + 4 MCP tools + 1 new scope + multi-turn chat orchestration)
**Maps to N-tests:** TBD — Stage 10 (estimate: R-CUBICLE-1 happy path, R-CUBICLE-2 read-side summary, R-CUBICLE-3 role-denial, R-CUBICLE-4 cross-scope replay block)
**Maps to scenarios:** Sprint 4 Act I (HR Admin onboarding workflow)

## Actors
- **Primary:** HR Admin user (`hr_admin_user` in dev)
- **Secondary:** SPA, Orchestrator, HR Agent, HR Server (MCP), WSO2 IS

## Preconditions
- UC-01 has succeeded with `hr_admin_user` as the signed-in user.
- `hr_admin_user` has the `HR Admin` role in IS, granting `hr_read_rest` (existing) + `hr_assets_write_rest` (NEW scope, Sprint 4).
- HR Server has the cubicle data model seeded: 100 cubicles distributed across **4 floors** (e.g. 25 per floor, IDs C-001..C-100). All start `occupied=False`. Stored in `hr_server/service/store.py`.
- HR Server MCP tools registered:
  - `get_cubicle_summary()` → guards `hr_read_rest`
  - `get_vacant_cubicles_on_floor(floor)` → guards `hr_read_rest`
  - `assign_cubicle(cubicle_id, employee_username, employee_email)` → guards `hr_assets_write_rest`
- HR Agent's OAuth App in IS is subscribed to the HR API resource with both `hr_read_rest` and `hr_assets_write_rest` scopes.
- The target employee (e.g. `jane.doe`) is registered in IS with `username` and `email` claims populated. (Identity model — see [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §7. No `employee_id` concept.)

## Trigger
HR Admin types in the SPA chat: *"Show me vacant cubicles."*

## Main flow (4 turns)

### Turn 1 — vacant summary

1. SPA `POST <orch>/api/chat` with `{session_id, user_message: "Show me vacant cubicles."}`.
2. Orchestrator routes (LLM or keyword-fallback) to `{agent_id: "hr_agent", tool: "get_cubicle_summary"}`.
3. Orchestrator emits SSE `{type: "routing", agent: "hr_agent"}`.
4. Orchestrator → HR Agent A2A. HR Agent dispatches the read tool. Required scope: `hr_read_rest` — already in token-A from login (HR Admin role); no CIBA needed.
5. HR Agent calls HR Server `GET /mcp/tools/get_cubicle_summary` with token-A as Bearer.
6. HR Server `validate_token` (F-04 + Step 7 denylist), runs `service.get_cubicle_summary()`, returns:
   ```
   {floor_1: {total: 25, vacant: 23},
    floor_2: {total: 25, vacant: 25},
    floor_3: {total: 25, vacant: 18},
    floor_4: {total: 25, vacant: 25}}
   ```
7. HR Agent → orchestrator → SPA via SSE `chat_message`. Reply:
   *"Vacant cubicles by floor: Floor 1 — 23 of 25, Floor 2 — 25 of 25, Floor 3 — 18 of 25, Floor 4 — 25 of 25. Which floor would you like to pick from?"*

### Turn 2 — admin selects floor

8. HR Admin: *"Show me floor 2."*
9. Orchestrator routes to `{agent_id: "hr_agent", tool: "get_vacant_cubicles_on_floor", args: {floor: 2}}`.
10. Same path as turn 1 — token-A scoped `hr_read_rest`, no CIBA.
11. HR Server returns:
    ```
    {floor: 2, vacant: ["C-026", "C-027", "C-028", ..., "C-050"]}
    ```
12. SSE `chat_message`: *"Floor 2 has 25 vacant cubicles available: C-026 through C-050. Which one would you like to assign, and to whom?"*

### Turn 3 — admin picks + names target employee

13. HR Admin: *"Assign C-027 to jane.doe."* (Or with email: *"Assign C-027 to jane.doe@example.com."*)
14. Orchestrator routes to `{agent_id: "hr_agent", tool: "assign_cubicle", args: {cubicle_id: "C-027", employee_username: "jane.doe"}}`.
15. HR Agent peeks the tool's required scope: `hr_assets_write_rest`. **CIBA is required** (admin-write with user delegation).
16. HR Agent before initiating CIBA, resolves the username → user record by calling `hr_server.lookup_employee("jane.doe")` (read-side helper, scope `hr_read_rest`). Returns `{username: "jane.doe", email: "jane.doe@example.com", sub: "<uuid>"}`. If no match, returns to step 17 EX-3.
17. HR Agent initiates CIBA:
    ```
    POST <IS>/oauth2/ciba
    scope=openid hr_assets_write_rest
    login_hint=<hr_admin_user UUID>
    binding_message="HR Agent wants to assign cubicle C-027 to jane.doe (corr-id: <rid>)"
    ```
18. IS evaluates `hr_admin_user`'s role: `HR Admin` grants `hr_assets_write_rest`. IS returns `{auth_req_id, auth_url, interval, expires_in}`.
19. HR Agent returns A2A `{type: "consent_required", auth_req_id, auth_url, agent_label: "HR Agent", action: "Assign cubicle C-027 to jane.doe"}`. Polling continues in background.
20. Orchestrator → SPA via SSE `ciba_url`. SPA renders Consent Widget (amber tint, write-scope visual convention from UC-07).
21. HR Admin clicks Approve → IS consent screen → admin confirms → IS issues token-C: `{sub=hr_admin_user UUID, act.sub=hr_agent-id, aud=<hr_agent OAuth Client ID>, scope=openid hr_assets_write_rest, exp=now+3600}`.
22. HR Agent's poll succeeds; calls HR Server MCP tool `assign_cubicle({cubicle_id: "C-027", employee_username: "jane.doe", employee_email: "jane.doe@example.com"})` with `Authorization: Bearer <token-C>`.

### Turn 4 — confirmation

23. HR Server validates token-C: `aud`, `act.sub`, `scope` contains `hr_assets_write_rest`. Re-checks cubicle C-027 occupancy. If still vacant, sets `occupied=True`, `assigned_to_username="jane.doe"`, `assigned_to_email="jane.doe@example.com"`, `assigned_to_sub="<uuid>"`, `assigned_at=<ISO 8601>`.
24. HR Server returns `{success: true, cubicle_id: "C-027", floor: 2, assigned_to: {username: "jane.doe", email: "jane.doe@example.com"}, assigned_at: "<ts>"}`.
25. HR Agent returns A2A result. Orchestrator composes reply: *"Cubicle C-027 on floor 2 has been assigned to jane.doe (jane.doe@example.com)."*
26. SSE delivers reply. SPA chat surface shows confirmation. (No automatic Reports page navigation; HR Admin can manually navigate to verify the new row.)

## Exception flows

### EX-1 — Cubicle already occupied (TOCTOU)
1. Between Turn 2 (vacant list) and Turn 3 (assign), another admin race-assigns C-027.
2. At step 23, HR Server finds `occupied=True`. Returns `{error: "cubicle_already_occupied", current_holder: {username: "bob.smith", email: "bob.smith@example.com"}}`.
3. Orchestrator: *"Cubicle C-027 was just assigned to bob.smith. Please pick a different one."*
4. No state change beyond what the other admin's call did.

### EX-2 — HR Admin denies the CIBA consent
1. At step 21, HR Admin clicks Deny (or closes the tab without approving).
2. IS returns `access_denied` on the CIBA poll.
3. HR Agent catches `ERR-CIBA-005`; returns A2A error.
4. Orchestrator: *"Cubicle assignment was not authorised. No change was made."*
5. SPA Consent Widget transitions to DENIED state.

### EX-3 — Employee lookup fails
1. At step 16, `hr_server.lookup_employee("jane.doe")` returns `{found: false}` (typo, or user not in IS).
2. HR Agent returns A2A error before CIBA is initiated. (No wasted IS interaction.)
3. Orchestrator: *"I couldn't find an employee with username 'jane.doe'. Please check the username or provide an email."*

### EX-4 — Employee (non-admin) attempts cubicle assignment
1. `employee_user` types "Assign cubicle C-027 to bob.smith."
2. HR Agent initiates CIBA on `hr_assets_write_rest` with `login_hint=<employee_user UUID>`.
3. IS evaluates: Employee role does NOT grant `hr_assets_write_rest`. IS returns `invalid_scope` (or `access_denied` at consent screen — both verified by R-CUBICLE-3).
4. HR Agent classifies as `ERR-CIBA-003`.
5. Orchestrator: *"You don't have permission to assign cubicles. This action requires the HR Admin role."*
6. No cubicle is assigned.

### EX-5 — CIBA window expires
1. HR Admin doesn't act on the consent URL within 300 seconds.
2. IS returns `expired_token` on the CIBA poll.
3. HR Agent returns A2A `{reason: "consent_window_expired"}`.
4. Orchestrator: *"The authorisation window expired. Please ask again to retry the cubicle assignment."*

### EX-6 — Read-side fails (turn 1 or turn 2)
1. HR Server returns 5xx (e.g. transient).
2. HR Agent surfaces error → orchestrator: *"I couldn't fetch the cubicle list. Please try again."*
3. No CIBA was initiated; no state.

## Postconditions
- **Success:** cubicle record updated in HR Server in-memory store (`occupied=True`, `assigned_to_username`, `assigned_to_email`, `assigned_to_sub`, `assigned_at`). Token-C jti recorded in orchestrator session map. IS audit log shows the CIBA event with `scope=hr_assets_write_rest`. The new assignment is visible in UC-16 Cubicles tab on next load.
- **Failure (any EX flow):** no cubicle record changed; orchestrator session map unchanged for this tool call; the error message is shown in SPA chat.

## Design notes for downstream stages

### UX (Stage 4)
- Multi-turn chat is plain chat — no widgets between turns 1 and 2. Orchestrator's reply text drives the prompt.
- Consent Widget appears only at turn 3 (when `hr_assets_write_rest` CIBA fires). Action text: *"Assign cubicle C-027 to jane.doe."* — admin-verb form, amber tint.
- No separate confirmation dialog post-assignment; orchestrator's text reply is the confirmation surface.
- Empty-state: if all 4 floors are full at turn 1, reply: *"All 100 cubicles are currently assigned. You'll need to reassign one before adding a new hire."* (Cubicle reassign is **out of scope for Sprint 4** — see `sprint-4.md` §5.)

### Architecture (Stage 6)
- Cubicle data model in `hr_server/service/store.py` (NEW list `cubicles`):
  ```
  {
    cubicle_id: "C-027",
    floor: 2,
    occupied: bool,
    assigned_to_username: str | None,
    assigned_to_email: str | None,
    assigned_to_sub: str | None,    # internal join only; never displayed
    assigned_at: ISO8601 | None,
  }
  ```
  Seed 100 cubicles across 4 floors at module import (mirror `_SEED_ASSETS` shape from `it_server`).
- New service functions:
  - `hr_service.get_cubicle_summary()` — group by floor, count vacant.
  - `hr_service.get_vacant_cubicles_on_floor(floor: int)` — filter `occupied=False AND floor=N`.
  - `hr_service.assign_cubicle(cubicle_id, employee_username, employee_email, sub)` — validates inputs, checks occupancy, mutates store. Returns `{success, ...}` or `{error, current_holder}`.
  - `hr_service.lookup_employee(username_or_email: str)` — returns `{found: bool, username, email, sub}`. Pre-seeded user table; for the demo, `employee_user` and `hr_admin_user` plus 1–2 "new hire" stubs.
- `hr_assets_write_rest` is a new IS scope — registered as an HR API resource scope and granted to the HR Admin role before Stage 11. Pre-flight via `scripts/check-is-config.sh` (Stage 9 deliverable).
- Idempotency rule: `assign_cubicle` of the same `(cubicle_id, employee_username)` twice → second call returns the existing record with `success=true` (no error). Different employee → `cubicle_already_occupied`.

### Testing (Stages 10–11)
- **R-CUBICLE-1** automated: `hr_admin_user` triggers full 4-turn flow; final assign p95 ≤ 2 s; HR Server reflects the new assignment.
- **R-CUBICLE-2** automated: `hr_admin_user` triggers turns 1–2 only (read-side); validates summary and floor list shapes.
- **R-CUBICLE-3** automated: `employee_user` attempts turn 3 assign; IS denies; ERR-CIBA-003 emitted; no HR Server mutation.
- **R-CUBICLE-4** automated: token-C captured at step 21 cannot be replayed against `get_all_leave_requests` (different scope); 401 ERR-MCP-003.
- **Manual (Stage 11):** sign in as `hr_admin_user`; run all 4 turns; verify IS audit log shows the CIBA event; verify HR Server in-memory store; verify the row appears in UC-16 Cubicles tab.
