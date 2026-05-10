# Sprint 4 — Sprint Plan (objectives + exit criteria)

**Stage:** 3 (sprint objectives / asks / exit criteria — locked after user back-track on 2026-05-10).
**Date:** 2026-05-10.
**Branch (entry):** `sprint-3-build` @ `b497616` (Sprint 3 close + hardening pass).
**Branch (work):** `sprint-4-build` (cut at Stage 9 kickoff).
**Read order:** [`sprint-4.md`](sprint-4.md) (this — binding) → [`sprint-4-stage-1-product-review.md`](sprint-4-stage-1-product-review.md) (PM brief, *historical* for narrative + initial scope; this doc supersedes scope decisions where they conflict) → BA-expanded UCs in [`docs/use-cases/UC-11..UC-16`](../use-cases/) → downstream stage docs once written.

> **User back-track (2026-05-10):** Five course-corrections at Stage 3 manual review. Recorded inline; this doc supersedes the PM brief on:
> 1. **No `employee_id` concept.** Use `username` (preferred) and `email` (secondary) as the identifying claims. Both come from the IS-issued ID/access token. Drops one Stage 6 architecture blocker entirely.
> 2. **Reporting tables data-flow + scope walk-through must be documented.** See §8.
> 3. **Cubicle assignment is multi-turn:** vacant summary → admin selects floor → vacant numbers on that floor → admin picks → assign. **4 floors**, not 3.
> 4. **`it_assets_self_rest`** is the new scope (preserves the locked `_rest` suffix from `docs/scope-policy.md`; semantic intent is parallelism with `it_assets_read_rest` / `it_assets_write_rest`).
> 5. **UC-13 / UC-14 are NOT verify-existing-only.** Backend exists, but the SPA has no surface for "My Leaves." Sprint 4 adds a SPA panel (build new) + the existing chat path keeps working.

---

## 1. Sprint goal (one sentence)

Pivot the demo from infrastructure/IAM hardening (Sprints 1–3) to **business-domain richness**: add a multi-turn cubicle-assignment workflow, employee self-service for IT assets, HR-admin reporting tables, **and a "My Leaves" SPA panel that surfaces the existing leave backend**.

---

## 2. Objectives (build-new vs verify-existing)

### Build-new — backend (services)
1. **Cubicle domain model** in `hr_server/service/store.py` — 100 cubicles seeded across **4 floors** (e.g. C-001..C-025 on floor 1, C-026..C-050 on floor 2, etc.), in-memory, last-write-wins on conflict.
2. **MCP tools (HR Server)** for the multi-turn cubicle flow:
   - `get_cubicle_summary()` → `{floor_1: {total, vacant}, floor_2: {...}, floor_3: {...}, floor_4: {...}}` — read-side, scope `hr_read_rest` (existing).
   - `get_vacant_cubicles_on_floor(floor)` → list of vacant cubicle IDs on that floor — scope `hr_read_rest` (existing).
   - `assign_cubicle(cubicle_id, employee_username, employee_email)` → assignment record — scope `hr_assets_write_rest` (**NEW**).
   - `get_my_cubicle()` → caller's assigned cubicle (or `{assigned: false}`) — scope `hr_self_rest` (existing).
3. **MCP tool `get_my_assets`** in `it_server` (returns caller's IT assets, e.g. laptop) — scope `it_assets_self_rest` (**NEW**).
4. **REST reporting endpoints (orchestrator-proxied per §8)**:
   - `GET /api/reports/leave-requests?status=pending` — backend `hr_server.get_all_leave_requests` already exists; add thin REST adapter. Scope `hr_read_rest` (existing).
   - `GET /api/reports/cubicle-assignments` — new on `hr_server`. Scope `hr_read_rest`.
   - `GET /api/reports/device-assignments` — new on `it_server`. Scope `it_assets_read_rest` (existing).
   - `GET /api/me/leaves` — new on `hr_server`, returns caller's leave list for the My Leaves panel. Scope `hr_self_rest` (existing).

### Build-new — frontend (SPA)
5. **Reports page** — top-level navigation entry, three tabs (Pending Leaves | Cubicles | Devices), static tables (sortable client-side, no filters / bulk / export).
6. **My Leaves panel** on the SPA home page (visible immediately on login for any logged-in user) — pulls `GET /api/me/leaves` and renders a sortable table of the user's own leave requests (id, type, start_date, end_date, status). Sits beside / below the chat surface; not modal.
7. **Multi-turn cubicle UX** in the chat surface — see UC-11 for the four-message pattern. No separate UI; the chat agent drives the flow.
8. **Consent-widget binding-message variants** for HR-admin write actions (admin-verb form distinguishing self-service from admin-on-behalf-of).

### Build-new — IS configuration
9. New scopes `hr_assets_write_rest` and `it_assets_self_rest` registered as API resources; attached per role-scope matrix in §6.

### Verify-existing — apply leave + check leave (UC-13 / UC-14)
10. **UC-13 (apply leave) — backend already shipped** (`hr_service.apply_leave`); chat path verified manually under Sprint 4. **UI gap closed by item 6 (My Leaves panel)** — applied leaves now appear in the panel after creation.
11. **UC-14 (check leave status) — backend already shipped** (`hr_service.get_my_leave_requests`); chat path verified manually. **My Leaves panel is the GUI surface.**

---

## 3. Asks (external prerequisites)

| # | Ask | Owner | Resolution before |
|---|---|---|---|
| A1 | New IS scopes `hr_assets_write_rest`, `it_assets_self_rest` registered + attached per §6 role-scope matrix. | Operator | Stage 11 (manual test). Pre-flight script in Stage 9. |
| A2 | `employee_user` and `hr_admin_user` accounts confirmed alive in IS with role memberships per §6, and IS profile fields `username` + `email` populated for every test account. | Operator | Stage 9 kickoff. |
| A3 | `username` claim (preferred) and `email` claim are present in IS-issued tokens (ID token and access/OBO tokens). Verify before Stage 9 — IS may need OIDC scope `profile email` mapped into the access token claim set. **No `groups` claim required** — role-gating in the SPA is derived from the `scope` claim (canonical probe: `scopes.includes("hr_approve_rest")` ⇒ HR Admin, since `hr_approve_rest` is HR-Admin-exclusive per `docs/scope-policy.md` §2). The role-to-scope binding at IS is already the authoritative gate. | Operator + Tech-arch verifies | Stage 6 close. |
| A4 | Both `hr_server` and `it_server` re-seed cubicle and asset stores (now keyed by `username`) on container start. | Build | Stage 9. |
| A5 | WSO2 IS expert chat outcomes — **NOT required for Sprint 4.** Sprint 4 is independently schedulable. | User | None. |

---

## 4. Exit criteria (testable / falsifiable)

| # | Criterion | Verification |
|---|---|---|
| **EC4-1** | Multi-turn cubicle assignment end-to-end. HR Admin asks: "show me vacant cubicles" → HR Agent returns counts per floor. Admin: "show me floor 2" → HR Agent returns vacant numbers. Admin: "assign C-027 to jane.doe" → CIBA on `hr_assets_write_rest` → admin approves → assignment confirmed. p95 latency of the final assign step ≤ 2 s. | Manual demo (Stage 11) + R-CUBICLE-1, R-CUBICLE-2 automated tests (Stage 10 — one for the read-side summary, one for the write-side assign). |
| **EC4-2** | Employee attempting `assign_cubicle` is denied at IS with `invalid_scope` / `access_denied`. Orchestrator surfaces a copy-deck message ("You don't have permission to assign cubicles."). | R-CUBICLE-3 test. |
| **EC4-3** | Employee self-service via chat: "where is my cubicle and what laptop do I have" yields two consents (`hr_self_rest`, `it_assets_self_rest`), both approved, both responses returned within 1 s/agent. | R-SELF-1, R-SELF-2 tests. |
| **EC4-4** | SPA Reports page (HR Admin) renders three tabs. Each tab fetches its data via the orchestrator-proxied flow in §8 and renders a sortable table with at least one row of seed data. | R-REPORTS-1..3 tests. |
| **EC4-5** | Non-admin who navigates to the Reports page (URL-fuzzed if no nav element) gets 403 from each reporting endpoint. SPA renders an "access denied" panel, not a broken table. | R-REPORTS-4 test. |
| **EC4-6** | Cross-scope replay: a token-C minted for `hr_assets_write_rest` cannot be used at any read endpoint (audience and scope mismatch). 401 from validators. | R-CUBICLE-4 test (mirrors Sprint 3 cross-scope replay). |
| **EC4-7** | UC-13 chat path: Employee says "apply for 3 days annual leave starting 2026-05-20" → CIBA on `hr_apply_rest` (existing) → leave created → My Leaves panel shows the new request without a manual refresh (panel re-fetches on `chat_message` SSE settle). | Manual + R-LEAVE-1 test. |
| **EC4-8** | UC-14 chat path: Employee says "what leave have I applied for?" → HR Agent calls `get_my_leave_requests` → reply matches My Leaves panel content. | Manual + R-LEAVE-2 test. |
| **EC4-9** | My Leaves panel loads on first paint after login for both `employee_user` and `hr_admin_user`. Empty state renders cleanly when no leaves exist. | R-PANEL-1 test. |
| **EC4-10** | `tools/run-tests.sh` strict-mode green. Total tests ≥ 906 (Sprint 3 baseline) + new R-tests (~14 estimated). No `failed` / `error` / `xfailed` in any file summary. | CI run. |
| **EC4-11** | Demo runbook narrates the new acts in ≤ 4 minutes wall-clock with both test users. Includes the multi-turn cubicle flow and the "My Leaves panel + chat asks the same backend" framing. | Manual rehearsal + runbook PR. |
| **EC4-12** | `sprint-4-signoff.md` lists all 6 UCs (UC-11..UC-16) with build-status (NEW / verify-existing-with-new-surface / hybrid) and points to the verifying tests. | Stage 12. |

---

## 5. Out of scope (frozen at Stage 3)

Door-closed for Sprint 4:

1. Persistent storage / databases (in-memory survives the demo).
2. Asset return / unassign workflow.
3. Cubicle reclaim / reassign.
4. Equipment lifecycle (purchase date, warranty, depreciation, condition).
5. Multi-tenant asset pools.
6. Concurrent-allocation locks / optimistic concurrency. (Server-side `if occupied: reject` is the only defense.)
7. Calendar / Gantt visualization for leaves.
8. Email or push notifications.
9. Audit-UI dashboards (use `tools/grep-trace.sh` instead).
10. Bulk operations on reporting tables (no bulk-approve, no bulk-export).
11. Filter widgets on reporting tables (sortable only, client-side, no server-side filtering).
12. Floor plans / seating charts / multi-building hierarchy. (4 flat floors, numeric IDs.)
13. Persistent denylist (parked behind WSO2 IS expert chat per memory `project_introspection_deferred.md`).
14. **No Redis ever** (per user, pinned in memory).
15. **No `employee_id` concept anywhere new.** Existing usage in legacy IT seed data is rewritten to key by `username` in Sprint 4. (See §7.)

---

## 6. Scope lock (frozen scope set + role-scope matrix)

Source: [`docs/scope-policy.md`](../scope-policy.md). Any addition beyond this table requires a PM exception (same gate as adding a UC).

| Scope | Status | API Resource | Used by |
|---|---|---|---|
| `hr_basic_rest` | existing | hr_server | both roles — holiday/policy reads |
| `hr_self_rest` | existing | hr_server | both roles — caller's own leave/cubicle/profile (covers UC-12 cubicle-self, UC-13/14 leave-self, My Leaves panel) |
| `hr_apply_rest` | existing | hr_server | Employee, HR Admin — `apply_leave` |
| `hr_read_rest` | existing | hr_server | HR Admin only — read all leaves, cubicle summary, vacant-on-floor, cubicle-assignments report |
| `hr_approve_rest` | existing | hr_server | HR Admin only — approve / reject |
| `hr_assets_write_rest` | **NEW (Sprint 4)** | hr_server | HR Admin only — `assign_cubicle` |
| `it_assets_read_rest` | existing | it_server | both roles today (per scope-policy.md). Sprint 4 keeps both — admin uses it for the Devices report; Employee's narrower path is via the new `it_assets_self_rest`. |
| `it_assets_self_rest` | **NEW (Sprint 4)** | it_server | both roles — caller's own assets (UC-12 self-service) |
| `it_assets_write_rest` | existing | it_server | HR Admin only — `issue_asset` |

**Role-scope matrix (locked):**

| Role | Scopes |
|---|---|
| Employee | `hr_basic_rest`, `hr_self_rest`, `hr_apply_rest`, `it_assets_read_rest`, `it_assets_self_rest` |
| HR Admin | _all of Employee's_ + `hr_read_rest`, `hr_approve_rest`, `hr_assets_write_rest`, `it_assets_write_rest` |

HR-Admin-can-do-everything-Employee-can is enforced by **role inheritance at IS Console role-scope assignment**, not by scope hierarchy in code.

---

## 7. Identity model — `username` + `email`, no `employee_id`

User-facing identifiers in Sprint 4 are the `username` (primary) and `email` (secondary) claims from IS. Sprint 4 does **not** introduce an `employee_id` concept and rewrites the legacy IT seed data to drop the numeric `employee_id` field.

**Where the claims come from:**
- `username` — IS user attribute, surfaced in the access token claim set (operator action A3 confirms this is mapped). Today used at `hr_server` `ensure_user(...)` as `first_name`/`last_name` plumbed from the agent's argument list.
- `email` — IS user attribute, surfaced in the OIDC `email` claim with scope `email`.
- `sub` — UUID, kept as the system-internal join key (token validation, session map, denylist). Never surfaced to chat or UI.

**Where used:**
- Chat: HR Admin says "assign C-027 to **jane.doe**" — `jane.doe` is the username. If the username is ambiguous or not a unique match, agent falls back to email (`jane.doe@example.com`).
- Reporting tables: rows show `username` + `email`, not `sub`.
- Backend stores: `cubicles.assigned_to_username`, `cubicles.assigned_to_email`, plus `cubicles.assigned_to_sub` for joining (never displayed). Mirror in `it_server.assets`.
- Tools: `assign_cubicle(cubicle_id, employee_username, employee_email)` — agent resolves the username to a sub via `hr_server.lookup_employee(username_or_email)` (new helper) before mutating; the lookup is cheap because users are pre-seeded.

**Stage 6 follow-up:** the legacy IT seed `employee_id="1042"` etc. in `it_server/service/store.py` is rewritten to `username="jane.doe"` etc. as a one-shot data migration. Old asset records with the numeric IDs are not preserved.

---

## 8. Reporting tables — data flow at each hop (security view)

Answers user question 2: *"how does the GUI access the laptop and cubicle data directly from a security and OAuth scopes point of view at each hop."*

### Trust model recap (unchanged from Sprint 1–3)

The SPA holds **only a session cookie** (`orchestrator_session=<sid>`, `Secure`, `HttpOnly`, `SameSite=Strict`). It does NOT hold token-A or any OBO token. All token-bearing calls happen orchestrator-side. This is the existing pattern from the chat path; reporting tables reuse it.

### Three options considered, one locked

| Option | Description | Verdict |
|---|---|---|
| A | SPA → backend directly with `Authorization: Bearer token-A` | ❌ exposes token-A to the browser → XSS blast radius |
| B | SPA → orchestrator (cookie) → orchestrator → backend with `Authorization: Bearer token-A` | ✅ **LOCKED** — same trust model as chat path |
| C | Orchestrator uses a separate "system" identity to read on behalf of the user's session | ❌ loses user identity in audit trail |

### Concrete hops — UC-16 "Cubicle Assignments" tab

```
[SPA] ─── GET /api/reports/cubicle-assignments ───► [Orchestrator]
                  Cookie: orchestrator_session=<sid>
                  X-Request-ID: <rid>

[Orchestrator] step 1: session lookup (sid → Session)
               step 2: assert Session.terminating == False
               step 3: read Session.token_a (HR Admin's IS-issued access token)
               step 4: pre-flight scope check on token-A claims —
                       require "hr_read_rest" in scope. If missing → 403 with copy-deck "access denied".
                       (cheap UI-bug guard; backend is still authoritative)

[Orchestrator] ─── GET /api/reports/cubicle-assignments ───► [hr_server]
                  Authorization: Bearer <token-A>
                  X-Request-ID: <rid>

[hr_server] validate_token() — F-04 six-step + Step 7 denylist (Sprint 3 add):
               (a) sig vs IS JWKS
               (b) alg=RS256, typ=JWT
               (c) iss == IS issuer
               (d) aud == hr_server expected aud
               (e) exp not expired
               (f) scope contains hr_read_rest
               (g) jti not in denylist
            return {data: [...], count: N}  (envelope locked at Stage 5)

[Orchestrator] passes the body through to SPA verbatim.

[SPA] renders sortable table (client-side sort only).
```

### Per-tab summary

| Tab | Endpoint | Scope checked at server | Backend |
|---|---|---|---|
| Pending Leaves | `GET /api/reports/leave-requests?status=pending` | `hr_read_rest` | `hr_server.get_all_leave_requests` (existing) |
| Cubicles | `GET /api/reports/cubicle-assignments` | `hr_read_rest` | `hr_server.get_all_cubicle_assignments` (NEW) |
| Devices | `GET /api/reports/device-assignments` | `it_assets_read_rest` | `it_server.get_all_device_assignments` (NEW) |

### Approve / Reject buttons in the Pending Leaves table

The buttons are **writes** with user delegation, so they take the CIBA path, not the proxied-read path. Click → SPA → orchestrator triggers HR Agent → HR Agent CIBA on `hr_approve_rest` → admin approves the consent widget → HR Server `approve_leave_request` runs. Same pattern as UC-07 asset issuance.

### Token lifecycle

Token-A's lifetime is the user's IS session (~1 h default). On expiry, the orchestrator gets a 401 from the backend, surfaces "session expired, please re-login" to the SPA, and the user re-runs Pattern C login. No new auth machinery needed for Sprint 4.

### Why not put reporting reads through CIBA too?

Reporting tables are routine, role-gated reads. They do not need per-action user consent — the token-A issued at login already carries `hr_read_rest` for HR Admin role members, and that's the same authority the user grants when they log in. Forcing CIBA on every report load would be a poor UX for the demo and is not the pattern.

### My Leaves panel (UC-13/14 surface)

Same flow with two changes:
- Endpoint: `GET /api/me/leaves`.
- Scope: `hr_self_rest` (caller's own data).
- Works for both roles. Each user sees only their own.

---

## 9. Dependencies (between stages)

```
Stage 1 (PM brief — historical)  ──┐
Stage 2 (BA UCs)                  ──┴─→ Stage 3 (this doc, binding)
                                            ↓
                                      Stage 4 (UX design)
                                            ↓
                                      Stage 5 (API design)
                                            ↓
                                      Stage 6 (tech arch)  ← username claim verification (A3)
                                            ↓
                                      Stage 7 (slice plan)
                                            ↓
                                      Stage 8 (team review)
                                            ↓
                                      Stage 9 (implementation)
                                            ↓
                                      Stages 10, 11, 12
```

**Stage 4 must answer:**
- Reports page placement (Option A inline vs Option B separate page) — recommend Option B.
- My Leaves panel placement on the home page — recommend a card below the chat input on first paint.
- Pending-Leaves Approve/Reject interaction — recommend inline form.
- Devices table drilldown — recommend inline expand row.
- Consent-widget binding-message variant for `hr_assets_write_rest` (admin-verb form).
- Multi-turn cubicle flow chat copy (4 turns: summary → floor pick → vacant list → assign).

**Stage 5 must answer:**
- Reporting response envelope shape (`{data, count}` vs `[...]`).
- `assign_cubicle` idempotency rule (recommend: same-user same-cubicle = silent success; different-user = `cubicle_already_occupied`).
- `lookup_employee(username_or_email)` return shape.
- `GET /api/me/leaves` envelope (probably re-uses the leave-requests envelope).

**Stage 6 must resolve:**
- `username` / `email` claim presence in the access token (operator A3) — actually verifyable today against the live IS at `13.60.190.47:9443` with the existing C-probe scaffolding.
- Lookup table for `username → sub` and `email → sub` in `hr_server/service/store.py`. Pre-seeded for the demo accounts.
- One-shot data migration in `it_server/service/store.py` to drop `employee_id` and key assets by `username`.

---

## 10. Risks (re-asserted post-back-track)

| # | Risk | Likelihood | Mitigation | Owner |
|---|---|---|---|---|
| R1 | `username` claim not present in IS-issued token. The agent then can't resolve "jane.doe" → user without an extra IS userinfo round-trip. | Medium | Stage 6 verifies claim presence on Day 1 via a tiny probe script (`scripts/probe-claims.sh`); if absent, operator maps `username` → access token claim set in IS Console and the script re-runs. | Tech-arch (Stage 6). |
| R2 | Scope explosion mid-implementation. | Medium | §6 scope lock binding; exceptions go through PM. | PM. |
| R3 | Cubicle concurrency bug on stage. | Low | One-line `if occupied: reject` server-side check; pick from unassigned pool during demo. | Tech-arch (Stage 6). |
| R4 | Reports / My Leaves feature creep (filters, calendars, exports). | Medium | §5 out-of-scope is binding; UX wireframes are static tables only. | UX (Stage 4) + PM challenge in Stage 8. |
| R5 | IS scope misconfig at demo time → "scope not recognized." | Low | Pre-flight script `scripts/check-is-config.sh` (Stage 9 deliverable) audits scopes / role attachments. | Tech-arch authors; operator runs Day 1 of Stage 9. |
| R6 | Reporting endpoints break the agent-CIBA narrative ("why isn't this an MCP tool?"). | Low | §8 framing in this doc; runbook copy mirrors it. | Stage 9 runbook updater. |
| R7 | Multi-turn cubicle flow looks scripted (LLM doesn't naturally produce four messages in sequence). | Medium | Use the existing keyword-fallback path on the orchestrator with explicit four-step intent labels (`cubicle_summary`, `cubicle_floor_pick`, `cubicle_vacant_list`, `cubicle_assign`). LLM kept for non-scripted niceties only. | Stage 6 chat plumbing. |

---

## 11. Stage exit gates

To exit Stage 3 (this doc) and proceed to Stage 4:
- ✅ Sprint goal locked.
- ✅ Objectives split build-new vs verify-existing, all five user back-tracks incorporated.
- ✅ External asks (A1–A5) listed with owner.
- ✅ Exit criteria testable (12 bullets).
- ✅ Out-of-scope frozen (15 items).
- ✅ Scope lock written (9-row table + role matrix).
- ✅ Identity model documented (§7).
- ✅ Reporting data-flow with hop-by-hop security walkthrough (§8) — answers user question 2.
- ✅ Stage-4 / 5 / 6 hand-off questions enumerated.
- ✅ Risks with owners.

If user has no objection, Stage 4 (UX) starts.

---

## 12. References

- [Stage 1 — PM brief (historical)](sprint-4-stage-1-product-review.md)
- [User's source draft](../use-cases/new-user-cases-after-sprint-4.md)
- UC-11..UC-16 in `docs/use-cases/`
- [Sprint 3 close](sprint-3-signoff.md)
- [Scope policy](../scope-policy.md) — locked naming convention `<resource>_<action>_rest`
- Memory: `project_sprint_3_signoff.md`, `project_introspection_deferred.md`, `feedback_compose_secret_alignment.md`.
