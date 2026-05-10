# Sprint 4 — Stage 11: Manual Gate Runbook

**Stage:** 11 (manual gate against live IS at `13.60.190.47:9443`)
**Date:** 2026-05-11 (prep)
**Branch:** `sprint-4-build` @ `48edeea` (988/60 strict-green)
**Read order:** [`sprint-4.md`](sprint-4.md) §3 (asks A1–A5) §4 (exit criteria) → this doc → run.

This is the operator + tester runbook for the Sprint 4 live walkthrough. It is structured so one person (or two — operator + observer) can run it from start to finish in ≤ 30 minutes. Each UC has its exact chat copy + acceptance criteria; failures map to a recovery action in §6.

---

## §1. Operator pre-flight — IS Console actions (do BEFORE the demo)

These are the **A1 + A2 + A3** asks from `sprint-4.md` §3. Without them, S4.1+ slices fail at consent-screen time.

### 1.1 Register two new API resource scopes

In IS Console at `https://13.60.190.47:9443/console`:

| Scope | API resource | Display name (suggested) |
|---|---|---|
| `hr_assets_write_rest` | `hr_server-api` | "Assign HR assets (cubicles)" |
| `it_assets_self_rest` | `it_server-api` | "View own IT assets" |

**Path:** Console → API Resources → select the resource → Scopes tab → New Scope.

### 1.2 Bind scopes to roles

The locked role-scope matrix (sprint-4.md §6):

| Role | Scopes (Sprint 4 deltas in **bold**) |
|---|---|
| Employee | `hr_basic_rest`, `hr_self_rest`, `hr_apply_rest`, `it_assets_read_rest`, **`it_assets_self_rest`** |
| HR Admin | _all of Employee's_ + `hr_read_rest`, `hr_approve_rest`, **`hr_assets_write_rest`**, `it_assets_write_rest` |

**Path:** Console → User Management → Roles → select role → Permissions tab → add scopes.

### 1.3 Confirm `username` + `email` claims flow into the access token

Sprint 4 keys business data by `username` (with `email` as a secondary identifier). Both must appear in the IS-issued access token claim set.

**Path:** Console → API Resources → `hr_server-api` (and `it_server-api`) → Properties tab → Authorized API Calls → confirm the `profile` and `email` OIDC scopes are mapped into access-token claims.

(If IS surfaces a "default attribute mapping" toggle, ensure `Username Claim` is mapped under the `username` key and not just `preferred_username`. WSO2 IS 7.x defaults vary; the script in §1.4 catches misconfiguration.)

### 1.4 Run the pre-flight script

```bash
cd /Users/ramith/demo/dda-poc/iam-ai-samples/smart-employee-agent
./scripts/check-is-config.sh
```

Expected: `PASS: N | FAIL: 0 | WARN: 0` (or only WARNs on operator-discretion items). Any FAIL means stop and fix before continuing.

The script audits:
1. Connectivity to `13.60.190.47:9443/oauth2/jwks`.
2. JWKS keys array non-empty.
3. The two new scopes are registered as API resource scopes.
4. A sample access token (issued via `client_credentials` against `IS_ADMIN_CLIENT_*` env vars) carries `username` + `email`.

If check 4 fails, IS attribute mapping for `username`/`email` needs fixing (see §1.3) before re-running.

### 1.5 Verify both demo accounts are alive

| Username | Role | Email |
|---|---|---|
| `employee_user` | Employee | `employee@example.com` (or whatever IS holds) |
| `hr_admin_user` | HR Admin | `hr.admin@example.com` |

Sign in to each via the SPA (`/`) once, sign out, confirm no account-locked banner. Then sign out again so the demo starts clean.

---

## §2. Service smoke (do BEFORE each rehearsal)

Bring the fleet up:

```bash
cd /Users/ramith/demo/dda-poc/iam-ai-samples/smart-employee-agent
docker compose up -d --force-recreate
```

`--force-recreate` matters — partial restarts split the fleet on the `INTERNAL_REVOKE_SHARED_SECRET` env var (memory: `feedback_compose_secret_alignment.md`).

Verify:

```bash
# 1. All 5 services healthy
for svc in orchestrator hr_agent hr_server it_agent it_server; do
  echo -n "$svc: "
  curl -sk http://localhost:$(docker compose port $svc 8000 2>/dev/null | cut -d: -f2)/healthz 2>&1 | head -1
done

# 2. denylist_enforcement=on on both MCP servers
docker compose logs hr_server it_server 2>&1 | grep -E "denylist_enforcement|expected_audiences"

# 3. Sprint 4 audience-list lock printed at startup
# Expected: each REST validator emits "validator.startup expected_audiences=[...]"
# with ≤ 3 entries per server (security audit F-01 cap).
docker compose logs hr_server 2>&1 | grep -E "expected_audiences|REST validator"
docker compose logs it_server 2>&1 | grep -E "expected_audiences|REST validator"
```

If any of these fail: see §6 failure modes.

---

## §3. UC walkthroughs

Each UC has: **trigger** (exact chat copy), **expected behaviour**, **acceptance criteria** (one per EC4-N where applicable).

> Use **two browser windows** (incognito + normal, or two profiles) so you can switch between `employee_user` and `hr_admin_user` without sign-out churn. The orchestrator session is HttpOnly-cookie scoped, so two browsers = two independent sessions.

### UC-13/14 — Employee leave application + My Leaves panel (5 min)

**Sign in as `employee_user`.**

1. Confirm the **My Leaves panel** appears below chat. Empty state should read: *"You have no leave requests yet. Ask the HR agent to apply for one."* (EC4-9)

2. Type in chat:

   > *"I'd like to take 3 days annual leave starting 2026-06-15."*

3. Approve the consent widget (`hr_apply_rest`). Wait for chat reply.

4. Verify the My Leaves panel re-renders (without page reload) and the new request appears with status = **Pending** (neutral pill). (EC4-7)

5. Type in chat:

   > *"What leave have I applied for?"*

6. Approve the consent widget (`hr_self_rest`). Verify the chat reply matches the row visible in the My Leaves panel. (EC4-8)

**Acceptance:** all 6 steps pass. Status pill is neutral (Pending). Panel + chat agree on row count.

---

### UC-11 — HR Admin assigns cubicle (multi-turn) (5 min)

**Sign in as `hr_admin_user`** (in a separate browser window).

1. Confirm the **Reports** button appears in the header (gated on `hr_approve_rest`). Don't click it yet.

2. **Turn 1** — type in chat:

   > *"Show me vacant cubicles."*

   Expected reply: *"Vacant cubicles by floor: Floor 1 — 25 of 25, Floor 2 — 25 of 25, Floor 3 — 25 of 25, Floor 4 — 25 of 25. Which floor would you like to pick from?"* (counts vary if you've previously assigned; the format is the lock.)

3. **Turn 2** — type:

   > *"Show me floor 2."*

   Expected: *"Floor 2 has 25 vacant cubicles available: C-026 through C-050. Which one would you like to assign, and to whom?"*

4. **Turn 3** — type:

   > *"Assign C-027 to jane.doe."*

5. Consent widget appears with **action_text** showing *"Assign cubicle C-027 to jane.doe"* (admin-verb form, **amber tint** for write scope). Approve. (EC4-1)

6. **Turn 4** — verify chat reply: *"Cubicle C-027 on floor 2 has been assigned to jane.doe."*

7. Verify the IS audit log (Console → Audit Logs) shows the CIBA event with `scope=hr_assets_write_rest` and `sub=hr_admin_user` UUID.

**Acceptance:** action_text is admin-verb form (not user-self form); amber tint visible; assignment confirmed in the chat reply.

**Sub-check (EC4-2 — role denial):** sign back in as `employee_user`, type *"Assign C-028 to bob.smith."* The CIBA must fail at IS with `invalid_scope` / `access_denied`. Orchestrator should surface a copy-deck-style denial message. (No widget should reach DONE.)

---

### UC-12 — Employee self-service asset discovery (5 min)

**Sign in as `employee_user`** (the original window from UC-13).

1. Type in chat:

   > *"Where is my cubicle and what laptop do I have?"*

2. **First widget** appears for HR Agent (`hr_self_rest` — neutral tint). Action text: *"View your cubicle assignment"*. Approve.

3. Wait for HR leg to complete; **second widget** appears for IT Agent (`it_assets_self_rest` — neutral tint). Action text: *"View your assigned IT equipment"*. Approve.

4. Combined chat reply: *"Your cubicle is C-001 on floor 1. Your assigned laptop is a [model] (AST-XXXXX), currently outstanding."* (Specific cubicle/asset depends on seed; the format is the lock.) (EC4-3)

**Acceptance:** two consecutive widgets (never overlapping), both neutral-tinted (read scope), combined reply names cubicle + asset by `username`. **No `employee_id`** appears anywhere.

**Sub-check (EC4-3 partial denial):** redo the dual flow but **deny** the IT widget. Reply should preserve cubicle answer + apologise about IT. ("Your cubicle is C-001 on floor 1. I couldn't retrieve your IT assets because you declined the authorisation.")

---

### UC-15 — HR Admin Pending Leaves table + Approve / Reject (5 min)

**Sign in as `hr_admin_user`** (window from UC-11, still alive).

1. Click **Reports** in the header. Reports view opens. Default tab = **Pending Leaves**.

2. Verify the table renders with at least the leave applied in UC-13 visible. Columns: Request ID, Username, Email, Leave Type, Days, Start, Actions. (EC4-4)

3. Click **Approve** on the row from UC-13 (employee_user's leave).

4. Consent widget appears with action_text: *"Approve employee_user's leave from 2026-06-15"* (or however the date renders). **Amber tint** (admin-write). Approve.

5. After widget settles, Pending Leaves table re-fetches automatically. The approved row should disappear (no longer Pending).

6. Switch to the `employee_user` browser. The My Leaves panel re-renders on next chat_message; or sign in again. Confirm the row's status pill is now **green** (Approved).

7. Back to admin: apply a new leave as `employee_user` (use UC-13 again with different dates), come back, click **Reject** on the new row, supply reason "Insufficient notice", confirm. The row disappears from Pending.

**Acceptance:** Approve/Reject buttons trigger CIBA on `hr_approve_rest`; action_text uses the admin-verb form including the username + start_date; the table re-fetches on settle.

**Sub-check (EC4-5 non-admin 403):** in `employee_user` window, navigate the URL bar to `https://localhost:<port>/api/reports/leave-requests?status=Pending` directly. Should get **403** (orchestrator pre-flight rejects before backend round-trip). The Reports button should already be hidden in the employee header.

**Sub-check (security F-02 CSRF guard):** in browser DevTools console, run:
```js
fetch("/api/reports/leave-requests/LR001/approve", {method: "POST", credentials: "include"})
  .then(r => r.status)  // expect 400 (X-Request-ID header missing)
```

---

### UC-16 — HR Admin cubicle + device reporting tables (3 min)

Still as `hr_admin_user`, on Reports view:

1. Click **Cubicles** tab. Table renders with at least one assignment from UC-11 (jane.doe → C-027 → floor 2). Columns: Username, Email, Cubicle ID, Floor, Assigned At. **No `sub` column. No `employee_id` anywhere.**

2. Click **Devices** tab. Type filter dropdown above the table; defaults to "All".

3. Verify table renders with seeded asset rows (laptops, phones, etc.). Columns: Username, Email, Asset ID, Type, Model, Status. **No `sub`. No `employee_id`.**

4. Click the Type filter, select "laptop". Table re-renders showing only laptops (client-side filter; no extra API call — verify via DevTools Network tab if curious).

5. Click on any **username cell** (not a button). Inline drilldown row expands below it showing all assets for that user. Click again — drilldown collapses.

**Acceptance:** Both tabs render with `username` + `email` only; type filter narrows the device list; drilldown expand/collapse works. (EC4-4)

---

## §4. Sign-off checklist

Tick each box as you complete it. All twelve must pass to proceed to Stage 12.

- [ ] **EC4-1** — UC-11 multi-turn cubicle assignment (4 turns) succeeds; p95 final-step latency ≤ 2 s.
- [ ] **EC4-2** — Employee attempting `assign_cubicle` is denied at IS; copy-deck message surfaced.
- [ ] **EC4-3** — Employee dual-agent self-service returns cubicle + laptop within 1 s/agent.
- [ ] **EC4-4** — All three Reports tabs render with seed data (Pending Leaves, Cubicles, Devices).
- [ ] **EC4-5** — Non-admin URL-fuzz to `/reports/...` returns 403; Reports button hidden in employee header.
- [ ] **EC4-6** — Already covered by automated test (`test_hr_assets_write_token_cannot_call_self_endpoint`); reverify with strict-mode gate in §5.
- [ ] **EC4-7** — UC-13 chat-applied leave appears in My Leaves panel without manual reload.
- [ ] **EC4-8** — UC-14 chat status reply matches My Leaves panel content.
- [ ] **EC4-9** — My Leaves panel renders on first paint with empty-state copy on a fresh user.
- [ ] **EC4-10** — `tools/run-tests.sh` strict-green at 988/60.
- [ ] **EC4-11** — Demo runbook narrates the three new acts in ≤ 4 minutes wall-clock.
- [ ] **EC4-12** — `sprint-4-signoff.md` lists all 6 UCs with build-status + verifying tests (Stage 12 deliverable).

---

## §5. Pre-tear-down — re-verify the gate

```bash
./tools/run-tests.sh
# Expected:
# Files passed: 60    Files failed: 0
# Total tests:  988
```

Capture this output for the sign-off doc.

---

## §6. Failure modes

| Symptom | Likely cause | Operator move |
|---|---|---|
| Pre-flight script: scope `hr_assets_write_rest` not registered | A1 not done | Console → API Resources → `hr_server-api` → Scopes → New |
| Pre-flight script: sample token missing `username` claim | A3 attribute mapping not done | Console → OIDC Scopes / API Resource Properties — see §1.3 |
| Reports button missing for `hr_admin_user` | `scopes.includes("hr_approve_rest")` returned false; A2 incomplete | Console → User Management → Roles → HR Admin → Permissions → add `hr_approve_rest`. Then sign out + back in (the SPA caches scopes from `/auth/exchange`). |
| Approve/Reject button: 400 "X-Request-ID required" | SPA bug — file ticket | Re-verify SPA app.js shipped `_ridGen()` integration |
| UC-12 IT leg fails: "scope `it_assets_self_rest` not recognized" | A1/A2 not done for IT | Mirror §1.1/§1.2 for the IT side |
| Cubicle assignment widget: action_text reads "Assign cubicle to <empty>" | `lookup_employee` failed; username typo | Tell admin to use a seeded demo user (`jane.doe` / `bob.smith`) |
| `denylist_enforcement=off` at startup | Sprint 3 wiring regression | Stop. Don't demo this build. |
| Cascade fan-out 401 `invalid_secret` | Secret drift | `docker compose up -d --force-recreate` (memory: `feedback_compose_secret_alignment.md`) |

---

## §7. After the gate — handoff to Stage 12

When all twelve EC4 boxes are ticked:
1. Mark this doc complete (add a `## §8. Run log` section with the date + your initials + any notes).
2. Update [`docs/use-cases/UC-11..UC-16`](../use-cases/) status fields to "verified live" if you want.
3. Move to Stage 12 — say "go for retro + signoff" and I'll draft `sprint-4-signoff.md` + spawn the retro agents.

---

## §8. Quick-reference — chat copy cheat sheet

Print this and keep on screen during the demo:

```
UC-13 (employee):     "I'd like to take 3 days annual leave starting 2026-06-15."
UC-14 (employee):     "What leave have I applied for?"
UC-11 turn 1 (admin): "Show me vacant cubicles."
UC-11 turn 2 (admin): "Show me floor 2."
UC-11 turn 3 (admin): "Assign C-027 to jane.doe."
UC-12 (employee):     "Where is my cubicle and what laptop do I have?"
UC-11 EX-4 (employee, expect denial):
                      "Assign C-028 to bob.smith."
```

---

## §9. References

- [Stage 3 binding plan](sprint-4.md) §3 (asks) §4 (exit criteria)
- [Stage 6 tech-arch](sprint-4-tech-arch.md) §9 (pre-flight script spec)
- [Stage 7 slice plan](sprint-4-stage-7-slice-plan.md)
- [Stage 8 security audit](sprint-4-stage-8-security-audit.md) F-02 (CSRF), F-08 (action_text), F-01 (audience cap)
- [Stage 10 test coverage](sprint-4-stage-10-test-coverage.md)
- Existing `docs/demo-runbook.md` (Sprint 1 + Sprint 3 acts; Sprint 4 acts will fold in post-Stage-11 sign-off)
- Memory: `feedback_compose_secret_alignment.md`
