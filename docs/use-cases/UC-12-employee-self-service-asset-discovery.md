# UC-12 — Employee self-service asset discovery

**Sprint:** 4
**Priority:** Critical
**Build status:** NEW (1 new HR MCP tool + 1 new IT MCP tool + 1 new scope `it_assets_self_rest`).
**Maps to N-tests:** TBD — Stage 10 (estimate: R-SELF-1 happy path dual-agent, R-SELF-1a cubicle not assigned, R-SELF-1b laptop not assigned, R-SELF-2 employee denies IT consent)
**Maps to scenarios:** Sprint 4 Act II (self-service asset discovery)

## Actors
- **Primary:** Employee user (`employee_user` in dev)
- **Secondary:** SPA, Orchestrator, HR Agent, IT Agent, HR Server (MCP), IT Server (MCP), WSO2 IS

## Preconditions
- UC-01 has succeeded with `employee_user` as the signed-in user.
- `employee_user` has the `Employee` role in IS, granting `hr_self_rest` (existing) and `it_assets_self_rest` (new scope, Sprint 4).
- HR Server has a cubicle record assigning `employee_user` to a cubicle (set up via UC-11 or via seed data for demo).
- IT Server has at least one outstanding asset assigned to `employee_user`'s `username` in `it_server/service/store.py` seed data (`_SEED_ASSETS` rewritten in Sprint 4 to key by `username`, no `employee_id` field — see [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §7).
- HR Server MCP tool `get_my_cubicle` is registered, guarded by `hr_self_rest`.
- IT Server MCP tool `get_my_assets` is registered, guarded by `it_assets_self_rest` (new scope, Sprint 4).
- Both agents' OAuth Apps in IS are subscribed to the respective API resources with the relevant scopes.

## Fan-out pattern decision: serial

This UC uses **serial** fan-out (HR leg first, then IT leg), consistent with the established pattern from UC-03. Rationale: the CIBA consent widget is a single-at-a-time surface (Council decision Q2, confirmed in UC-03 design notes). Showing two simultaneous consent widgets would confuse the demo audience and violate the existing UX contract. Serial fan-out also makes the demo narrative cleaner: the employee first discovers their workspace location, then their equipment.

## Trigger
Employee types one or both of:
- *"Where is my cubicle?"* (HR leg only)
- *"What laptop do I have?"* (IT leg only)
- *"Where is my cubicle and what laptop do I have?"* (full dual-agent path, the canonical demo query)

## Main flow (canonical dual-agent path)
1. SPA `POST <orch>/api/chat` with `{session_id, user_message: "Where is my cubicle and what laptop do I have?"}`.
2. Orchestrator's LLM produces a two-step tool-call plan: `[{agent_id: "hr_agent", tool: "get_my_cubicle"}, {agent_id: "it_agent", tool: "get_my_assets"}]`.
3. **--- HR leg begins ---**
4. Orchestrator emits SSE `{type: "routing", agent: "hr_agent"}`. SPA chat shows: *"Routing to HR Agent..."*
5. Orchestrator `POST <hr_agent>/a2a/message/send` with `Authorization: Bearer <token-A>`.
6. HR Agent validates token-A (signature, `iss`, `aud`, `act.sub` allowlist). Extracts `user_sub = employee_user UUID`.
7. HR Agent initiates CIBA: `scope=openid hr_self_rest`, `login_hint=<employee_user UUID>`, `binding_message="HR Agent wants to look up your cubicle assignment (<corr-id>)"`.
8. IS evaluates `employee_user`'s role: `Employee` grants `hr_self_rest`. IS returns `{auth_req_id, auth_url, interval=2, expires_in=300}`.
9. HR Agent returns A2A `{type: "consent_required", auth_url, action: "View your cubicle assignment"}`.
10. SPA renders Consent Widget: *"View your cubicle assignment"* (neutral tint, read scope).
11. Employee clicks Approve. Browser opens `auth_url`. Employee confirms in IS consent screen.
12. HR Agent polls; receives token-C-HR: `{sub=employee_user UUID, act.sub=hr_agent-id, scope=openid hr_self_rest}`.
13. HR Agent calls HR Server `get_my_cubicle({user_sub: <employee_user UUID>})` with `Authorization: Bearer <token-C-HR>`.
14. HR Server validates token-C-HR, calls `hr_service.get_my_cubicle(user_sub)`. Returns `{cubicle_id: "C-045", floor: 2, assigned_at: "<ts>"}`.
15. HR Agent returns A2A result. Orchestrator holds the HR result; emits SSE with partial answer: *"Your cubicle is C-045 on floor 2."*
16. **--- IT leg begins ---**
17. Orchestrator emits SSE `{type: "routing", agent: "it_agent"}`. SPA chat shows: *"Now routing to IT Agent..."*
18. Orchestrator `POST <it_agent>/a2a/message/send` with `Authorization: Bearer <token-A>`.
19. IT Agent validates token-A (same checks as HR Agent step 6).
20. IT Agent initiates CIBA: `scope=openid it_assets_self_rest`, `login_hint=<employee_user UUID>`, `binding_message="IT Agent wants to look up your assigned devices (<corr-id>)"`.
21. IS evaluates `employee_user`'s role: `Employee` grants `it_assets_self_rest`. IS returns `{auth_req_id, auth_url, interval=2, expires_in=300}`.
22. IT Agent returns A2A `{type: "consent_required", auth_url, action: "View your assigned IT equipment"}`.
23. SPA renders second Consent Widget: *"View your assigned IT equipment"* (neutral tint, read scope). The HR widget has already completed and collapsed.
24. Employee clicks Approve. Browser opens `auth_url`. Employee confirms.
25. IT Agent polls; receives token-C-IT: `{sub=employee_user UUID, act.sub=it_agent-id, scope=openid it_assets_self_rest}`.
26. IT Agent calls IT Server `get_my_assets()` with `Authorization: Bearer <token-C-IT>`. (No employee identifier in args — IT Server derives the caller's `username` from token claims and looks up that user's assets.)
27. IT Server validates token-C-IT (`aud`, `act.sub`, `scope` contains `it_assets_self_rest`). Reads `username` from the token claim set; calls `it_service.get_my_assets(username)`. Returns `{assets: [{asset_id: "AST-12345", type: "laptop", model: "MBP 14 M3", status: "outstanding"}], total: 1}`.
28. IT Agent returns A2A result to orchestrator.
29. Orchestrator LLM composes final combined reply from both results: *"Your cubicle is C-045 on floor 2. Your assigned laptop is a MacBook Pro 14 M3 (AST-12345), currently outstanding."*
30. SSE delivers the combined reply to SPA chat view.

> Note on `it_assets_self_rest`: this is the first UC in the demo that gates an IT Server tool behind `it_assets_self_rest` rather than `it_assets_read_rest`. The distinction enforces least privilege: `it_assets_read_rest` is the admin-grade scope used for the Devices reporting table (UC-16) where any user's record can be listed; `it_assets_self_rest` restricts the query to the authenticated employee's own records. IT Server's `get_my_assets` tool must enforce this by reading `username` from the OBO token claim set and scoping the lookup to that user only. **No `employee_id` indirection** — Sprint 4 keys IT asset records directly by `username` in the rewritten seed data (`it_server/service/store.py` `_SEED_ASSETS` list).

## Exception flows

### EX-1 — Employee not assigned a cubicle yet
1. At step 14, HR Server's `get_my_cubicle` finds no cubicle assigned to `employee_user UUID`.
2. HR Server returns `{assigned: false}`.
3. Orchestrator LLM composes: *"You have not been assigned a cubicle yet. Contact HR to arrange one."*
4. IT leg proceeds normally regardless (the two queries are independent).

### EX-2 — Employee not assigned any IT assets
1. At step 27, IT Server's `get_my_assets` finds no assets keyed by the caller's `username`.
2. IT Server returns `{assets: [], total: 0}`.
3. Orchestrator LLM composes: *"No IT assets are currently assigned to you. Contact IT to request equipment."*

### EX-3 — Employee denies HR consent
1. At step 11, employee clicks Deny on the HR consent.
2. IS returns `access_denied`. HR Agent emits `ERR-CIBA-005`.
3. Orchestrator skips the HR result but still proceeds to the IT leg.
4. Final reply: *"I couldn't retrieve your cubicle assignment (you declined the authorisation). Your assigned laptop is a MacBook Pro 14 M3 (AST-12345)."*
5. If employee denies both consents: *"Both lookups were declined. Please ask again when you're ready to authorise."*

### EX-4 — Employee denies IT consent
1. At step 24, employee clicks Deny on the IT consent.
2. IS returns `access_denied`. IT Agent emits `ERR-CIBA-005`.
3. Orchestrator composes: *"Your cubicle is C-045 on floor 2. I couldn't retrieve your IT assets (you declined the authorisation)."*

### EX-5 — IT Agent not subscribed to `it_assets_self_rest` (regression check)
1. IT Agent's OAuth App in IS is not subscribed to `it_assets_self_rest`.
2. At step 21, CIBA initiation with `scope=it_assets_self_rest` returns `unauthorized_client` or `invalid_scope`.
3. IT Agent classifies as `ERR-CIBA-003`.
4. Orchestrator: *"The IT system is misconfigured and could not look up your assets. Contact admin."*
5. This scenario is the regression guard that confirms `it_assets_self_rest` was correctly provisioned in IS. Test R4-3c covers this path.

## Postconditions
- **Success:** chat shows combined reply with cubicle and IT asset details; orchestrator session map has two records: `(hr_agent, jti_c_hr, scope=hr_self_rest)` and `(it_agent, jti_c_it, scope=it_assets_self_rest)`; IS audit log shows two separate CIBA events attributed to `employee_user`.
- **Partial:** one result present, one missing; chat reflects what was retrieved and why the other was unavailable.
- **Full denial:** no asset data retrieved; both widgets shown as DENIED; no new session map records.

## Design notes for downstream stages

### UX (Stage 3)
- Two sequential consent widgets, same as UC-03. Widget 1 (HR) collapses to a "Completed" state before Widget 2 (IT) appears. Never display both simultaneously (Q2 lock).
- Transition banner between widgets: *"Now routing to IT Agent..."* (mirrors UC-03 Step 4 pattern).
- Partial results should render progressively: show cubicle result in chat as soon as the HR leg completes; IT result appends to the same message or follows as a second message. Stage 3 decides the exact layout.
- Consent Widget action text per scope: `hr_self_rest` = *"View your cubicle assignment"*; `it_assets_self_rest` = *"View your assigned IT equipment"*.

### Architecture (Stage 6)
- New HR Server MCP tool `get_my_cubicle()`: scope guard `hr_self_rest`. Reads caller's `username` from token claims; returns `{cubicle_id, floor, assigned_at}` or `{assigned: false}`. New service function `hr_service.get_my_cubicle(username)` reads from `store.cubicles` filtered by `assigned_to_username`.
- New IT Server MCP tool `get_my_assets()`: scope guard `it_assets_self_rest`. Reads caller's `username` from token claims; returns assets keyed by that username. Service function: new `it_service.get_my_assets(username)`. Existing `it_service.get_employee_assets(employee_id)` is removed in Sprint 4 along with the legacy `employee_id` field.
- `it_assets_self_rest` scope registration in IS: must be added as an HR/IT API resource scope, granted to both `Employee` and `HR Admin` roles. See [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §6 scope table.
- **No sub → employee_id resolution.** Sprint 4 drops the legacy `employee_id` concept; IT seed data is rekeyed by `username`. See [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §7. The `username` claim must be present in IS-issued access tokens (operator action A3 in sprint-4.md §3 verifies this).
- Token-C-HR and token-C-IT are independent tokens with different `aud` (HR Agent client ID vs IT Agent client ID). Replay of token-C-HR at IT Server must fail (`aud` mismatch — covered by existing ERR-MCP-001 guard).

### Testing (Stages 10–11)
- **R-SELF-1** automated: `employee_user` dual-agent happy path; HR leg returns cubicle; IT leg returns assets; both within 1 second per agent.
- **R-SELF-1a** automated: `employee_user` with no cubicle assigned; HR Server returns `{assigned: false}`; orchestrator surfaces the "not yet assigned" message gracefully.
- **R-SELF-1b** automated: `employee_user` with no IT assets; IT Server returns `{assets: [], total: 0}`; orchestrator surfaces the "no assets" message.
- **R-SELF-2** automated (regression): IT Agent initiates CIBA for `it_assets_self_rest` with IS misconfigured (scope not subscribed); CIBA returns `invalid_scope`; ERR-CIBA-003 emitted; IT Server never reached.
- **Manual (Stage 11):** sign in as `employee_user`; run the dual query; verify IS audit log shows two CIBA events; verify `it_assets_self_rest` scope appears in IS audit for the IT leg.
