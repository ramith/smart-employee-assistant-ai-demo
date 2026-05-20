# Sprint 4 — Stage 7: Slice Plan (UC-wise)

**Stage:** 7 (slice plan — locked once Stage 8 team-review approves)
**Date:** 2026-05-10
**Branch (entry):** `sprint-3-build` @ `b497616`
**Branch (work):** `sprint-4-build` (cut at S4.0 kickoff)
**Read order:** [`sprint-4.md`](sprint-4.md) (binding) → [`sprint-4-stage-4-ux-design.md`](sprint-4-stage-4-ux-design.md) → [`sprint-4-stage-5-api-design.md`](sprint-4-stage-5-api-design.md) → [`sprint-4-tech-arch.md`](sprint-4-tech-arch.md) → this doc → Stage 8 (team review).

> **Slice principle (user lock 2026-05-10):** **slice by use case, not by technical layer.** Each slice (except S4.0 foundation) ends with one UC (or a tightly-paired UC duo) walkable end-to-end. Easier focus, better context, cleaner manual gate. Foundation work (claims, validators, pre-flight script) is concentrated in a single S4.0 so it stays out of the UC slices.
>
> **Stage 6.5 reconciliation amendments 2026-05-10:** post-Stage-8 reviewers' P0 findings folded in. See [`sprint-4-stage-6.5-reconciliation.md`](sprint-4-stage-6.5-reconciliation.md). Material changes: (a) S4.0 expanded — IT REST validator built from scratch; `hr_server/main.py` + `it_server/main.py` gain `include_router` for `rest_api/`; existing canned-data implementations in `mcp/tools.py` replaced by delegations into `service/`; security audit P1 fixes (audience-list cap, username sanitisation) land here. (b) S4.1+ file-lists add `hr_agent/mcp/client.py` + `it_agent/mcp/client.py` for new tool methods. (c) S4.2 test-churn corrected: 7 files / ~43 references for `employee_id`→`username` rename. (d) Test-count floor: 948 (was 942). (e) Sprint budget: 7 days (was 6).

After Stage 8 sign-off, Stage 9 (implementation) begins slice-by-slice. Convention from Sprint 1–3 holds: each slice ends with `tools/run-tests.sh --strict` green AND a UC walkthrough on live IS at `13.60.190.47:9443` before the next slice opens.

---

## §1. Sprint shape — 6 slices over ~6 working days

| Slice | Day | UCs walkable after | Tests after (target) |
|---|---|---|---|
| **S4.0 Foundation** (expanded after 6.5) | Day 1–1.5 | (none — claims + validators + pre-flight + IT REST validator from scratch + main.py wiring + canned-data replacement + security P1 fixes) | 906 → 918 |
| **S4.1 UC-11 — Cubicle assignment (multi-turn)** | Day 2–3 | UC-11 full 4-turn | 918 → 928 |
| **S4.2 UC-12 — Employee self-service asset discovery** | Day 3.5–4 | UC-12 dual-agent | 928 → 932 |
| **S4.3 UC-13/14 — My Leaves panel** | Day 4–5 | UC-13 chat + panel; UC-14 chat + panel | 932 → 938 |
| **S4.4 UC-15 — Pending Leaves table + Approve/Reject** | Day 5–6 | UC-15 full | 938 → 944 |
| **S4.5 UC-16 — Cubicle + Device reporting tables** | Day 6–7 | UC-16 full | 944 → 948 |

**Total budget:** ~7 working days nominal (was 6 pre-6.5), up to 9 with slack. Sprint floor 948 tests strict-mode green. If S4.3–S4.5 stretch (SPA work historically does), extend the sprint rather than cut. The `tools/run-tests.sh --strict` gate is non-negotiable per slice.

The pre-flight script `scripts/check-is-config.sh` (delivered in S4.0) runs at the start of each subsequent slice's manual gate. Any IS config drift mid-sprint is caught before it propagates.

---

## §2. Per-slice detail

### S4.0 — Foundation (Day 1–1.5; expanded after Stage 6.5 reconciliation)

**Goal:** every cross-cutting concern that every subsequent UC slice needs, plus runtime reconciliation work surfaced by Stage 8 reviewers. Done once, here, so no UC slice has to deal with foundation churn.

**Scope:**
- Identity claim plumbing: `username` + `email` flow into JWT claim model; surfaced via auth context in HR Server, IT Server, and orchestrator. **(No `groups` field — amendment.)**
- `/auth/exchange` response gains `scopes: list[str]` (no `roles`/`groups`).
- **Runtime reconciliation (post-6.5):**
  - Build `it_server/auth/jwt_validator.py:JWTValidator` from scratch (current file is a Sprint 0 stub). Mirror `hr_server/auth/jwt_validator.py:JWTValidator` exactly, with IT-specific config wiring.
  - Build IT `_authenticate` + `_AuthContext` + `_require_scope` machinery in `it_server/rest_api/server.py` (mirror `hr_server/rest_api/server.py`).
  - Add `app.include_router(rest_api.routes(...), prefix="")` to `hr_server/main.py` and `it_server/main.py` — currently neither mounts `rest_api/`.
  - Replace canned-data implementations in `hr_server/mcp/tools.py` (`_CANNED_LEAVE_BALANCES`, `_CANNED_LEAVE_HISTORY`) with delegations into `hr_service.get_my_leave_balance(...)` / `get_my_leave_requests(...)`. Same routes, same scope guards, real data.
  - Replace canned-data implementations in `it_server/mcp/tools.py` (`_CANNED_ASSIGNED_ASSETS`) with delegations into `it_service`.
- Token-validator audience extension: REST validator (both HR + IT servers) accepts a configurable list including the orchestrator MCP client ID; MCP-tool validator stays strict. New env vars `HR_SERVER_REST_VALID_AUDIENCES`, `IT_SERVER_REST_VALID_AUDIENCES`. **Audience list capped at ≤3 entries; startup log enumerates each accepted audience; fail-closed if cap exceeded** (security audit F-01).
- **Username/email sanitisation** — strip control chars + Unicode line separators; cap username at 64 chars, email at 256 (security audit F-03).
- Drop the `JWTClaims.groups` field (Stage 6 amendment cleanup) and the `_derive_roles_and_scopes` helper; replace with `_derive_scopes` returning `list[str]` from `token_a.scope.split()`.
- `scripts/check-is-config.sh` — audits scope registration, role bindings, agent app subscriptions, sample-token claim dump (no `groups` check).
- IS-side operator action: register `hr_assets_write_rest` + `it_assets_self_rest` API resource scopes; bind to roles per `sprint-4.md` §6.

**Files touched:**
- [`common/auth/models.py`](../../common/auth/models.py) — add `username`, `email` to `JWTClaims`.
- [`common/auth/jwt_validator.py`](../../common/auth/jwt_validator.py) — populate the new fields; fail-closed if absent on user-identity-bearing tokens.
- [`hr_server/auth/jwt_validator.py`](../../hr_server/auth/jwt_validator.py) — REST path accepts configurable audience list.
- [`hr_server/auth/validators.py`](../../hr_server/auth/validators.py) — `_AuthContext` exposes `username` / `email`.
- [`it_server/auth/jwt_validator.py`](../../it_server/auth/jwt_validator.py) — same audience-list extension as HR.
- [`it_server/auth/validators.py`](../../it_server/auth/validators.py) — same context surface as HR.
- [`hr_server/config.py`](../../hr_server/config.py), [`it_server/config.py`](../../it_server/config.py) — read the new env vars.
- [`orchestrator/auth/routes.py`](../../orchestrator/auth/routes.py) — extend `ExchangeResponse` with `scopes`.
- [`scripts/check-is-config.sh`](../../scripts/check-is-config.sh) — NEW.

**Tests added:**
- `tests/common/auth/test_jwt_validator.py` — claim-presence (positive + negative).
- `tests/orchestrator/auth/test_routes.py` — `ExchangeResponse.scopes` populated.
- `tests/hr_server/auth/test_jwt_validator.py` — REST path accepts orchestrator client; MCP-tool path rejects it.
- `tests/it_server/auth/test_jwt_validator.py` — same.

**Manual gate:** `scripts/check-is-config.sh` passes against live IS (or operator action documented to fix any failures). Capture output; attach to the slice PR.

**Exit:** `tools/run-tests.sh --strict` green. No UC walkable yet — foundation only.

---

### S4.1 — UC-11: HR Admin assigns cubicle (multi-turn) (Day 2–3)

**Goal:** UC-11 walkable end-to-end — the 4-turn HR-admin cubicle assignment flow with CIBA on `hr_assets_write_rest`.

**Scope:**
- HR Server cubicle data model + 5 service functions + 5 MCP tools.
- HR Agent CIBA path constructs `binding_message` AND `action_text` from looked-up employee metadata for `hr_assets_write_rest`.
- SSE `ciba_url` payload extended with `action_text`.
- A2A `ConsentRequiredPayload` extended with `action_text`.
- Keyword fallback router gains 3 cubicle intents (lookup_self in S4.2).
- SPA `renderWidget()` prefers `action_text`; amber tint for write scopes (existing convention from UC-07).

**Files touched:**
- [`hr_server/service/store.py`](../../hr_server/service/store.py) — NEW `cubicles` list seeded with 100 across 4 floors. Helper `lookup_employee_in_users(username_or_email)` against the existing `users` dict.
- [`hr_server/service/hr_service.py`](../../hr_server/service/hr_service.py) — NEW: `get_cubicle_summary()`, `get_vacant_cubicles_on_floor(floor)`, `get_my_cubicle(username)`, `assign_cubicle(cubicle_id, employee_username, employee_email, sub)`, `get_all_cubicle_assignments()`, `lookup_employee(username_or_email)`. Idempotency rule on `assign_cubicle` per Stage 5 D3.
- [`hr_server/mcp/tools.py`](../../hr_server/mcp/tools.py) — register D1 (`get_cubicle_summary` / `hr_read_rest`), D2 (`get_vacant_cubicles_on_floor` / `hr_read_rest`), D3 (`assign_cubicle` / `hr_assets_write_rest`), D4 (`get_my_cubicle` / `hr_self_rest`), D5 (`lookup_employee` / `hr_read_rest`).
- [`hr_agent/mcp/client.py`](../../hr_agent/mcp/client.py) — **NEW methods (post-6.5):** `get_cubicle_summary`, `get_vacant_cubicles_on_floor`, `assign_cubicle`, `lookup_employee`. Mirror existing client method shape (`async def name(self, ..., token, request_id) -> Pydantic_model`).
- [`hr_agent/ciba/orchestrator.py`](../../hr_agent/ciba/orchestrator.py) — extend `_TOOL_REGISTRY` (currently 3 entries) with the 4 new cubicle tool entries. For `tool=assign_cubicle`, call `lookup_employee` first, then construct binding_message + `action_text="Assign cubicle <id> to <username>"`. **action_text charset whitelist + 256-char cap** (security audit F-08).
- [`common/a2a/models.py`](../../common/a2a/models.py) — extend `ConsentRequiredPayload` with `action_text: str | None`.
- [`orchestrator/events/sse.py`](../../orchestrator/events/sse.py) — propagate `action_text` end-to-end.
- [`orchestrator/chat/keyword_fallback.py`](../../orchestrator/chat/) — add intents `cubicle.summary`, `cubicle.list_floor`, `cubicle.assign`.
- [`client/app.js`](../../client/app.js) — `renderWidget()` prefers `action_text`; falls back to `SCOPE_ACTION_MAP`.

**Tests added:**
- `tests/hr_server/service/test_cubicle_service.py` — NEW. Seed shape; summary aggregation; floor filter; assign happy path; idempotent re-assign; `cubicle_already_occupied`; non-existent cubicle; lookup_employee.
- `tests/hr_server/mcp/test_tools.py` — D1–D5 scope guards.
- `tests/orchestrator/chat/test_routes.py` — `action_text` propagates through `ciba_url`.
- `tests/orchestrator/chat/test_keyword_fallback.py` — three cubicle intents resolve.

**Manual gate:** UC-11 full 4-turn walk with `hr_admin_user`:
1. Turn 1 — "show me vacant cubicles" → counts per floor returned.
2. Turn 2 — "show me floor 2" → vacant list returned.
3. Turn 3 — "assign C-027 to jane.doe" → consent widget renders with `action_text` ("Assign cubicle C-027 to jane.doe"), amber tint.
4. Turn 4 — admin approves at IS → confirmation message.

Verify in IS audit log: `scope=hr_assets_write_rest`, `sub=hr_admin_user`. Verify HR Server in-memory store reflects the assignment.

---

### S4.2 — UC-12: Employee self-service asset discovery (Day 3.5)

**Goal:** UC-12 walkable end-to-end — the dual-agent (HR cubicle + IT laptop) self-service flow. **IT seed migration lands in this slice** because UC-12 is the first UC to exercise IT data; bundling the migration with the UC keeps churn focused.

**Scope:**
- IT seed data migration: drop `employee_id`; rekey `_SEED_ASSETS` by `username`; add `users` dict mirroring HR pattern.
- `it_service.get_my_assets(username)`.
- IT Server MCP tool E1 (`get_my_assets` / `it_assets_self_rest`).
- IT Agent CIBA path with `action_text` for `it_assets_self_rest` ("View your assigned IT equipment").
- HR `get_my_cubicle` (already shipped in S4.1) is exercised here for the HR leg of UC-12.
- Keyword fallback: `cubicle.lookup_self` intent ("where is my cubicle").
- Test churn fix: `tests/it_server/mcp/test_tools.py` updates for the new seed shape.

**Files touched (expanded post-6.5 to capture full `employee_id` rename scope across 7 test files):**
- [`it_server/service/store.py`](../../it_server/service/store.py) — rewrite `_SEED_ASSETS`: `{asset_id, username, type, model, status}` (no `employee_id`). Add `users` dict seeded for `employee_user`, `hr_admin_user`, plus 1–2 demo stubs.
- [`it_server/service/it_service.py`](../../it_server/service/it_service.py) — drop `get_employee_assets(employee_id)`; add `get_my_assets(username)`, `get_all_asset_assignments()`.
- [`it_server/mcp/tools.py`](../../it_server/mcp/tools.py) — register E1 (`get_my_assets` / `it_assets_self_rest`); rename existing tool args from `employee_id` to `username`.
- [`it_agent/mcp/client.py`](../../it_agent/mcp/client.py) — **NEW (post-6.5):** add `get_my_assets` method; rename existing `employee_id` args to `username`.
- [`it_agent/ciba/orchestrator.py`](../../it_agent/ciba/orchestrator.py) — `action_text="View your assigned IT equipment"` for `it_assets_self_rest`. Update `_TOOL_REGISTRY` for `get_my_assets`.
- [`hr_agent/mcp/client.py`](../../hr_agent/mcp/client.py) — **NEW (post-6.5):** add `get_my_cubicle` method (UC-12 HR-leg).
- [`hr_agent/ciba/orchestrator.py`](../../hr_agent/ciba/orchestrator.py) — register `get_my_cubicle` in `_TOOL_REGISTRY`.
- [`orchestrator/chat/keyword_fallback.py`](../../orchestrator/chat/) — add `cubicle.lookup_self` intent.
- **Test churn (7 files, ~43 references — corrected post-6.5):**
  - [`tests/it_server/mcp/test_tools.py`](../../tests/it_server/mcp/) — 10 refs, rename `employee_id`→`username` in fixtures + assertions.
  - [`tests/it_agent/mcp/test_client.py`](../../tests/it_agent/mcp/) — 8 refs, same pattern.
  - [`tests/it_agent/ciba/test_orchestrator.py`](../../tests/it_agent/ciba/) — 1 ref.
  - [`tests/hr_agent/mcp/test_client.py`](../../tests/hr_agent/mcp/) — 9 refs (only IT-related fixtures need rename if any pass through).
  - [`tests/hr_agent/ciba/test_orchestrator.py`](../../tests/hr_agent/ciba/) — 5 refs.
  - [`tests/hr_server/mcp/test_tools.py`](../../tests/hr_server/mcp/) — 8 refs.
  - [`tests/common/a2a/test_models.py`](../../tests/common/a2a/) — 2 refs (cosmetic dict args).

**Tests added (beyond the migration churn fix):**
- `tests/it_server/service/test_store.py` — extended for the new `users` dict.
- `tests/it_server/mcp/test_tools.py` — E1 scope guard; missing `it_assets_self_rest` returns 401.
- `tests/orchestrator/chat/test_keyword_fallback.py` — `cubicle.lookup_self` resolves.

**Manual gate:** UC-12 full dual-agent walk with `employee_user`: "where is my cubicle and what laptop do I have?" → two consent widgets sequentially (HR `hr_self_rest` then IT `it_assets_self_rest`); both approved; combined response renders. Verify IS audit log shows two CIBA events with the right scopes.

---

### S4.3 — UC-13/14: My Leaves panel (Day 4)

**Goal:** UC-13 (apply leave) and UC-14 (check leave status) gain a visible UI surface — the My Leaves panel on the home page. Backend is unchanged from Sprint 1; the panel is a thin REST adapter + SPA component.

UC-13 and UC-14 are bundled in one slice because they share the panel and the same `GET /api/me/leaves` endpoint. Splitting would be artificial.

**Scope:**
- Orchestrator-side proxy primitive (new `orchestrator/reports/proxy.py`). Cookie session → token-A → backend → pass-through. Reusable for all subsequent reporting endpoints.
- Orchestrator endpoint A1 `GET /api/me/leaves`.
- HR Server endpoint B1 `GET /api/me/leaves` (scope `hr_self_rest`, calls existing `hr_service.get_my_leave_requests(sub)`).
- SPA: My Leaves panel below chat surface; renders for both Employee and HR Admin (each sees own data only). Status pills (Pending=neutral, Approved=green, Rejected=red).
- SPA: re-fetches on `chat_message` SSE settle so chat-applied leaves appear without manual reload.

**Files touched:**
- [`orchestrator/reports/__init__.py`](../../orchestrator/reports/) — NEW package.
- [`orchestrator/reports/proxy.py`](../../orchestrator/reports/proxy.py) — NEW. The reusable proxy primitive.
- [`orchestrator/reports/routes.py`](../../orchestrator/reports/routes.py) — NEW. A1 handler.
- [`orchestrator/main.py`](../../orchestrator/main.py) — wire the new router.
- [`hr_server/rest_api/server.py`](../../hr_server/rest_api/server.py) — NEW B1 endpoint. (Router was wired into `main.py` in S4.0 reconciliation; here we just add new route handlers to it.)
- [`hr_agent/mcp/client.py`](../../hr_agent/mcp/client.py) — **NEW methods (post-6.5):** `apply_leave`, `get_my_leave_requests`. Required by UC-13/14 chat path; the Sprint 1 client only had `get_leave_balance`/`get_leave_history`/`approve_leave`.
- [`hr_agent/ciba/orchestrator.py`](../../hr_agent/ciba/orchestrator.py) — register `apply_leave`, `get_my_leave_requests` in `_TOOL_REGISTRY`.
- [`client/index.html`](../../client/index.html) — NEW My Leaves panel container below chat.
- [`client/app.js`](../../client/app.js) — `renderMyLeavesPanel()`; SSE handler re-fetches on settle.
- [`client/styles.css`](../../client/styles.css) — panel styles + status pills.

**Tests added:**
- `tests/orchestrator/reports/test_proxy.py` — NEW. Cookie auth; pre-flight scope reject; upstream 5xx → `ERR-API-PROXY-001`.
- `tests/orchestrator/reports/test_routes.py` — A1 happy path.
- `tests/hr_server/rest_api/test_my_leaves.py` — B1 returns caller's leaves only; non-self-data filtered.

**Manual gate:**
- Sign in as `employee_user`. My Leaves panel renders (possibly empty). Apply leave via chat (UC-13). Panel updates without page reload. Ask in chat "what leave have I applied" (UC-14). Reply matches panel content.
- Sign in as `hr_admin_user`. Panel shows admin's own leaves only.
- Empty state copy renders cleanly when no leaves.

---

### S4.4 — UC-15: Pending Leaves table + Approve/Reject (Day 5)

**Goal:** UC-15 walkable end-to-end — HR Admin's Pending Leaves report tab with inline Approve / Reject buttons that trigger CIBA on `hr_approve_rest`.

**Scope:**
- Reports page top-level nav entry, gated on `scopes.includes("hr_approve_rest")` (canonical "is HR Admin" probe per `sprint-4.md` §3 A3).
- Reports page route + Pending Leaves tab (Cubicles + Devices tabs are stubbed/empty here; populated in S4.5).
- Orchestrator endpoint A3 `GET /api/reports/leave-requests?status=pending` (proxy).
- HR Server endpoint B2 `GET /api/reports/leave-requests` (scope `hr_read_rest`). **Calls `hr_service.get_all_leave_requests(status=status)`** — NOT `get_leaves_for_dashboard` (latter drops `request_id`, which A6/A7 require). Stage 6.5 D5.
- Approve / Reject: dedicated REST handlers (Stage 5 Decision A — NOT chat plumbing).
  - Orchestrator A6 `POST /api/reports/leave-requests/{id}/approve`, A7 `.../reject`.
  - HR Server B4 `POST /api/leave-requests/{id}/approve`, B5 `.../reject` (token-C, scope `hr_approve_rest`).
- HR Agent CIBA `action_text` variants: `(hr_approve_rest, approve_leave_request) → "Approve <username>'s leave from <start_date>"` and reject equivalent.
- SPA Pending Leaves rows render Approve / Reject buttons (green outline / red outline). Click → REST → consent widget → success → table refetch.

**Files touched:**
- [`orchestrator/reports/routes.py`](../../orchestrator/reports/routes.py) — extend with A3, A6, A7.
- [`hr_server/rest_api/server.py`](../../hr_server/rest_api/server.py) — extend with B2, B4, B5.
- [`hr_agent/mcp/client.py`](../../hr_agent/mcp/client.py) — **NEW (post-6.5):** add `reject_leave` method (`approve_leave` already exists from Sprint 1).
- [`hr_agent/ciba/orchestrator.py`](../../hr_agent/ciba/orchestrator.py) — `action_text` variants for approve / reject. Register `reject_leave` in `_TOOL_REGISTRY`.
- [`orchestrator/reports/routes.py`](../../orchestrator/reports/routes.py) — A6/A7 handlers **require `X-Request-ID` header** (security audit F-02; mirror `/auth/logout` CSRF guard).
- [`client/index.html`](../../client/index.html) — Reports page route + tab structure.
- [`client/app.js`](../../client/app.js) — Reports page render; Pending Leaves tab; approve/reject button handlers.
- [`client/styles.css`](../../client/styles.css) — Reports page + tabs + button styles.

**Tests added:**
- `tests/orchestrator/reports/test_routes.py` — extended: A3 envelope; A6/A7 happy + denial paths.
- `tests/hr_server/rest_api/test_reports.py` — B2 happy path.
- `tests/hr_server/rest_api/test_approve_reject.py` — token-C scope check; non-`hr_approve_rest` token → 401.
- `tests/orchestrator/chat/test_routes.py` — extended: `action_text` correct for approve / reject.

**Manual gate:** UC-15 full walkthrough with `hr_admin_user`:
1. Reports nav appears in header (gated on `hr_approve_rest`).
2. Click → Pending Leaves tab → table renders with seed data ≥3 rows.
3. Click Approve on a row → consent widget appears with admin-verb text.
4. Approve at IS → row updates / disappears from pending.
5. Click Reject on another row → similar flow with reject copy.

Sign in as `employee_user`; verify Reports nav is hidden; URL-fuzz `/reports` → 403 page.

---

### S4.5 — UC-16: Cubicle + Device reporting tables (Day 6)

**Goal:** UC-16 walkable end-to-end — Cubicles and Devices tabs on the Reports page rendering the full assignment dataset.

**Scope:**
- Orchestrator A4 `GET /api/reports/cubicle-assignments`, A5 `GET /api/reports/device-assignments` (both proxied).
- HR Server B3 `GET /api/reports/cubicle-assignments` (calls `get_all_cubicle_assignments` shipped in S4.1).
- IT Server C1 `GET /api/reports/device-assignments` (calls new `it_service.get_all_asset_assignments()`).
- SPA Reports page Cubicles tab + Devices tab. Devices tab has Type filter dropdown + inline row drilldown for "all assets for this employee".
- Identity surfaces are `username` + `email` in every row (no `sub`, no `employee_id`).

**Files touched:**
- [`orchestrator/reports/routes.py`](../../orchestrator/reports/routes.py) — extend with A4, A5.
- [`hr_server/rest_api/server.py`](../../hr_server/rest_api/server.py) — extend with B3.
- [`it_server/rest_api/server.py`](../../it_server/rest_api/server.py) — NEW C1 endpoint.
- [`it_server/service/it_service.py`](../../it_server/service/it_service.py) — `get_all_asset_assignments()`.
- [`client/app.js`](../../client/app.js) — Cubicles tab render; Devices tab render (filter + drilldown).

**Tests added:**
- `tests/orchestrator/reports/test_routes.py` — extended: A4, A5 envelopes.
- `tests/hr_server/rest_api/test_reports.py` — extended: B3.
- `tests/it_server/rest_api/test_reports.py` — NEW: C1.

**Manual gate:** UC-16 full walkthrough with `hr_admin_user`:
1. Click Cubicles tab → ≥10 assigned cubicles render with username/email/floor.
2. Click Devices tab → seed assets render with username/email/type/model.
3. Click Type filter dropdown → list filters to one type.
4. Click an employee row in Devices → inline drilldown expands showing all assets for that user.

---

## §3. Cross-cutting concerns (slice-independent)

### Token validation amendment (delivered in S4.0)

Per Stage 6 OQ-3, the REST validator's audience list is configurable (env `*_REST_VALID_AUDIENCES`); the MCP-tool validator stays strict. S4.0 lands both for HR + IT. Every subsequent slice can assume the REST and MCP boundaries are wired.

### `username` / `email` fail-closed (delivered in S4.0)

If a user-identity-bearing token reaches a server with absent claims, server returns 401 `ERR-AUTH-claim-missing`. S4.0's `tests/common/auth/test_jwt_validator.py` covers this.

### IT seed migration is one-shot (delivered in S4.2)

S4.2 deletes the legacy `employee_id` field. No feature flag, no compatibility branch. Test fixes land in the same slice. Pre-S4.2 CI may break for the duration of the slice; post-S4.2 CI is restored.

### SSE / A2A `action_text` is additive (introduced in S4.1)

S4.1 adds the field. Any code path that doesn't construct an `action_text` falls back to the legacy `SCOPE_ACTION_MAP` in the SPA. No backward-compat work needed because client + servers ship in lockstep.

### `/auth/exchange` `scopes` field is additive (delivered in S4.0)

Old SPA builds (if any are floating) ignore it. New SPA builds rely on it. No backward-compat branch needed.

---

## §4. Test-count math

| Slice | New tests | Cumulative |
|---|---|---|
| S4.0 | +12 (post-6.5; claims + HR audience + IT validator from-scratch + IT audience + exchange + audience cap + username sanitise) | 906 → 918 |
| S4.1 | +10 (cubicle service: 5; MCP scopes: 3; action_text + keyword: 2) | 918 → 928 |
| S4.2 | +4 (E1 scope guard; users dict; lookup_self; existing test rename covers existing count) | 928 → 932 |
| S4.3 | +6 (proxy: 3; routes: 1; B1: 2) | 932 → 938 |
| S4.4 | +6 (A3/A6/A7 + B2/B4/B5 + action_text variants + X-Request-ID guard) | 938 → 944 |
| S4.5 | +4 (A4/A5/B3/C1) | 944 → 948 |

**Sprint 4 floor: 948 tests** (post-6.5 amendment; was 942 pre-6.5). Strict-mode `tools/run-tests.sh` is the gate. Any `failed` / `error` / `xfailed` keyword in a file summary fails CI.

---

## §5. Dependencies and parallelism

S4.0 is foundational; every UC slice depends on it. UC slices themselves are mostly independent post-foundation, BUT we keep them serial for execution-quality reasons:

```
S4.0 (foundation)
  ↓
S4.1 (UC-11)
  ↓
S4.2 (UC-12)         ← depends on S4.1 for SSE action_text plumbing
  ↓
S4.3 (UC-13/14)      ← depends on S4.0 for orchestrator-proxy primitive (lands here)
  ↓
S4.4 (UC-15)         ← depends on S4.3 for proxy primitive + Reports page scaffold
  ↓
S4.5 (UC-16)         ← depends on S4.4 for Reports page tabs scaffold
```

Why serial despite logical independence:
- Each slice ends with a UC walkable, and we want a clean manual gate per slice without coupling cognitive load across UCs.
- SPA changes in S4.3 / S4.4 / S4.5 share files (`client/app.js`, `client/styles.css`); avoiding parallel SPA work prevents merge friction.
- The reusable proxy primitive (introduced in S4.3) is consumed by S4.4 and S4.5; building UC-by-UC means the primitive is exercised under increasing load before it must support all reporting endpoints.

---

## §6. Slice-level rollback strategy

Each slice is a single PR (or branch merge into `sprint-4-build`). If a slice's manual gate fails, **revert the slice merge** rather than patching forward. Sprint 3 hardening pass established this discipline: a clean revert is cheaper than a surgical mid-slice patch.

The IS-side scope/role configuration changes from S4.0 are **not** rolled back automatically; the operator removes the new scope registrations only if the sprint is fully abandoned. For mid-sprint reverts, the IS config can stay (a token with an unused scope is harmless).

---

## §7. Stage 8 (team review) hand-off

Stage 8 reviews this slice plan plus the four predecessor docs (`sprint-4.md`, Stage 4 UX, Stage 5 API, Stage 6 tech-arch). Reviewers needed:
- **Architect** (`architect-reviewer`) — sanity-check the slice ordering and OQ-3 audience-list lock.
- **Code reviewer** (`code-reviewer`) — eyeball the file-touch lists for missing impacts.
- **Security auditor** (`security-auditor`) — verify the audience-list relaxation is bounded (REST path only, MCP path strict).

Stage 8 must produce a single GO / GO-WITH-CONDITIONS / NO-GO verdict per reviewer. Conditions land either before S4.0 kickoff or in the slice that introduces the relevant code.

---

## §8. References

- [Stage 3 binding plan](sprint-4.md)
- [Stage 4 UX](sprint-4-stage-4-ux-design.md)
- [Stage 5 API](sprint-4-stage-5-api-design.md)
- [Stage 6 tech arch](sprint-4-tech-arch.md)
- [Sprint 3 close](sprint-3-signoff.md) — 906/49 strict-green baseline
- [Scope policy](../scope-policy.md)
- Memory: `feedback_compose_secret_alignment.md`, `project_sprint_3_signoff.md`.
