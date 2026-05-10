# UC-16 — HR Admin reporting tables: cubicle and device assignments

> **Build status (revised after Stage 3 user back-track 2026-05-10):** **NEW — REST endpoints + GUI tables. Uses existing scopes `hr_read_rest` (cubicles) and `it_assets_read_rest` (devices). NO new IT scope is introduced for this UC. Identity surfaces are `username` + `email`; the legacy `employee_id` field is dropped (see [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §7).**
>
> Combines the user's original draft cases for "table of cubicle assignments" and "table of laptop assignments" (`new-user-cases-after-sprint-4.md` case 6). Same actor, same Reports page, same orchestrator-proxied REST pattern.

**Sprint:** 4
**Priority:** High
**Maps to N-tests:** TBD — Stage 10 (estimate: R-REPORTS-2 cubicle-assignments table, R-REPORTS-3 device-assignments table, plus 403 negative cases under R-REPORTS-4)
**Maps to scenarios:** Sprint 4 Act III (HR Admin reporting — tabs 2 and 3 of three)

## Actors
- **Primary:** HR Admin user (`hr_admin_user` in dev)
- **Secondary:** SPA (Reports page, Cubicles tab, Devices tab), Orchestrator (proxy), HR Server, IT Server, WSO2 IS (token validation only — no CIBA)

## No agent engagement, no CIBA — design rationale

Reporting tables are routine, role-gated reads. They do **not** go through any agent (HR Agent / IT Agent) and do **not** trigger CIBA. The HR Admin is reading aggregate assignment data in their own authorised capacity using the token-A obtained at login (which carries `hr_read_rest` and `it_assets_read_rest` because the user holds the HR Admin role). The path is orchestrator-proxied per [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §8 — token-A stays orchestrator-side; the SPA holds only a session cookie.

This is consistent with the demo's identity-first narrative: *"agents handle delegated, per-action consent flows; reporting tables are direct reads where the admin's role grants the authority — no extra delegation needed."*

## Trigger
HR Admin navigates to the SPA Reports page (defined in UC-15) and clicks either the **Cubicles** tab or the **Devices** tab.

---

## Part A — Cubicle Assignments Table

### Preconditions (Part A)
- `hr_admin_user`'s session token-A carries `hr_read_rest` (existing).
- HR Server exposes `GET /api/reports/cubicle-assignments` (NEW endpoint, Sprint 4). Recommended envelope: `{data: [{employee_username, employee_email, cubicle_id, floor, assigned_at}], count: N}`.
- HR Server in-memory cubicle store (`store.cubicles`) is seeded with at least 10 assigned cubicles (via UC-11 demo walks or seed script for the automated test run).

### Main flow (Part A)
1. HR Admin clicks the **Cubicles** tab on the Reports page.
2. SPA → orchestrator: `GET /api/reports/cubicle-assignments`, `Cookie: orchestrator_session=<sid>`.
3. Orchestrator session lookup; assert `Session.terminating == False`; read `Session.token_a`. Pre-flight scope check: `hr_read_rest` in token-A claims. If not, 403.
4. Orchestrator → HR Server: `GET /api/reports/cubicle-assignments` with `Authorization: Bearer <token-A>`.
5. HR Server runs `validate_token` (F-04 + Step 7 denylist): sig, alg, iss, aud, exp, scope contains `hr_read_rest`, jti not in denylist.
6. HR Server calls new `hr_service.get_all_cubicle_assignments()` → filters `store.cubicles` for `occupied=True` → returns rows containing `employee_username`, `employee_email`, `cubicle_id`, `floor`, `assigned_at`. (`assigned_to_sub` is in the underlying record but never returned — internal join only.)
7. Orchestrator passes the body through. SPA renders sortable table with columns: **Username** | **Email** | **Cubicle ID** | **Floor** | **Assigned At**.
8. Sortable client-side by Floor; no server re-query.

### Exception flows (Part A)

#### EX-A1 — No cubicles assigned yet
1. `get_all_cubicle_assignments()` returns `[]`.
2. SPA renders: *"No cubicles have been assigned yet."*

#### EX-A2 — Non-admin requests cubicle assignments
1. `employee_user` (cookie session) issues `GET /api/reports/cubicle-assignments`.
2. Orchestrator pre-flight scope check (step 3 above): `hr_read_rest` not in token-A → 403 returned to SPA without contacting HR Server.
3. SPA navigation hides the Reports item from non-admins as first-line guard.

---

## Part B — Device (laptop / IT asset) Assignments Table

### Preconditions (Part B)
- `hr_admin_user`'s session token-A carries `it_assets_read_rest` (existing — HR Admin role already holds this per `docs/scope-policy.md`).
- IT Server exposes `GET /api/reports/device-assignments` (NEW endpoint, Sprint 4). Recommended envelope: `{data: [{employee_username, employee_email, asset_id, type, model, status}], count: N}`.
- IT Server `store.assets` (rewritten in Sprint 4) is seeded with assets keyed by `username` directly — see [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md) §7. The legacy `employee_id` field is removed.

### Main flow (Part B)
1. HR Admin clicks the **Devices** tab on the Reports page.
2. SPA → orchestrator: `GET /api/reports/device-assignments`, cookie auth.
3. Orchestrator session lookup; pre-flight scope check: `it_assets_read_rest` in token-A. If not, 403.
4. Orchestrator → IT Server: `GET /api/reports/device-assignments` with `Authorization: Bearer <token-A>`.
5. IT Server `validate_token` (F-04 + Step 7 denylist) — `it_assets_read_rest` required. Calls new `it_service.get_all_asset_assignments()` → returns all assets, each row carrying `employee_username` and `employee_email` (looked up from the user table seeded alongside `_SEED_ASSETS`).
6. Orchestrator passes through. SPA renders sortable table with columns: **Username** | **Email** | **Asset ID** | **Type** | **Model** | **Status**.
7. Filterable client-side by Type (laptop / phone / monitor / headset).

### Exception flows (Part B)

#### EX-B1 — No assets assigned
1. Empty list returned. SPA: *"No IT assets have been assigned yet."*
2. Should not occur in normal demo — `_SEED_ASSETS` provides several rows from startup.

#### EX-B2 — Non-admin requests device assignments
1. `employee_user` (cookie session) → orchestrator pre-flight: `it_assets_read_rest` IS in their token (per scope-policy.md, both roles hold it for the existing `it.list_available_assets` flow).
2. **However**, the demo's intent is that only HR Admin sees the Devices report (employee should use UC-12 self-service for their own assets). The Reports page hides the tab from non-admins; the orchestrator pre-flight does not by itself reject employees here.
3. **Stage 6 must decide**: either (a) accept that employees could access the Devices report if they URL-fuzz the SPA — low-risk because the data is read-only and they already had the scope for `list_available_assets`; or (b) tighten the orchestrator pre-flight to require BOTH `it_assets_read_rest` AND a role-marker claim (e.g. `groups` claim contains `HR Admin`). Recommend option (a) for Sprint 4 simplicity and document the decision; revisit in Sprint 5+.

---

## Postconditions (UC-level)
- **Success (both tabs):** tables rendered with current assignment data; no data mutation; HR Admin has a read-only audit view of all allocations.
- **Failure:** error or empty-state message; no data exposed to unauthorised users beyond what their existing scope already permits.

---

## Drilldown design — query devices by employee

User's draft also asks: *"HR admin can query for devices allocated for a particular user (identified by either username, firstname + last name)."*

**Recommendation: inline row-expansion drilldown on the Devices tab, not a third tab.**

Rationale:
- A third tab adds a search input, separate API call, separate empty/error state — surface area for a 90-second demo with limited payoff.
- Drilldown reuses already-loaded data: HR Admin clicks an employee row in the Devices table → inline sub-row expands showing all assets for that user (filtered client-side).
- For the cubicle table, an employee has at most one cubicle; the row already contains the answer — no drilldown needed.

**Stage 4 UX confirms** the drilldown affordance (recommend inline `<details>` row).

---

## Design notes for downstream stages

### UX (Stage 4)
- Cubicles tab columns: Username | Email | Cubicle ID | Floor | Assigned At. Sort by Floor (client-side).
- Devices tab columns: Username | Email | Asset ID | Type | Model | Status. Filter by Type (client-side dropdown).
- Row-detail drilldown on Devices: click row → inline sub-row expansion. Client-side filter, no new API call.
- Reports page navigation hides for non-admins (no `hr_read_rest` in token-A claims).
- Paging not required at demo scale (≤100 cubicles, ≤30 assets seeded).

### Architecture (Stage 6)
- New REST endpoints — orchestrator side: `GET /api/reports/cubicle-assignments`, `GET /api/reports/device-assignments`. Cookie auth + token-A pre-flight scope check + proxy.
- New REST endpoints — backend side: same paths on `hr_server` (scope `hr_read_rest`) and `it_server` (scope `it_assets_read_rest`). Bearer token-A auth.
- New service functions:
  - `hr_service.get_all_cubicle_assignments()` — reads `store.cubicles` filtered by `occupied=True`, projects only the safe surface columns (no `assigned_to_sub`).
  - `it_service.get_all_asset_assignments()` — reads `store.assets` (rewritten Sprint 4 to key by `username`), joins user info from `store.users` for `email`.
- **Identity surface lock:** every reporting row exposes `username` + `email`. **Never** `sub`. **Never** `employee_id` (the field doesn't exist after Sprint 4 data migration).
- Pre-seeded `store.users` in `it_server` mirrors the seed in `hr_server` so name resolution doesn't require IS round-trips.

### Testing (Stages 10–11)
- **R-REPORTS-2** automated: `hr_admin_user` cookie session → cubicle-assignments endpoint → ≥10 rows; columns match shape (username, email, cubicle_id, floor, assigned_at); HTTP 200.
- **R-REPORTS-3** automated: `hr_admin_user` cookie session → device-assignments endpoint → seeded asset rows; columns match shape; HTTP 200.
- **R-REPORTS-4** automated: cubicle endpoint without `hr_read_rest` → 403 from orchestrator pre-flight, before backend.
- **Manual (Stage 11):** sign in as `hr_admin_user`; verify both tabs render; click a Devices row to expand drilldown; confirm only that user's assets appear in the expansion.
