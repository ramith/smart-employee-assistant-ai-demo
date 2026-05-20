# Sprint 4 — Stage 6.5: Runtime Reconciliation

**Stage:** 6.5 (post Stage-8 review; reconciliation pass before Stage 9 implementation)
**Date:** 2026-05-10
**Trigger:** Stage 8 code-reviewer + architect-reviewer independently flagged that Stage 6 / 7 attached new code to **orphan modules** that the running services don't load. This pass verifies actual runtime wiring, locks resolution decisions, and amends the affected docs.
**Read order:** [`sprint-4.md`](sprint-4.md) (binding) → [`sprint-4-tech-arch.md`](sprint-4-tech-arch.md) (Stage 6, with amendment) → this doc → [`sprint-4-stage-7-slice-plan.md`](sprint-4-stage-7-slice-plan.md) (with amendments).

---

## 1. Investigation findings (verified against live code)

### 1.1 HR Server runtime wiring (`hr_server/main.py`)

`hr_server/main.py` only imports and mounts `hr_server.mcp.tools`:
- Line 36: `from hr_server.mcp.tools import HRMcpToolRouterDeps, build_hr_mcp_router`
- Line 110-113: `app.include_router(build_hr_mcp_router(...), prefix="/mcp/tools")`

The orphan modules (NOT loaded today):
- `hr_server/rest_api/server.py` — exists, defines `routes()`, never imported by `main.py`
- `hr_server/service/hr_service.py` — exists, has full leave-flow logic (`apply_leave`, `get_my_leave_requests`, `approve_leave_request`, `reject_leave_request`, `get_all_leave_requests`, `get_leaves_for_dashboard`), never imported by `main.py`
- `hr_server/service/store.py` — exists, has `users` dict and `leave_*` stores, never imported by `main.py`
- `hr_server/mcp_server/server.py` (FastMCP variant) — exists, never imported

The **wired** module today:
- `hr_server/mcp/tools.py` — Sprint 1 scaffold using `_CANNED_LEAVE_BALANCES`, `_CANNED_LEAVE_HISTORY` dicts hard-coded inline. Three routes: `get_leave_balance`, `get_leave_history`, `approve_leave` — all return canned data, NOT data from the orphan `service/store.py`.

### 1.2 IT Server runtime wiring (`it_server/main.py`)

Same shape: `mcp/tools.py` is wired (with `_CANNED_ASSIGNED_ASSETS`); `service/it_service.py` + `service/store.py` are orphans.

`it_server/auth/jwt_validator.py` is a Sprint 0 stub (`"""Sprint 0 placeholder."""`) raising `NotImplementedError`. The hr_server analog is fully implemented; the it_server side never got built.

`it_server/rest_api/server.py` exists but only exposes `/health` — no auth machinery, no business endpoints.

### 1.3 HR Agent MCP client method count

`hr_agent/mcp/client.py` exposes 3 methods (verified):
- `get_leave_balance`
- `get_leave_history`
- `approve_leave`

Sprint 4 will need 8 client methods total (the existing 3 plus):
- `apply_leave` (UC-13 chat path)
- `get_my_leave_requests` (UC-14 chat path)
- `reject_leave` (UC-15 reject button)
- `get_my_cubicle` (UC-12)
- `get_cubicle_summary`, `get_vacant_cubicles_on_floor`, `assign_cubicle`, `lookup_employee` (UC-11)

**5 new client methods needed** (the cubicle ones plus the leave-flow ones the existing scaffold canned). The HR dispatcher's `_TOOL_REGISTRY` (`hr_agent/ciba/orchestrator.py:105-124`) currently has 3 entries; needs 8.

### 1.4 IT Agent MCP client

Mirror situation. Needs `get_my_assets` (UC-12).

### 1.5 `SCOPE_ACTION_MAP` keying (`client/app.js:122`)

Keyed by **legacy short names** (`hr.read`, `hr.approve`, `hr.write`, `it.read`, `it.assign`, `directory.read`). Does NOT use the locked `_rest`-suffixed convention.

The Stage 4 UX doc assumed the new keys would slot in alongside; in fact the existing keys are an entirely different vocabulary. This is cosmetic for Sprint 4 (the SPA looks up by exact-string match; existing entries don't collide with new ones), but it leaves the codebase carrying two parallel naming conventions.

### 1.6 `get_leaves_for_dashboard` row shape (`hr_server/service/hr_service.py:274-299`)

Projection emits `{employee, type, start_date, end_date, days_requested, status}` — **no `request_id`**. Stage 5 §B2 referenced this function for the Pending Leaves report; the row shape is missing the field that A6/A7 (Approve/Reject) need to address rows.

`get_all_leave_requests` (`hr_server/service/hr_service.py:148-168`) emits the right shape (includes `request_id`) and is the correct function for B2.

### 1.7 `employee_id` test references

`grep -l employee_id tests/` returns 7 files. Total references: 43 (per code-reviewer count, verified by grep). All in test files; zero hits on the seed-literal IDs (`1042` etc.) in tests.

The migration in Sprint 4 §7 isn't a seed-literal rename; it's a **parameter / field rename** from `employee_id` to `username` across the IT MCP tool args, the HR/IT MCP client method signatures, and downstream test fixtures.

### 1.8 `JWTClaims` construction sites

`JWTClaims` is `frozen=True, slots=True` (`common/auth/models.py:81-93`). Constructed primarily at `common/auth/jwt_validator.py:382-392`. Adding new fields with defaults is safe IF every test fixture uses keyword args. A pre-S4.0 grep + verify is required.

---

## 2. Resolution decisions (locked)

### D1. Replace canned-data implementations in `hr_server/mcp/tools.py` and `it_server/mcp/tools.py`; mount `rest_api/` routers from `main.py`

**Decision:** path (a) from the code-reviewer's recommendation. The wired modules become the implementation; the canned-data dicts go away.

**Concrete steps:**
- `hr_server/mcp/tools.py` handlers replace `_CANNED_LEAVE_BALANCES[claims.sub]` style lookups with calls into `hr_service.get_my_leave_balance(claims.sub, ...)`. Tools: `get_leave_balance`, `get_leave_history` → delegate to `hr_service`. `approve_leave` already maps cleanly to `hr_service.approve_leave_request`.
- `it_server/mcp/tools.py` handlers similarly replace `_CANNED_ASSIGNED_ASSETS[claims.sub]` with calls into `it_service`.
- `hr_server/main.py` adds `app.include_router(rest_api.routes(...), prefix="")` for the new REST surfaces (`/api/me/leaves`, `/api/reports/...`, `/api/leave-requests/{id}/approve|reject`).
- `it_server/main.py` mirrors with the IT REST router.

**Why path (a):** the orphan modules were always intended as the canonical implementation. The Sprint 1 scaffold was a stop-gap. Path (b) (re-target onto canned data) would require duplicating the leave logic into the canned shape, defeating the purpose of UC-13/14 verification.

### D2. New MCP tools added to existing `hr_server/mcp/tools.py` and `it_server/mcp/tools.py`

**Decision:** extend the existing files. Do not create a new `tools_v2.py` or parallel handler module.

**Concrete:** Sprint 4 D1–D5 (HR cubicle tools) land in `hr_server/mcp/tools.py`. E1 (`get_my_assets`) lands in `it_server/mcp/tools.py`. Each handler calls the corresponding `hr_service` / `it_service` function.

### D3. Build `it_server/auth/jwt_validator.py` from scratch in S4.0; mirror `hr_server/auth/jwt_validator.py`

**Decision:** clone the working HR validator class. The existing it_server stub gets replaced.

**Concrete:** copy the `JWTValidator` class structure from `hr_server/auth/jwt_validator.py` (which works); rename the config-driven values; wire via `it_server/main.py` lifespan. Same audience-list extension applies.

This roughly **doubles S4.0 effort** versus what the original Stage 7 estimated — from ~half a day to a full day.

### D4. HR Agent MCP client extended in slices that introduce the corresponding tools

**Decision:** `hr_agent/mcp/client.py` gets new methods slice-by-slice. `it_agent/mcp/client.py` similarly.

- S4.1 (UC-11): adds `get_cubicle_summary`, `get_vacant_cubicles_on_floor`, `assign_cubicle`, `lookup_employee` to `hr_agent/mcp/client.py`.
- S4.2 (UC-12): adds `get_my_assets` to `it_agent/mcp/client.py`. Adds `get_my_cubicle` to `hr_agent/mcp/client.py`.
- S4.3 (UC-13/14): adds `apply_leave`, `get_my_leave_requests` to `hr_agent/mcp/client.py`. (The chat path needs both.)
- S4.4 (UC-15): adds `reject_leave` to `hr_agent/mcp/client.py` (approve already exists; reject is new).

`hr_agent/ciba/orchestrator.py:_TOOL_REGISTRY` updates accompany each addition.

### D5. B2 endpoint: call `get_all_leave_requests`, not `get_leaves_for_dashboard`

**Decision:** Stage 5 §B2 amendment.

**Concrete:** B2 `GET /api/reports/leave-requests?status=pending` calls `hr_service.get_all_leave_requests(status="Pending")` because that function returns rows including `request_id` — required by A6/A7 (Approve/Reject buttons). `get_leaves_for_dashboard` is left as-is for Sprint 5+ rework.

### D6. SCOPE_ACTION_MAP — defer rename to Sprint 5+

**Decision:** Sprint 4 adds new keys (`hr_apply_rest`, `hr_assets_write_rest`, `it_assets_self_rest`, `hr_approve_rest`) alongside the legacy `hr.read` / `hr.approve` / etc. Don't bulk-rename.

**Why:** the SPA does exact-string match. New keys don't collide with old keys. Renaming would touch every CIBA-emitting path simultaneously — out of Sprint 4 scope. Document the dual keying in S4.1's SPA work.

### D7. Stage 6 doc — apply groups amendment uniformly

**Decision:** the `JWTClaims.groups` field, `_derive_roles_and_scopes` helper, fail-closed-for-`groups` paths, and pre-flight `groups` claim check are ALL removed. Stage 6 amendment block at top of `sprint-4-tech-arch.md` already declares the simplification; this pass strikes through the obsolete sections inline.

### D8. S4.2 file-list — extend to capture the 43-ref `employee_id` rename

**Decision:** Stage 7 §2 S4.2 file-list adds:
- `it_server/mcp/tools.py` (rename `employee_id` arg → `username` in tool definitions)
- `it_agent/mcp/client.py` (rename arg in client method signatures)
- `hr_agent/mcp/client.py` (only the IT-related fixtures; HR's own tools don't use `employee_id`)
- 7 test files (rename usages in fixtures + assertions)

Test churn estimate updated from "~1 file" to "7 files, ~43 references."

### D9. Stage 7 §4 test-count math — update floor from 942 to 948

**Decision:** S4.0 delta moves from +6 to +12 (IT REST validator from-scratch tests + audience cap + username sanitise + main.py wiring assertion).

| Slice | Updated test delta | Cumulative |
|---|---|---|
| S4.0 | +12 (was +6) | 906 → 918 |
| S4.1 | +10 | 918 → 928 |
| S4.2 | +4 | 928 → 932 |
| S4.3 | +6 | 932 → 938 |
| S4.4 | +6 | 938 → 944 |
| S4.5 | +4 | 944 → 948 |

**Sprint 4 floor: 948 tests.**

### D10. Sprint budget — 7 working days (was 6)

**Decision:** S4.0 expands from 1 day to 1.5 days due to D3 (IT REST validator from scratch) + D7 (groups amendment cleanup) + D8 (test-churn scope). Total budget 7 working days nominal, up to 9 with slack.

### D11. Land security P1s in S4.0 / S4.4 per security audit

**Decision:** F-01 (audience-list cap), F-02 (X-Request-ID on Approve/Reject), F-03 (username/email sanitisation) are inline conditions for the slices that introduce the corresponding code. No separate slice.

---

## 3. Doc amendments applied in this pass

The following docs are updated in conjunction with this reconciliation pass:

1. **`sprint-4-tech-arch.md`** — strengthen the top-of-doc amendment to explicitly strike through the half-applied `groups` references (§2.1, §8.2, §8.3, §8.4, §9.1 obsolete).
2. **`sprint-4-stage-5-api-design.md`** — amendment note at top points B2 to `get_all_leave_requests` instead of `get_leaves_for_dashboard`.
3. **`sprint-4-stage-7-slice-plan.md`** — multi-section update:
   - S4.0 expanded scope (IT REST validator from scratch; main.py wiring; security P1 fixes)
   - S4.1+ file-lists add `hr_agent/mcp/client.py` (and `it_agent/mcp/client.py` where relevant)
   - S4.2 test-churn description corrected
   - §4 test-count math: floor 948
   - §1 sprint shape: 7 days

---

## 4. Sign-off conditions for entering Stage 9

To proceed to Stage 9, all of the following must be true:

- ✅ Stage 8 reviewers' P0 findings (orphan modules, missing client methods, IT REST stub) have explicit resolution decisions in §2 above.
- ✅ Stage 6 + 7 docs reflect the amendments per §3.
- ✅ S4.0 scope expanded to include IT REST validator from-scratch + main.py wiring + security P1 fixes + groups amendment cleanup.
- ✅ Sprint floor updated to 948 tests.
- ✅ Sprint budget updated to ~7 days nominal.

This pass produced no NEW design decisions — it reconciled the design to runtime reality. Stage 8's GO-WITH-CONDITIONS verdict applies to the post-amendment plan; conditions are the §2 decisions above, all locked.

---

## 5. Outstanding risks (after reconciliation)

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| RR-1 | Replacing canned-data implementations in `hr_server/mcp/tools.py` breaks existing UC-02/UC-03 chat paths if the swap diverges from the canned response shape. | Medium | Compare existing canned response shapes against `hr_service.get_my_leave_balance` / `get_my_leave_requests` shapes BEFORE S4.0 implementation. Adjust whichever side needs adjustment. Existing test suite (`tests/hr_server/mcp/test_tools.py`) is the regression harness. |
| RR-2 | Building the IT REST validator from scratch may diverge from the HR REST validator's behaviour in subtle ways (claim handling, error envelope). | Low | Clone HR validator's class and tests verbatim (rename module references); diff against HR's test expectations. |
| RR-3 | `hr_agent/mcp/client.py` method additions across multiple slices risk merge conflicts between slices. | Low | Single-developer execution (per Sprint 1–3 pattern); slices serial. No parallel work expected. |
| RR-4 | The 43-ref `employee_id` rename in tests may surface additional unexpected coupling (e.g. fixtures imported across test modules). | Medium | S4.2 grep + read-through is the gate. If unexpected coupling found, expand S4.2 scope to address; total sprint may slip by ~half a day. |

---

## 6. References

- [Stage 8 code review](sprint-4-stage-8-code-review.md) — the source of F-01 through F-13
- [Stage 8 architect review](sprint-4-stage-8-architect-review.md) — independent corroboration of F-A, F-B, F-C
- [Stage 8 security audit](sprint-4-stage-8-security-audit.md) — F-01/F-02/F-03 (security P1s) folded into D11
- [Stage 6 tech arch](sprint-4-tech-arch.md) — amended
- [Stage 7 slice plan](sprint-4-stage-7-slice-plan.md) — amended
- [Stage 5 API design](sprint-4-stage-5-api-design.md) — amended
