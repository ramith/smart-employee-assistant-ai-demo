# UC-15 — HR Admin queries pending leave requests as table

> **Build status: hybrid — backend existing (`hr_read_rest` scope, `get_all_leave_requests` service function, `get_leaves_for_dashboard` REST helper); GUI surface NEW. Sprint 4 task is the GUI table + thin REST adapter only.**
>
> The data layer for this UC was shipped in Sprint 1–2. `hr_server/service/hr_service.py` contains `get_all_leave_requests(status, employee_name)` (lines 148–168) and `get_leaves_for_dashboard(user_sub, status, employee_name)` (lines 274–299), both guarded by the `hr_read_rest` scope. The Sprint 4 new build is: (1) a REST reporting endpoint exposed by HR Server, and (2) a GUI table in the SPA's Reports page.

**Sprint:** 4
**Priority:** High
**Maps to N-tests:** TBD — Stage 10 (estimate: R4-4 pending-leaves table renders with seed data, R4-4b non-admin gets 403)
**Maps to scenarios:** Sprint 4 Act III (HR Admin reporting — first of three tables)

## Actors
- **Primary:** HR Admin user (`hr_admin_user` in dev)
- **Secondary:** SPA (Reports page), HR Server (REST reporting endpoint), WSO2 IS (token validation only — no CIBA)

## Preconditions
- UC-01 has succeeded with `hr_admin_user` signed in.
- `hr_admin_user`'s session token carries `hr_read_rest` scope (granted to `HR Admin` role, existing).
- HR Server exposes `GET /api/reports/leave-requests?status=pending` (new REST endpoint, Sprint 4 build). Response shape deferred to Stage 5 (API designer); recommended envelope: `{data: [{employee_username, employee_email, leave_type, days_requested, start_date}], count: N, filters: {status: "pending"}}`. **Identity surfaced as `username` + `email`, not `employee_id`** — see [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §7.
- At least 3 pending leave requests exist in `store.leave_requests` (via demo seed data or prior UC-13 submissions).

## No CIBA in this UC — design rationale

This UC does NOT use CIBA. The HR Admin is reading aggregate data (all pending leaves) rather than acting on behalf of a specific employee. The HR Admin's own session token (obtained at login, carrying `hr_read_rest`) is sufficient to authorise this read. CIBA is the mechanism for *user delegation* — granting an agent permission to act as a specific user. For system-level reads where the requester is the authorised HR Admin acting in their own capacity, the session token is the correct credential. This is standard OAuth API tiering: agent-initiated OBO tokens for delegated writes and per-user reads; direct service-token reads for HR Admin aggregate data.

This design choice is consistent with the PM brief `sprint-4-stage-1-product-review.md` §2 Act III and §8 recommendation: *"HR Admin's service-token suffices for reading all records."*

## Trigger
HR Admin navigates to SPA Reports page and clicks the "Pending Leaves" tab.

## Main flow
1. HR Admin navigates to the SPA's top-level "Reports" menu item (new navigation entry, Sprint 4 UX build per Stage 3).
2. SPA renders the Reports page with three tabs: "Pending Leaves" | "Cubicles" | "Devices". "Pending Leaves" is the active tab.
3. SPA issues `GET <orch>/api/reports/leave-requests?status=pending` with `Cookie: orchestrator_session=<sid>` (no Bearer token in the browser; orchestrator-proxied per [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §8).
4. Orchestrator looks up the session, asserts `Session.terminating == False`, reads `Session.token_a`. Pre-flight scope check: `hr_read_rest` must be in token-A claims. If not, 403.
5. Orchestrator forwards `GET /api/reports/leave-requests?status=pending` to `hr_server` with `Authorization: Bearer <token-A>`.
6. HR Server runs `validate_token` (F-04 + Step 7 denylist): sig, alg, iss, aud, exp, scope contains `hr_read_rest`, jti not in denylist. Calls `hr_service.get_leaves_for_dashboard(status="Pending")`.
7. HR Server returns `{data: [{employee_username, employee_email, leave_type, days_requested, start_date, request_id}], count: N}`.
8. Orchestrator passes the body through to SPA verbatim. SPA renders the table with columns: **Username** | **Email** | **Leave Type** | **Duration (days)** | **Start Date** | **Actions (Approve / Reject)**.
9. HR Admin reviews the table. Rows are sortable by Start Date (client-side sort, no server call).
10. **Approve / Reject buttons take the CIBA path, not the proxied-read path.** Click → SPA chat-driven invocation of HR Agent → HR Agent CIBA on `hr_approve_rest` → HR Admin approves the consent widget → HR Server `approve_leave_request` runs. After completion, table re-fetches the pending list. Same pattern as UC-07; not free of CIBA because writes-with-user-delegation should require explicit per-action consent.

> Approve/Reject is a write with user delegation (admin actions ON BEHALF OF the system, with explicit per-action consent for audit clarity). Stage 4 UX decides the exact button → consent-widget transition. The REST endpoints for approve/reject already exist at the service layer (`hr_service.approve_leave_request`, `hr_service.reject_leave_request`); only the chat plumbing + button affordance are new.

## Exception flows

### EX-1 — No pending leaves in the system
1. `get_all_leave_requests(status="Pending")` returns an empty list.
2. HR Server returns `{data: [], count: 0}`.
3. SPA renders: *"No pending leave requests."* (empty-state message within the table area).
4. Not an error condition.

### EX-2 — Non-admin user attempts to access the Reports page
1. `employee_user` navigates to `/reports` (either by URL or menu, if the menu item is not hidden for employees).
2. SPA should not render the Reports menu item for users without `hr_read_rest` scope. If the URL is accessed directly, the SPA checks the user's role/scope from the session and renders a 403 message: *"You do not have access to this page."*
3. Even if the SPA is bypassed, HR Server validates the token: Employee's session token does not carry `hr_read_rest`. HR Server returns 403.
4. No leave data is exposed. Defence-in-depth: SPA hides the route; HR Server enforces at the API level.

### EX-3 — HR Server unavailable
1. SPA `GET` to `hr_server/api/reports/leave-requests` returns HTTP 503 or connection error.
2. SPA renders: *"The HR system is unavailable. Try again in a moment."*

## Postconditions
- **Success:** table rendered with current pending leaves; no data mutation; HR Admin can make informed approval decisions.
- **Failure:** error message displayed; no data exposed to unauthorised users.

## Design notes for downstream stages

### UX (Stage 3)
- Placement: Option B from PM brief §8 — separate Reports page with tabs. This is the recommended choice for a professional executive demo. Stage 3 UX review should confirm and produce wireframes.
- Table columns for this tab: Employee Name, Leave Type, Duration (days), Start Date, Actions. No calendar visualization, no date-range picker, no bulk operations — see PM brief §11 R-4 mitigation.
- Approve/Reject buttons: Stage 3 to decide between inline row action vs modal dialog. Recommend inline to keep the demo snappy (PM brief §12 [UX] item 5).
- Reports page should only appear in the SPA navigation for users with the `HR Admin` role. `employee_user` should not see the Reports tab.
- Client-side sorting by Start Date: a simple table `<th>` click toggle is sufficient. No server-round-trip for sort.

### Architecture (Stage 6)
- New REST endpoint pair: orchestrator-side `GET /api/reports/leave-requests?status=pending` (cookie auth) and HR-Server-side `GET /api/reports/leave-requests?status=pending` (Bearer token-A auth, scope `hr_read_rest`). Orchestrator is the proxy. Per [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §8 — overrides PM brief §12 [ARCH] item 9 (which had recommended direct SPA → server; corrected to keep token-A out of the browser).
- HR Server reuses `hr_service.get_leaves_for_dashboard(status=status)` (existing — `hr_server/service/hr_service.py` lines 274–299). The REST adapter is a thin wrapper.
- Approve/Reject path is **CIBA-driven**, not REST. Triggered from the table buttons via the same chat plumbing as UC-07. Service logic (`approve_leave_request`, `reject_leave_request`) is unchanged.
- Identity surfaces: row data exposes `employee_username` + `employee_email`; never `sub`, never `employee_id`.

### Testing (Stages 10–11)
- **R-REPORTS-1** automated: `hr_admin_user` cookie session calls `GET /api/reports/leave-requests?status=pending` via orchestrator; response contains ≥ 3 rows (from seed data); columns match expected shape (username + email); HTTP 200.
- **R-REPORTS-4** automated: `employee_user` cookie session calls the same endpoint; orchestrator pre-flight scope check returns 403 before HR Server is contacted.
- **Manual (Stage 11):** sign in as `hr_admin_user`; navigate to Reports → Pending Leaves; confirm table renders with seed data; click Approve on one row; confirm consent widget appears; approve; confirm row status updates after re-fetch.
