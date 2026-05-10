# Sprint 4 — Stage 1: Product Team Review

> **⚠ Historical for scope/identity decisions — superseded by Stage 3 ([`sprint-4.md`](sprint-4.md)) on 2026-05-10.**
>
> Stage 3 user back-track changed five things this brief got wrong:
> 1. **Identity model:** no `employee_id` concept; `username` + `email` only.
> 2. **Cubicles:** **4 floors** (not 3); multi-turn discovery flow (vacant summary → floor pick → vacant list → assign).
> 3. **Scope name:** the new IT self-service scope is `it_assets_self_rest` (not `it_self_rest` — preserves `_rest` suffix from `docs/scope-policy.md`).
> 4. **No new `it_read_assets` scope** — UC-16 reuses the existing `it_assets_read_rest`.
> 5. **UC-13 / UC-14 are hybrid (backend existing + NEW SPA "My Leaves" panel)** — not "verify-existing only" as this brief framed them.
>
> Use this brief for narrative shaping (Acts I–III, demo-runbook framing, out-of-scope rationale, risks). For binding scope / identity / build-status decisions, read `sprint-4.md` first.

**Live status (2026-05-10 evening):** M3 sign-off complete on `sprint-3-build`. Introspection/IS consent deferral resolved; Sprint 3 close-out carry-overs identified in [`sprint-3-signoff.md`](sprint-3-signoff.md). User has drafted a business-use-case pivot away from infrastructure hardening toward business-domain richness. This review curates and sizes that pivot.

**Date:** 2026-05-10
**Reviewers:** PM (voltagent-biz:product-manager), BA (voltagent-biz:business-analyst)
**Inputs:** [`docs/use-cases/new-user-cases-after-sprint-4.md`](../use-cases/new-user-cases-after-sprint-4.md) (user's draft), [`docs/architecture/sprint-3-stage-1-product-review.md`](sprint-3-stage-1-product-review.md) (template + narrative style), [`hr_server/service/hr_service.py`](../../hr_server/service/hr_service.py) (leave workflow already end-to-end), [`it_server/service/store.py`](../../it_server/service/store.py) (IT asset model), [`docs/scope-policy.md`](../scope-policy.md) (scope landscape).

**Design status entering Stage 1:** User's draft conflates two distinct products:
1. **Leave-workflow UCs (user's cases 3–5)** — already implemented end-to-end in Sprints 1–3 (`hr_service.py` has apply/approve/reject/list; UC-02/03 demo the agent engagement model; UC-07 proves write-scope + HR Admin role). These are **not new**. The user may not be aware of the existing coverage.
2. **Cubicle + reporting-table UCs (user's cases 1–2, 6)** — genuinely new domain concepts not yet in scope. Cubicle is a parallel asset concept to laptop/phone. Reporting tables are HR Admin GUI surfaces for read-only dashboards.

This review separates the two, curates a minimal-viable sprint scope, and flags over-engineering risks.

---

## §1. Sprint goal

Extend the agentic IAM demo to cover **asset assignment workflows (cubicle + IT device)** and **HR Admin reporting dashboards**, while validating that the existing **leave-approval workflow already delivers the business value the user intended** without Sprint 4 rework.

---

## §2. Demo narrative (new acts relative to Sprint 1–3)

**Wall-clock time: ~3 minutes. Two personas, three acts.**

**Setup:** Two test accounts active: `employee_user` (role = Employee) and `hr_admin_user` (role = HR Admin). Orchestrator session manager live.

**Act I — Onboarding (30 sec):** HR Admin types: *"Assign a cubicle to the new hire Jane."* Orchestrator routes to HR Agent → CIBA for `hr_assets_write_rest` (cubicle scope, new) → HR Admin approves → HR Agent calls `hr_server` MCP tool `assign_cubicle(employee_id, cubicle_id)` → 200 OK with assignment record. **What this proves:** the demo can now model domain-specific write operations (asset assignment) using the CIBA pattern that worked for leave approval. **New relative to Sprint 3:** cubicle concept + assignment scope.

**Act II — Self-service asset discovery (30 sec):** Employee logs in, asks: *"Where is my cubicle?"* and *"What laptop do I have?"* Orchestrator routes → HR Agent + IT Agent (fan-out) → both use `hr_self_rest` / `it_self_rest` scopes (employees can query their own assignments) → two returns in ~500 ms. **What this proves:** employee can discover their own assignment without HR Admin intervention. **New relative to Sprint 3:** `it_self_rest` scope for employee self-service on IT assets.

**Act III — HR Admin reporting (30 sec):** HR Admin navigates to a new "Reports" page → clicks "Cubicle Assignments" → table renders (user name, cubicle, floor, occupied flag) → clicks another tab "Laptop Assignments" → second table (user name, serial number, model). **What this proves:** HR Admin has a read-only dashboard for assignment auditing. **New relative to Sprint 3:** REST GET endpoints returning curated datasets; no CIBA (no write, no user delegation needed — HR Admin's service-token suffices for reading all records).

**Industry wedge (verbal):** this extends the identity-first governance narrative from Sprint 3. Instead of just revoking access, the demo now shows *building* cross-domain workflows (HR + IT) where each domain owns its assets but consults shared role/scope rules at every step. Acts I–II show the pattern; Act III shows the audit surface.

---

## §3. Scope assessment of the user's draft UCs

### User Case 1 — Newly joined employee, cubicle assignment

**Claim:** HR Admin allocates a cubicle for a new joiner.

**Assessment:**
- **Is this new?** Yes, cubicle is a new asset class not in `it_server/store.py` today.
- **Maps to:** UC-11 (proposed below).
- **Minimal viable build:**
  - Add cubicle data model to `hr_server` store: `100 cubicles` × `{id, floor, occupied, assigned_to_sub, assigned_to_name}`.
  - New MCP tool `assign_cubicle(cubicle_id: str, employee_id: str, employee_name: str)` in hr_server guarded by `hr_assets_write_rest` scope (new scope).
  - HR Agent initiates CIBA with `hr_assets_write_rest` when tool is invoked.
  - HR Server allocates and returns `{success, cubicle_id, floor, assigned_to}`.
- **Over-engineering risk to watch:**
  - **❌ Multi-building / floor-plan UI.** Demo has one imaginary building. Do not add floor-plan visualization, seating charts, or multi-facility features. Stay in-memory; 100 cubicles is the seed data.
  - **❌ Cubicle availability discovery.** Do not add a separate "list_available_cubicles" tool — HR Admin just knows they have 100 and picks one. Constraint: tool validation rejects already-occupied cubicle.
  - **❌ Persistent allocation history / audit trail separate from logs.** Use the orchestrator's existing `tools/grep-trace.sh` audit model. Do not add a dedicated history table.

### User Case 2 — New Employee self-service: cubicle lookup + laptop lookup

**Claim:** Employee can ask "where is my cubicle?" and "what laptop do I have?" and get back details.

**Assessment:**
- **Is this new?** Partially. Laptop lookup is possible today via `it_assets_read_rest` (IT Agent can call `get_my_assets`). Cubicle lookup is new.
- **Maps to:** UC-12 (employee self-service on assignments).
- **Minimal viable build:**
  - HR Agent: new tool `get_my_cubicle()` guarded by `hr_self_rest` scope. Returns `{cubicle_id, floor, occupied_by_me: true}` or `{assigned: false}`.
  - IT Agent: add `get_my_assets` tool to the existing `it_assets_read_rest` scope (mirrors `get_my_leave_requests` shape in HR). Returns `{assets: [{asset_id, type, model, serial}], total}`.
  - Both tools use OBO tokens (CIBA initiated by agent, approved by user in consent widget).
  - Employee approval grants same scope they already need for HR self-service.
- **Over-engineering risk to watch:**
  - **❌ Equipment return workflow.** "My laptop is ready to go back" is a separate UC (return/reclaim logic). Do not add it; out of scope.
  - **❌ Asset condition/maintenance tracking.** Keep assets dumb: id, type, model, status (outstanding/returned). Do not add condition flags, warranty dates, etc.
  - **❌ Multi-step reservation.** Employee cannot "request" a cubicle; HR Admin assigns it. One-way allocation only.

### User Case 3 — Employee applies for leave

**Claim:** Employee asks for leave; lands on HR Admin's approval queue.

**Assessment:**
- **Is this new?** **No. This is already deployed end-to-end.**
  - Existing: `hr_service.apply_leave()` in `hr_server/service/hr_service.py` (lines 72–143).
  - Existing: HR Agent `hr.apply_leave` tool, scoped to `hr_self_rest`, wired in Sprint 1.
  - Existing: UC-02 (single specialist) and UC-03 (two specialist) both exercise this in the demo.
  - Existing: Sprint 1 & 2 live-walks confirmed the flow works end-to-end.
- **Maps to:** UC-02 (reuse, no new UC needed).
- **What user may not realize:** The agent engagement model (CIBA initiation, approval, token-C to hr_server) is already in place. User can ask the orchestrator to demo this immediately — no new build.
- **Minimal viable for demo script:** Ensure `demo-runbook.md` has a concrete "Employee applies for leave" script that walks through the approval flow and shows the HR Admin approve step. (Likely already in runbook; confirm.)

### User Case 4 — Employee checks leave status

**Claim:** Employee can see the status of their applied leave requests.

**Assessment:**
- **Is this new?** **No. This is already deployed end-to-end.**
  - Existing: `hr_service.get_my_leave_requests()` in hr_server/service/hr_service.py (lines 54–69).
  - Existing: HR Agent `hr.get_my_leave_requests` tool, scoped to `hr_self_rest`, wired in Sprint 1.
  - Existing: UC-02 exercises this in the demo as part of the multi-step query flow.
- **Maps to:** UC-02 (reuse).
- **Minimal viable for demo script:** Same as UC-3: runbook should show the employee querying leave status and seeing approved/pending/rejected requests with dates.

### User Case 5 — HR Admin queries all leave requests in approval stage (reporting table)

**Claim:** HR Admin can see a filtered table of all pending leave requests (user name, type, duration, start date).

**Assessment:**
- **Is this new?** Partially. The *data* is already available via `hr_service.get_all_leave_requests()` (hr_service.py, lines 148–168). The *GUI surface* (reporting page / dashboard table) is new.
- **Maps to:** UC-13 (HR Admin reporting dashboard — phase 1).
- **Minimal viable build:**
  - HR Admin does NOT need CIBA for this — they're reading data, not invoking a tool on behalf of the user.
  - New REST endpoint: `GET /api/reports/leave-requests?status=pending` (in hr_server REST API, `/rest_api/server.py`). Guarded by `hr_read_rest` scope (scope already exists).
  - Orchestrator (or SPA directly) calls hr_server to fetch the dataset.
  - SPA renders a table: columns = employee_name, leave_type, days_requested, start_date, action=approve/reject buttons.
  - Approve button sends `POST /api/leave/{request_id}/approve` (also new REST endpoint guarded by `hr_approve_rest`).
- **Over-engineering risk to watch:**
  - **❌ Calendar visualization.** Do not render a Gantt chart or calendar heatmap. Simple sortable table is sufficient.
  - **❌ Email notification integration.** Do not send emails when leaves are approved/rejected. Logs are audit trail enough.
  - **❌ Bulk operations.** Do not add bulk-approve or CSV export. Single-row actions only.
  - **❌ Approval workflows with multiple tiers.** "Department head + HR manager signature" is out of scope. Single HR Admin approver only.

### User Case 6 — HR Admin queries devices by employee (reporting table)

**Claim:** HR Admin can generate a table of cubicle assignments and laptop assignments.

**Assessment:**
- **Is this new?** Partially. The *data queries* are new (cubicles are new domain; laptops are already in store but not yet surfaced to HR Admin). The *GUI surfaces* are new.
- **Maps to:** UC-13 (HR Admin reporting — phase 2, cubicle + IT asset tables).
- **Minimal viable build:**
  - Two new REST endpoints in hr_server and it_server respectively:
    - `GET /api/reports/cubicle-assignments?employee_name=<filter>` (in hr_server).
    - `GET /api/reports/device-assignments?employee_name=<filter>` (in it_server or called by orchestrator on behalf of HR Admin).
  - Both guarded by read-scopes (`hr_read_rest` / `it_assets_read_rest`).
  - Orchestrator or SPA calls these endpoints using the logged-in HR Admin's service token (no CIBA, no user delegation — this is system-level data).
  - SPA renders two tables: one for cubicles (user_name, cubicle_id, floor), one for IT assets (user_name, asset_id, type, model).
- **Over-engineering risk to watch:**
  - **❌ Device lifecycle tracking / condition scoring.** Do not add asset health, age, or depreciation schedules. Just name, model, and assignment.
  - **❌ Cross-tenant asset pooling.** Do not add multi-org tenancy. Single demo tenant only.
  - **❌ Real-time availability / allocation conflicts.** Do not add concurrency control or reservation locks. In-memory assignment is fire-and-forget.

---

## §4. Use cases (PM-curated set)

**Philosophy:** Inherit UC-01..UC-10 as-is from Sprint 3. Add UC-11..UC-13 to cover new business scope (cubicles + dashboards). Do not re-number existing UCs.

| ID | Name | Priority | Sprint | Status | Notes |
|---|---|---|---|---|---|
| UC-01..10 | (Inherited from Sprint 3) | — | 1–3 | ✓ Signed off | Carry forward. |
| **UC-11** | **Assign cubicle to new employee** | High | 4 | New | HR Admin uses HR Agent to allocate cubicle via CIBA `hr_assets_write_rest`. Act I of the demo. |
| **UC-12** | **Employee self-service on asset assignments** | Medium | 4 | New | Employee queries own cubicle and laptop via HR/IT agents using `hr_self_rest` / `it_self_rest`. Act II of the demo. |
| **UC-13** | **HR Admin reporting dashboards** | Medium | 4 | New | Two-part: (a) leave-request pending queue as a filterable table, (b) cubicle + device assignment tables. No CIBA. Act III of the demo. |

---

## §5. Personas + role mapping

**Confirmed:** Two personas as stated by user.

| Persona | Role in IS | Scopes granted | Can do |
|---|---|---|---|
| **Employee** | `Employee` | `hr_basic_rest`, `hr_self_rest`, `it_assets_read_rest` | View own leave balance, apply for leave, view own leave requests, view own assigned cubicle + laptop. |
| **HR Admin** | `HR Admin` | All of Employee's scopes PLUS `hr_read_rest`, `hr_approve_rest`, `hr_assets_write_rest`, `it_assets_write_rest`, `it_assets_read_rest` | All Employee actions PLUS: view all leave requests, approve/reject leave, assign cubicles, issue IT assets, view all assignment tables. |

**Role inheritance pattern:** NOT role-hierarchy inheritance in the code. Rather, **IS scope assignment rules** (configured in IS Console → Roles → Permissions) ensure that `HR Admin` role simply grants a superset of scopes. Simplest demo approach:
- Do not add a role-inheritance layer in the orchestrator or agents.
- Rely on IS's role-to-scope binding to prevent Employees from ever receiving `hr_approve_rest` or write scopes.
- If an Employee somehow gets assigned `hr_approve_rest` at IS, the MCP server's scope validator rejects the call with ERR-MCP-003 anyway (fail-safe, per scope-policy.md §3 rule 3).

**Demo enforcement:** Spot-check the role assignments in IS Console → Users → `employee_user` and `hr_admin_user` before Stage 2 QA. Confirm role members are correct and scopes are as above.

---

## §6. Scope changes — minimal set

**User's question:** "Should we add `it_self_rest` for employee self-service on IT assets?"

**PM opine:** **Yes, add it. It is not overkill.** Rationale:

| Scope | Existing? | Purpose | Minimal? |
|---|---|---|---|
| `hr_basic_rest` | Yes | Employee + HR Admin read holidays, leave policy | Yes; already deployed. |
| `hr_self_rest` | Yes | Employee read own leave data, apply for leave | Yes; already deployed. |
| `hr_read_rest` | Yes | HR Admin read all employees' leave requests | Yes; already deployed. |
| `hr_approve_rest` | Yes | HR Admin approve/reject leave | Yes; already deployed. |
| `hr_assets_write_rest` | **New** | HR Admin assign cubicles to employees | **Yes, minimal. Balances hr_self_rest.** |
| `it_assets_read_rest` | Yes | Employee + HR Admin list available IT assets | Yes; already deployed. |
| **`it_self_rest`** | **New** | **Employee read own IT assets (laptop, phone, etc.)** | **Yes, minimal. Mirrors hr_self_rest pattern.** |
| `it_assets_write_rest` | Yes | HR Admin issue/allocate IT assets | Yes; already deployed. |

**Why `it_self_rest` is justified:**
1. **Scope hygiene:** `it_assets_read_rest` is a coarse-grained "list everything" scope. `it_self_rest` (like `hr_self_rest`) restricts to "my own record," which is the principle of least privilege for employee self-service.
2. **Pattern consistency:** HR domain has `hr_self_rest` for employee's own data and `hr_read_rest` for all data. IT domain should mirror: `it_self_rest` for employee's own data, `it_assets_read_rest` for all data. (Today `it_assets_read_rest` is granted to both; tomorrow it should be `hr_admin` only, and employee should use `it_self_rest`.)
3. **Business logic:** Employee asking "what's my laptop" is fundamentally different authorization than HR Admin asking "list all laptops" — they should map to different scopes.
4. **Demo credibility:** Having a matching pair (hr_self_rest / it_self_rest) for employee self-service strengthens the identity-first governance narrative.

**Proposed scope table for Sprint 4:**

| Scope | Granted to | New? | Tool scope | Notes |
|---|---|---|---|---|
| `hr_basic_rest` | Employee, HR Admin | No | `get_holidays`, `get_leave_policy` | General HR info. |
| `hr_self_rest` | Employee, HR Admin | No | `get_my_leave_balance`, `get_my_leave_requests`, `apply_leave`, `get_my_cubicle` | Own-data self-service. |
| `hr_read_rest` | HR Admin | No | `get_all_leave_requests`, `get_leave_request_details` | Admin read-all. |
| `hr_approve_rest` | HR Admin | No | `approve_leave_request`, `reject_leave_request` | Admin write. |
| `hr_assets_write_rest` | HR Admin | **Yes** | `assign_cubicle` | Cubicle allocation. |
| `it_assets_read_rest` | HR Admin | No | `list_available_assets`, `get_asset_by_id` (admin read-all). | Kept for HR Admin; change scope binding in IS. |
| `it_self_rest` | Employee, HR Admin | **Yes** | `get_my_assets` | Employee's own IT assets. |
| `it_assets_write_rest` | HR Admin | No | `issue_asset` | Admin issue/allocate. |

**Role-to-scope binding at IS (post-stage-1):**
- `Employee` role gets: `hr_basic_rest`, `hr_self_rest`, `it_self_rest`.
- `HR Admin` role gets: all of Employee's scopes PLUS `hr_read_rest`, `hr_approve_rest`, `hr_assets_write_rest`, `it_assets_read_rest`, `it_assets_write_rest`.

---

## §7. Cubicle data model — minimum

**In `hr_server/service/store.py`:**

```
cubicles = [
  {
    "cubicle_id": "C-001",
    "floor": 1,
    "occupied": False,
    "assigned_to_sub": None,        # user's sub UUID, or None if unassigned
    "assigned_to_name": None,       # cached full name
    "assigned_at": None,            # ISO 8601 timestamp, or None
  },
  ...
  {
    "cubicle_id": "C-100",
    "floor": 3,
    ...
  },
]
```

**Seed data:** 100 cubicles across 3 floors (C-001..C-033 on floor 1, C-034..C-066 on floor 2, C-067..C-100 on floor 3). All start `occupied=False`.

**Operations:**
- `assign_cubicle(cubicle_id: str, employee_sub: str, employee_name: str) → Dict` — returns `{success: bool, cubicle: {...}}` or error.
- `get_my_cubicle(user_sub: str) → Dict` — returns user's assigned cubicle or `{assigned: False}`.
- `get_all_cubicle_assignments() → List[Dict]` — returns all (for HR Admin reporting).

**Out of scope:**
- Reclaim / unassign (not in this sprint).
- Conflict detection (if two requests arrive concurrently, last-write-wins; logs capture it).
- Floor/building hierarchy beyond flat numbering.
- Availability scoring or allocation heuristics.

---

## §8. HR Admin reporting tables — UI placement

**Question for UX (Stage 3):** Where do the reporting tables live in the SPA?

**PM recommendation:** Defer the exact placement to Stage 3 UX review, but flag two viable options now:

### Option A: Embedded in Chat UX (inline results)
- Employee / HR Admin can ask "show me all pending leaves" in the chat box.
- Orchestrator routes to HR Agent → HR Agent calls `hr_server` REST endpoint (no CIBA, no user delegation).
- Result renders in the chat pane as a HTML table below the message.
- **Pros:** Natural chat extension; user doesn't navigate away. **Cons:** Table scrolling in chat pane is cramped.

### Option B: Separate Reporting / Dashboard Page
- Top-level SPA navigation (menu): Chat | **Reports** | Settings.
- Reports page has tabs: "Pending Leaves" | "Cubicles" | "Devices".
- Each tab is a filterable table with buttons (Approve, Reject, etc.).
- **Pros:** Dedicated real estate; better UX for tabular data. **Cons:** Two UX paradigms (chat + traditional UI).

**PM view:** Option B is more professional for a demo to executives. Option A feels like a quick add-on. Recommend Option B unless UX has strong reasons otherwise. But defer the final choice to Stage 3 design review.

**Tech note:** Either way, the orchestrator needs new REST routes:
- `POST /api/reports/leave-requests` → calls `hr_server GET /api/reports/leave-requests?status=pending` + returns JSON list.
- `POST /api/reports/cubicle-assignments` → calls `hr_server GET /api/reports/cubicle-assignments` + returns JSON list.
- `POST /api/reports/device-assignments` → calls `it_server GET /api/reports/device-assignments` + returns JSON list.
- (Or SPA calls those endpoints directly if CORS allows; scope-check at server side using the logged-in user's service token.)

---

## §9. Out of scope (explicit)

The following are tempting additions but **explicitly out of Sprint 4**. Cut ruthlessly to avoid the over-engineering pattern that nearly derailed Sprint 2:

1. **Persistent storage / database.** All assignment data is in-memory. If the hr_server pod restarts, cubicle assignments are lost. This is fine for a demo. (Production roadmap: wire a persistent HR datastore post-GA.)

2. **Asset return workflow.** "I want to return my laptop" is a separate business process (two-step: employee initiates return, HR Admin accepts/rejects). Out of scope. UC-12 is query-only.

3. **Equipment lifecycle tracking.** Asset purchase date, warranty expiry, depreciation, condition scoring, maintenance logs — none of this. Assets are dumb: `{asset_id, type, model, status: outstanding|returned}`.

4. **Multi-tenant asset pooling.** Do not model shared asset pools across departments or locations. All assets belong to the single demo tenant.

5. **Concurrent allocation / reservation system.** If two HR Admins assign the same cubicle at the same time, last-write-wins. Do not add distributed locks, optimistic concurrency, or allocation queues.

6. **Cubicle reclaim / reassignment workflow.** Once a cubicle is assigned to an employee, you cannot reassign it to someone else in Sprint 4. Unassign is the next sprint's problem.

7. **Leave calendar visualization / Gantt chart.** Pending-leaves table is row-by-row. Do not add calendar heatmaps, conflict detection with overlapping leaves, or visual timeline renderings.

8. **Email notifications.** HR Admin approves leave → no email sent to employee. Logs capture the action; that's the audit trail for the demo.

9. **Audit UI for compliance / historical compliance reports.** "Show me all leaves approved by HR Admin X in Q1 2026" is a compliance feature. Out of scope. Use `tools/grep-trace.sh` for ad-hoc audit.

10. **Role-based view filtering in tables.** HR Admin sees all columns; Employee sees none (because they don't have table access). Do not add column-level masking or role-based table redaction.

---

## §10. Exit criteria (draft)

All must be falsifiable from a live demo run or an automated test. Mirror the rigor of `sprint-3-signoff.md` acceptance criteria.

1. **D4.1 — Cubicle assignment happy path end-to-end.** HR Admin types "assign cubicle C-045 to Jane"; orchestrator routes to HR Agent; CIBA for `hr_assets_write_rest` succeeds; HR Admin approves in widget; hr_server returns assignment record; orchestrator confirms "Cubicle C-045 on floor 2 assigned to Jane Doe." Latency ≤2 seconds. (R4-1 test.)

2. **D4.1 — Employee cannot assign cubicles.** Employee types "assign a cubicle"; CIBA for `hr_assets_write_rest` fails at IS with `invalid_scope` or `access_denied`. Orchestrator surfaces "You don't have permission to assign cubicles." (R4-2 test — role denial.)

3. **D4.2 — Employee self-service asset query end-to-end.** Employee logs in, asks "where is my cubicle"; HR Agent CIBA for `hr_self_rest` + `get_my_cubicle` tool returns assignment. Employee asks "what laptop do I have"; IT Agent CIBA for `it_self_rest` + `get_my_assets` tool returns device. Both within 1 second per agent. (R4-3 test.)

4. **D4.3 — HR Admin leave-requests reporting table.** HR Admin navigates to Reports → Pending Leaves tab → table renders with ≥3 rows (pre-seeded pending requests); columns include employee_name, leave_type, days_requested, start_date. Table is sortable by start_date. (R4-4 test — GET /api/reports/leave-requests.)

5. **D4.3 — HR Admin cubicle-assignments reporting table.** HR Admin navigates to Reports → Cubicles tab → table renders with ≥10 assigned cubicles (from seed assignments); columns are employee_name, cubicle_id, floor, assigned_at. (R4-5 test.)

6. **D4.3 — HR Admin device-assignments reporting table.** HR Admin navigates to Reports → Devices tab → table renders with IT assets from seed data (laptops, phones, headsets); columns are employee_name, asset_id, type, model. (R4-6 test.)

7. **D4.1 — Captured assignment token (-C from CIBA) cannot be replayed.** HR Admin approves cubicle assignment, token-C captured pre-acceptance. Token-C presented post-transaction to a different scope validator (e.g., hr_server's `get_all_leave_requests`) → 401 ERR-MCP-001 (aud mismatch) or 401 ERR-MCP-003 (scope mismatch, since assignment token is scoped to `hr_assets_write_rest`, not `hr_read_rest`). (R4-7 test — cross-scope replay block from Sprint 3 infrastructure.)

8. **D4.1–4.3 — No test failures.** `tools/run-tests.sh` green on the strict-mode runner. Minimum 900/50 tests passing (estimate: +4 R-tests per UC × 3 UCs + integration tests). (R4-8 — regression gate.)

---

## §11. Risks

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| R-1 | **User conflates "new UCs" with "new code."** User believes cases 3–5 (leave workflow) are new, but apply/approve/list are already shipped in Sprint 1–2. Risk: Sprint 4 accidentally re-implements the leave workflow, duplicating code and confusing the narrative. | High | **Immediate action (Stage 1 close):** PM walks through the leave flow in hr_service.py with the user to confirm UC-02/03 already exercise the entire workflow. Update the demo runbook to show the leave flow as Act I (before cubicle/reporting). This re-frames the narrative: "leave was the first workflow; cubicles are the second." |
| R-2 | **Scope explosion during implementation.** PM has proposed `hr_assets_write_rest` + `it_self_rest`. During Stage 4 (architecture) or 6 (build), a dev suggests: "we also need `it_assets_read_rest` split into `it_self_read` and `it_admin_read`" or "let's add `hr_cubicle_report_rest`." Each new scope requires IS config + tool updates. | Medium | **Scope lock: freeze the proposed 8-scope table at the end of Stage 1.** Any new scope requires PM exception approval (same process as adding a new UC). Document the lock in the stage-1 decision summary. |
| R-3 | **Cubicle assignment without conflict detection ships as single-point-of-failure.** If two concurrent HR Admin requests assign the same cubicle, one silently overwrites the other. Logs capture it, but the demo looks broken on stage. | Low | **Mitigation:** In-memory assignment is fire-and-forget by design. Pre-seed the 100 cubicles; during the demo, HR Admin only assigns from the unassigned pool (which is >95 cubicles). Likelihood of two concurrent assigns to the same one is low. If risk materializes during Stage 6 integration, add a server-side uniqueness check: reject assignment if `occupied=true` already. (One-line if statement; no concurrency needed in demo.) |
| R-4 | **Reporting tables drown in feature creep (calendar, filtering, export, etc.).** UX team sees a table and suggests "add a date picker to filter by date range" or "allow bulk-approve." Risk: Stage 4 arch explodes; Stage 6 build slips. | Medium | **Hard constraint from Stage 1:** Tables are static renders of full dataset. Sorting only (client-side). No filtering widgets. No bulk operations. No export buttons. PM enforces this in Stage 2 BA expansion (see §12 [BA] items). |
| R-5 | **`it_self_rest` scope not correctly configured in IS before Stage 6 demo.** BA forgets to add `it_self_rest` to `IT Admin` role in IS Console. During demo, Employee approves IT Agent consent widget and receives IS error: "scope not recognized." | Low | **Mitigation:** Add an explicit pre-flight checklist for Stage 1 close (mirror sprint-3-stage-1 §5). Check: (a) all 8 scopes exist as API resources in IS, (b) both roles' "Permissions" tab grants the right scopes, (c) both agent OAuth Apps are subscribed to their scopes. Runbook: `scripts/check-is-config.sh` (new) audits this on Day 1 of Stage 6. |
| R-6 | **REST reporting endpoints confuse the "agent-routed CIBA" narrative.** The demo's main story is agent-initiated CIBA → user approval → OBO token. But reporting tables are "HR Admin's service token → system read." Some stakeholders may ask "why isn't this an MCP tool + agent?" Risk: narrative confusion in Act III. | Low | **Mitigation:** Stage 2 narrative framing. Emphasize in copy-deck: "MCP tools are for actions *on behalf of users* (applying leave, assigning assets). Reporting tables are *system reads* that don't need user delegation — HR Admin's service token is sufficient. This is normal API tiering." One or two sentences in the runbook clarify the pattern. |

---

## §12. Open questions for downstream stages

### [BA] — Requirement expansion (Stage 2)

1. **[BA] Cubicle floor mapping.** User draft says "3rd floor, cubicle #23." Does the demo use numeric floor IDs (1, 2, 3) or labels ("Floor 3", "Level B")? Seed data only — no multi-building support. **Decision defer to Stage 2 BA expansion** — propose floor = numeric int for simplicity.

2. **[BA] Employee lookup in reporting tables.** When HR Admin views "Cubicle Assignments" table, can they filter by employee name? The table renderer could do client-side filter (JavaScript `<input type="text">` box). Or no filtering at all — render all 100 rows and let the admin scroll. **Decision defer to Stage 2 BA expansion** — recommend "no filters, static table" to minimize scope creep.

3. **[BA] Cascading asset deallocation on role change.** If an Employee is downgraded to Contractor (loses HR Admin role), should their `hr_assets_write_rest` scope be revoked? Does that revoke in-flight CIBA calls? Out of scope for Sprint 4, but defer to product backlog. **Note for planning:** This is a role-revocation pattern, not asset reclaim.

### [UX] — Interface design (Stage 3)

4. **[UX] Reporting table placement decision.** Option A (embedded in chat) vs Option B (separate Reports page). **Decision defer to Stage 3 UX review.** Recommend Option B for professional appearance.

5. **[UX] Approve/Reject buttons in leave-requests table.** Do clicks open a modal dialog? Inline form? Or navigate to a detail page? **Decision defer to Stage 3 wireframe.** Recommend simple inline form to keep the demo snappy.

6. **[UX] Consent widget binding message for `hr_assets_write_rest`.** Should it say "HR Agent wants to assign you a cubicle" or "HR Agent wants to assign a cubicle to a new employee"? The binding message needs to distinguish user-initiated (employee applies for leave) from admin-initiated (HR Admin assigns cubicle on behalf of employee). **Decision defer to Stage 3 UX + binding_message.py branch logic.** Recommend: if `scope=hr_assets_write_rest` and `tool=assign_cubicle`, override binding_message to say "Assign cubicle to [employee_name]" (admin action verb, not user delegating).

### [API] — REST endpoint design (Stage 4)

7. **[API] Reporting endpoint response envelope.** Should `GET /api/reports/leave-requests?status=pending` return `{data: [{…}], total: N}` or `[{…}]` or `{items: [{…}], count: N}`? **Decision defer to Stage 4 API design.** Recommend: `{data: [{…}], count: N, filters: {status: "pending"}}` to mirror the MCP pagination envelope style.

8. **[API] Cubicle assignment idempotency.** If HR Admin assigns cubicle C-045 to Jane twice (retry after timeout), should the second attempt succeed (idempotent) or fail with "already assigned"? **Decision defer to Stage 4 API design.** Recommend: succeed silently if the same user is already assigned; fail if a *different* user is assigned. (One-line check in assign_cubicle.)

### [ARCH] — Technical decisions (Stage 4)

9. **[ARCH] Reporting endpoints — orchestrator routing vs direct SPA-to-server.** Should `GET /api/reports/leave-requests` be called via the orchestrator (one additional hop) or directly from SPA to hr_server? **Decision defer to Stage 4 architecture.** Recommend: direct SPA-to-hr_server, guarded by `hr_read_rest` scope validation at hr_server. Orchestrator is only needed for CIBA workflows; reporting is system-read.

10. **[ARCH] Cubicles in hr_server vs new domain service.** Should cubicles live in `hr_server` alongside leave data, or in a new `facilities_server` MCP service? **Decision defer to Stage 4 architecture.** Recommend: keep in `hr_server` for Sprint 4 (single service per domain is enough). Facilities as a separate service is a post-GA refactor.

---

## §13. Stage gate

**Recommend proceeding to Stage 2 (BA expansion).**

The user's draft has been curated to separate "already-done" (leave workflow UCs 3–5) from "genuinely new" (cubicles + reporting). The scope is minimal: three new UCs, two new scopes, one new asset domain (cubicle), three REST reporting endpoints, and re-use of the existing CIBA + agent engagement pattern proven in Sprints 1–3.

**BA expansion priorities for Stage 2:**
1. **Confirm leave workflow coverage.** Walk the user through hr_service.py to validate that UC-02/03 already cover their business intent. Update the demo runbook to feature the leave flow prominently (it's the first workflow, the foundation).
2. **Cubicle model finalization.** Lock the cubicle data shape (100 cubicles, 3 floors, numeric floor IDs). Confirm assignment is one-way (no reassign in Sprint 4).
3. **Reporting table datasets.** For each of the three tables (pending leaves, cubicle assignments, device assignments), write the exact column list and sort order. Mock-render in a text table to validate UX readability.
4. **Role-scope matrix.** Verify the proposed 8-scope table against IS Console configuration. Confirm both roles' Permissions tabs are correctly set.
5. **Narrative flow.** Draft the 3-minute demo runbook: Act I (cubicle assignment), Act II (employee self-service), Act III (reporting tables). Thread the leave workflow as context-setting (already shipped, no new code).

**Handoff to Stage 3 (UX):** Wireframes for reporting-table placement (Option A vs B decision) and cubicle-assignment consent widget binding message.

**Handoff to Stage 4 (Architecture):** API endpoint design for reporting, cubicle assignment idempotency, reporting-endpoint routing architecture.

---

## Addendum — Scope policy alignment check

All proposed scopes follow `docs/scope-policy.md` naming convention (`<resource>_<action>_rest`). No deviations. All scopes are one-tier (no `_a2a` / `_mcp` split). All scopes are requested at CIBA, embedded in OBO tokens, and validated by servers. Audience + actor chain handle cross-tier trust. ✓
