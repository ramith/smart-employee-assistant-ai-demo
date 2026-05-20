# UC-14 — Employee checks status of own leave requests

> **Build status (revised after Stage 3 user back-track 2026-05-10):** **hybrid — backend existing (`hr_service.get_my_leave_requests` shipped Sprint 1); SPA "My Leaves" panel is NEW in Sprint 4 (shared with UC-13).**
>
> Sprint 4 keeps the chat path unchanged. The new visible surface is the My Leaves panel on the SPA home page (defined in UC-13). When the employee asks the chat agent "what's the status of my leaves," the agent's reply will match the panel's content.

**Sprint:** 1 (chat path), 4 (My Leaves panel via UC-13)
**Priority:** High
**Maps to N-tests:** N1, N4, N7 (from UC-02, reused for chat); R-LEAVE-2 + R-PANEL-1 (Sprint 4 — panel + chat consistency)
**Maps to scenarios:** Sprint 4 home page surfaces (My Leaves panel) + chat path

## Actors
- **Primary:** Employee user (`employee_user` in dev)
- **Secondary:** SPA, Orchestrator, HR Agent, HR Server (MCP), WSO2 IS

## Preconditions
- UC-01 has succeeded with `employee_user` signed in.
- `employee_user` has `hr_self_rest` scope (granted to `Employee` role, deployed Sprint 1).
- At least one leave request exists for `employee_user` in `store.leave_requests` (submitted via UC-13 or seeded).
- HR Server MCP tool `get_my_leave_requests` is registered, guarded by `hr_self_rest`. Implemented in `hr_server/service/hr_service.py` `get_my_leave_requests()` (lines 54–69).

## Trigger
Employee types in the SPA chat: *"What's the status of my leave requests?"* or *"Did my leave get approved?"*

## Main flow
1. SPA `POST <orch>/api/chat` with `{session_id, user_message}`.
2. Orchestrator's LLM routes to HR Agent, tool `get_my_leave_requests`, args `{}`.
3. Orchestrator emits SSE `{type: "routing", agent: "hr_agent"}`.
4. Orchestrator `POST <hr_agent>/a2a/message/send` with `Authorization: Bearer <token-A>`.
5. HR Agent validates token-A; extracts `user_sub = employee_user UUID`.
6. HR Agent initiates CIBA: `scope=openid hr_self_rest`, `login_hint=<employee_user UUID>`, `binding_message="HR Agent wants to retrieve your leave request history (<corr-id>)"`.
7. IS evaluates `employee_user`'s role: `Employee` grants `hr_self_rest`. IS returns `{auth_req_id, auth_url}`.
8. SPA renders Consent Widget: action text *"View your leave requests"* (neutral tint, read scope).
9. Employee clicks Approve; IS consent screen confirms; IS issues token-C with `scope=openid hr_self_rest`.
10. HR Agent calls HR Server `get_my_leave_requests({sub, first_name, last_name})` with `Authorization: Bearer <token-C>`.
11. HR Server validates token-C (`aud`, `act.sub`, `scope` contains `hr_self_rest`). Calls `hr_service.get_my_leave_requests(sub)`.
12. `get_my_leave_requests(sub)` filters `store.leave_requests` by `user_sub == sub`. Returns a list of `{request_id, type, start_date, end_date, days_requested, status, reason}` for each matching record.
13. HR Agent returns A2A result. Orchestrator LLM composes a readable summary, e.g.: *"You have 2 leave requests: Annual Leave (June 10–14, 5 days) — Pending; Sick Leave (May 2–3, 2 days) — Approved."*

## Exception flows

### EX-1 — No leave requests on record
1. `get_my_leave_requests(sub)` finds no records for `employee_user`.
2. Returns an empty list `[]`.
3. Orchestrator: *"You have no leave requests on record."*
4. No error; clean empty state.

### EX-2 — Employee denies CIBA consent
1. Employee clicks Deny on the consent widget.
2. IS returns `access_denied`. HR Agent emits `ERR-CIBA-005`.
3. Orchestrator: *"I couldn't retrieve your leave requests (you declined the authorisation)."*

### EX-3 — Token-C reused across users (security boundary check)
1. An `employee_user` token-C with `scope=hr_self_rest` is presented to HR Server by a different agent or caller.
2. HR Server's `get_my_leave_requests` uses the `sub` embedded in the token to filter records — it only returns that employee's records regardless of who calls the endpoint.
3. This is an existing design property, not a new Sprint 4 guard. Documented here for clarity.

## Postconditions
- **Success:** employee sees their leave requests with current status; no state changed; IS audit log records the CIBA event.
- **Failure:** empty reply or error message; no state changed.

## Relationship to UC-13

UC-13 (apply for leave) and UC-14 (check status) form a natural pair. The demo runbook should show them in sequence: employee applies for leave → employee checks the resulting "Pending" status → HR Admin (in UC-15) sees the same request in the pending-leaves table. This narrative thread ties Acts 0 through III.

## Sprint 4 — relationship to the My Leaves panel

The My Leaves panel (defined in UC-13 §"NEW My Leaves panel") is the visual / always-on surface of the same data this UC's chat path returns. They MUST agree:
- Same backend (`hr_service.get_my_leave_requests`).
- Same scope (`hr_self_rest`).
- Same envelope shape (locked at Stage 5).

Use the My Leaves panel as the demo's quick-glance surface; use the chat path for natural-language queries ("which of my leaves are pending?") that benefit from LLM-composed prose answers.

## Sprint 4 verification task

During Stage 11 (manual testing), the test operator must:
1. Sign in as `employee_user`. Confirm My Leaves panel renders.
2. Query leave status via chat. Confirm the response lists the same records the panel shows.
3. Confirm IS audit log records the CIBA event for the chat path with `scope=hr_self_rest`.
4. Confirm flow is unaffected by any Sprint 4 build changes (regression check — specifically, confirm that adding `get_my_cubicle` to `hr_self_rest` tooling does not break the existing `get_my_leave_requests` path).

## Design notes for downstream stages

### UX (Stage 4)
- No new UX work specific to UC-14 beyond what UC-13 specifies for the panel. Chat rendering was established in Sprint 1.

### Architecture (Stage 6)
- No backend changes. Logic remains in `hr_service.get_my_leave_requests()`.
- Adding `get_my_cubicle` as a new tool under `hr_self_rest` does NOT affect this UC — each tool call triggers its own CIBA (or reuses an active token-C if the agent caches). Stage 6 confirms.

### Testing (Stages 10–11)
- **R-LEAVE-2** automated: chat path returns the same records as the My Leaves panel for the same user. Cross-surface consistency check.
- Existing N-tests from UC-02 still cover the chat path.
