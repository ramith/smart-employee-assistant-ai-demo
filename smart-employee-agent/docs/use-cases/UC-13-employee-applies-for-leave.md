# UC-13 — Employee applies for leave

> **Build status (revised after Stage 3 user back-track 2026-05-10):** **hybrid — backend existing (`hr_service.apply_leave` shipped Sprint 1–2); SPA "My Leaves" panel is NEW in Sprint 4.**
>
> Sprint 4 keeps the chat path unchanged but adds a visible UI surface so the employee sees their applied leaves on the home page without having to ask the agent. After applying via chat, the panel re-fetches and shows the new request without page reload. See [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §2 items 6 + 10.

**Sprint:** 1 (chat path built), 4 (My Leaves panel + documentation)
**Priority:** High
**Maps to N-tests:** N1, N4, N7 (from UC-02, reused for chat path); R-LEAVE-1 + R-PANEL-1 (Sprint 4 panel work)
**Maps to scenarios:** Sprint 4 home page surfaces (My Leaves panel below chat) + chat path for natural-language flow

## Actors
- **Primary:** Employee user (`employee_user` in dev)
- **Secondary:** SPA, Orchestrator, HR Agent, HR Server (MCP), WSO2 IS

## Preconditions
- UC-01 has succeeded with `employee_user` signed in.
- `employee_user` has the `Employee` role in IS, granting `hr_self_rest` (existing, deployed Sprint 1).
- HR Server `apply_leave` MCP tool is registered, guarded by `hr_self_rest`. Implemented in `hr_server/service/hr_service.py` `apply_leave()` (lines 72–143).
- Employee has sufficient leave balance for the requested leave type and duration.

## Trigger
Employee types in the SPA chat: *"I'd like to take annual leave from June 10 to June 14."*

## Main flow
1. SPA `POST <orch>/api/chat` with `{session_id, user_message}`.
2. Orchestrator's LLM routes to HR Agent, tool `apply_leave`, args `{leave_type: "Annual Leave", start_date: "2026-06-10", end_date: "2026-06-14", reason: "<optional>"}`.
3. Orchestrator emits SSE `{type: "routing", agent: "hr_agent"}`.
4. Orchestrator `POST <hr_agent>/a2a/message/send` with `Authorization: Bearer <token-A>`.
5. HR Agent validates token-A; extracts `user_sub = employee_user UUID`.
6. HR Agent initiates CIBA: `scope=openid hr_self_rest`, `login_hint=<employee_user UUID>`, `binding_message="HR Agent wants to apply for leave on your behalf (<corr-id>)"`.
7. IS evaluates `employee_user`'s role: `Employee` grants `hr_self_rest`. IS returns `{auth_req_id, auth_url}`.
8. SPA renders Consent Widget: action text *"Apply for leave on your behalf"* (neutral tint, self-service write).
9. Employee clicks Approve; IS consent screen confirms; IS issues token-C with `scope=openid hr_self_rest`.
10. HR Agent calls HR Server `apply_leave({sub, first_name, last_name, leave_type, start_date, end_date, reason})` with `Authorization: Bearer <token-C>`.
11. HR Server validates token-C (`aud`, `act.sub`, `scope` contains `hr_self_rest`). Calls `hr_service.apply_leave(...)`.
12. `apply_leave()` validates leave type against `store.leave_policy`, validates dates (YYYY-MM-DD format, end >= start), checks minimum notice period, checks leave balance. Creates a new leave request record in `store.leave_requests` with `status="Pending"`. Returns `{success: true, request_id: "LR-NNN"}`.
13. HR Agent returns A2A result. Orchestrator LLM composes: *"Your Annual Leave request from June 10 to June 14 (5 days) has been submitted and is pending approval. Request ID: LR-NNN."*

## Exception flows

### EX-1 — Invalid leave type
1. Employee specifies a leave type not in `store.leave_policy` (e.g., "Study Leave").
2. `hr_service.apply_leave()` returns `{error: "invalid_leave_type", message: "...Valid types: Annual Leave, Sick Leave, Personal Leave"}`.
3. Orchestrator: *"That leave type is not valid. Available types are: Annual Leave, Sick Leave, Personal Leave."*
4. Implemented and shipped in Sprint 1. No new code required.

### EX-2 — Insufficient leave balance
1. Employee requests more days than their remaining balance for the leave type.
2. `hr_service.apply_leave()` returns `{error: "insufficient_balance", message: "You only have N days remaining..."}`.
3. Orchestrator surfaces the balance shortfall to the employee.
4. Implemented and shipped in Sprint 1. No new code required.

### EX-3 — Insufficient notice period
1. Employee requests leave starting sooner than the minimum notice required for the leave type (e.g., Annual Leave requires 7 days notice; employee requests leave starting tomorrow).
2. `hr_service.apply_leave()` returns `{error: "insufficient_notice", message: "Annual Leave requires at least 7 days notice..."}`.
3. Orchestrator advises the employee to choose a later start date.
4. Implemented and shipped in Sprint 1. No new code required.

### EX-4 — Employee denies CIBA consent
1. Employee clicks Deny on the consent widget.
2. IS returns `access_denied`. HR Agent emits `ERR-CIBA-005`.
3. Orchestrator: *"Leave request was not submitted (you declined the authorisation)."*
4. No leave record created.

### EX-5 — Invalid date format
1. Employee provides dates in an unrecognised format (e.g., "June 10th").
2. LLM should normalise the date before passing to the tool; if it cannot, `hr_service.apply_leave()` returns `{error: "invalid_dates", message: "Dates must be in YYYY-MM-DD format."}`.
3. Orchestrator asks the employee to clarify the dates.

## Postconditions
- **Success:** new record in `store.leave_requests` with `status="Pending"`; employee can query status via UC-14; HR Admin sees the request in UC-15 pending-leaves table; IS audit log records the CIBA event.
- **Failure:** no leave record created; employee can retry.

## Sprint 4 — NEW My Leaves panel (UI surface)

The chat-based apply flow above is unchanged from Sprint 1. What's new in Sprint 4 is the **My Leaves panel** on the SPA home page, which surfaces the employee's leaves visually without requiring them to ask the agent. This addresses the user's feedback at Stage 3 review: *"applying leave may be implemented but it's not visible in the UX."*

### Panel data flow
1. SPA on first paint after login → `GET /api/me/leaves` (orchestrator-proxied per [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §8).
2. Orchestrator session lookup → token-A → forwards as Bearer to `hr_server`.
3. `hr_server` validates `hr_self_rest` scope on token-A → calls existing `hr_service.get_my_leave_requests(sub)` → returns leave list.
4. Orchestrator passes the body through. SPA renders sortable table (id, type, start, end, days, status).
5. After a successful chat-based apply (step 13 of Main flow above), SPA re-fetches `/api/me/leaves` on the `chat_message` SSE settle → table updates without page reload.

### Panel visibility
- Both `Employee` and `HR Admin` roles see the panel (HR Admin sees only their *own* leaves, not all employees').
- Empty state: *"You have no leave requests yet. Ask the HR agent to apply for one."*

## Sprint 4 verification task

During Stage 11 (manual testing), the test operator must:
1. Sign in as `employee_user`. Confirm My Leaves panel renders (possibly empty initially).
2. Submit a leave request via chat for a future date with sufficient balance. Confirm chat returns success.
3. Without refreshing the page, confirm the new leave appears in the My Leaves panel.
4. Confirm the IS audit log records the CIBA event with `scope=hr_self_rest` and `sub=employee_user UUID`.
5. Confirm this flow is unaffected by any other Sprint 4 build changes (regression check).

## Design notes for downstream stages

### UX (Stage 4)
- My Leaves panel placement: recommend a card below the chat input on the home page (visible without scrolling). Stage 4 wireframes confirm.
- Empty state copy: see above.
- Sort order on first paint: `start_date` descending (most recent first).
- Status pill colour mapping: Pending = neutral, Approved = green, Rejected = red.

### Architecture (Stage 6)
- New REST endpoint on orchestrator: `GET /api/me/leaves` — proxies to `hr_server`.
- New REST endpoint on `hr_server`: `GET /api/me/leaves` — calls existing `hr_service.get_my_leave_requests(sub)`. Scope guard: `hr_self_rest`.
- Apply-leave chat path is unchanged (no new code in `hr_service.apply_leave`).

### Testing (Stages 10–11)
- **R-LEAVE-1** automated: chat-based apply succeeds; HR Server in-memory store reflects the new leave.
- **R-PANEL-1** automated: panel fetch returns the caller's leave list; non-self leaves are excluded.
- Existing N-tests from UC-02 still cover the chat path. No new tests needed there.
