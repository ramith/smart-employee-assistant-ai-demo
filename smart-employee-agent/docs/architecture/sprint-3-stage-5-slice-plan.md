# Sprint 3 — Stage 5: Slice Plan

**Date:** 2026-05-09
**Branch:** `sprint-3-build` (origin tracked).
**Inputs:** [Stage 1](sprint-3-stage-1-product-review.md), [Stage 4 review](sprint-3-stage-4-review.md), [tech-arch](sprint-3-tech-arch.md), [UC-09](../use-cases/UC-09-logout-cascade.md), [UC-10](../use-cases/UC-10-admin-terminate.md).

This is the implementation-ready slice plan. After Stage 5 lock, Stage 6 implementation begins slice-by-slice with the convention from Sprint 2: **each slice ends with `tools/run-tests.sh` green AND a UC walkthrough on live IS** before merging onward.

---

## §1. Sprint shape — 7 slices over ~6 working days

| Slice | Day | Scope | Stage 4 BLOCKs/FIXes covered | D3.x mapped | R-LOGOUT mapped |
|---|---|---|---|---|---|
| **3A.0** spikes | Day 1 (½) | C10 + C13 capability probes; outcome documented as F-20/F-21. | BLOCK-A | — | — |
| **3A.1** orchestrator backbone | Day 1 (½) – Day 2 | `POST /auth/logout` rewrite: per-user lock + Session.terminating + cancel barrier + IS revoke + redirect_url JSON. SPA `performSignOut()` follows `redirect_url`. | G-1, G-2, G-9, G-10, BLOCK-F, BLOCK-G, FIX-9, FIX-12, NIT-5 | D3.1 (orchestrator side) | R-LOGOUT-1, R-LOGOUT-2 |
| **3A.2** internal RPC fan-out | Day 3 | `POST /internal/events` receivers on 4 services; shared `JtiDenylist` in `common/revocation/`; `RevocationState` injection; lifespan-wired sweepers; 4-receiver fan-out from orchestrator with inline retry-once. | G-3, G-4, G-5, G-8, BLOCK-B, BLOCK-I, FIX-1, FIX-2, FIX-3, FIX-4, FIX-7, FIX-19, FIX-20, FIX-22, NIT-1 | D3.1 (fan-out side) | R-LOGOUT-3, R-LOGOUT-4 |
| **3A.3** MCP server enforcement | Day 4 | Denylist + 60 s introspection cache (monotonic negative); `validate_token()` extended; introspection cache class. | G-6, G-7, FIX-10, FIX-13, FIX-18, NIT-6 | D3.1 (server side) | R-LOGOUT-5 |
| **3A.4** demo polish + manual verify | Day 5 (½) | UC-09 walkthrough end-to-end live; latency measurement (Day 4 deliverable per Q4); spinner phases in SPA; trace panel `DEMO_MODE` gate; copy-deck wiring. | NIT-7, NIT-8 | D3.1 (UC-09 fully) | R-LOGOUT-6, R-LOGOUT-8 |
| **3B.1** admin-terminate (D3.2) | Day 5 (½) – Day 6 | Orchestrator BCL receiver `POST /backchannel-logout`; full 9-check `logout_token` validation; `_seen_logout_jtis` bounded; IS Console BCL URL registration on `orchestrator-mcp-client`. | BLOCK-C, BLOCK-D, BLOCK-H, FIX-11 | D3.2 | R-LOGOUT-7, R-LOGOUT-7b |
| **3B.2** binding_message + carries | Day 6 | `binding_message` reason variants; SPA `?reason=admin_terminated` banner; UC-10 walkthrough; F-20 wiring (auth_req_id revoke if C10 PASS). | FIX-15, FIX-17 | D3.4 | — |
| **3B.3** R-LOGOUT suite + retro | Day 7 | All R-LOGOUT-1..8 + R-LOGOUT-7b authored as automated tests against mocked IS; CI smoke; sprint retro draft. | — | D3.3 | All |

**Total budget:** ~6 working days nominal, up to 9 days with slack per L-5 lock (extend sprint rather than cut R-LOGOUT scope). Each slice ends with `tools/run-tests.sh` green AND a manual walkthrough against live IS at `13.60.190.47:9443`.

---

## §2. Slice details

### 3A.0 — Capability spikes (½ day, Day 1 morning)

**Goal:** lock the Stage 4 BLOCK-A open question + close C10 (Q-LOGOUT-4 ghost approval).

**Tasks:**
- Author `idp_capability_test/c10_authreqid_revoke.py` — does IS support `auth_req_id` revoke before consent completes?
- Author `idp_capability_test/c13_introspection_capability.py` per [`docs/spikes/sprint-3-c13-introspection-capability.md`](../spikes/sprint-3-c13-introspection-capability.md).
- Run both probes against live IS as `employee_user`.
- Capture outcomes in `docs/architecture/sprint-1-fixes.md` as F-20 (C10) and F-21 (C13).

**Exit criteria:**
- F-20 PASS/FAIL recorded → drives 3B.2 `auth_req_id` revoke wiring decision.
- F-21 PASS/FAIL recorded → if FAIL, **patch tech-arch §5 rows 1, 2, 3 to SECURITY-DEGRADED label** and add operator-action note in `docs/demo-runbook.md` before continuing to 3A.1.

**Time-box:** 30 min per probe + 60 min documentation. Hard cap 3 hours; if longer, escalate.

**Risk:** if F-21 FAIL, the §5 backstop story is wrong and demo-runbook narration changes. Build still proceeds (denylist remains the primary boundary), but stakeholder narrative softens.

---

### 3A.1 — Orchestrator logout backbone (½ day, Day 1 afternoon → Day 2)

**Goal:** the SPA Sign Out button executes the locked five-step cascade (no fan-out yet) and lands on a clean IS-cleaned login page.

**Files touched:**
- [`orchestrator/auth/routes.py`](../../orchestrator/auth/routes.py) — `POST /auth/logout` body replaced; `X-Request-ID` enforced (FIX-9); `SameSite=Strict` cookie.
- [`orchestrator/auth/`](../../orchestrator/auth/) NEW `is_revoke.py` — wraps `/oauth2/revoke` call (FIX-1 split from `revocation.py`).
- [`orchestrator/auth/`](../../orchestrator/auth/) NEW `logout_handler.py` — orchestrates the cascade. Acquires per-user lock; sets `Session.terminating`; calls cancel barrier; calls `is_revoke`; (no fan-out yet — stub); clears cookie + Session; returns `{redirect_url}`.
- [`orchestrator/chat/session_store.py`](../../orchestrator/chat/) — add `Session.terminating: bool`; add `PendingCIBA.cancelled_ack: asyncio.Event`; new `_user_locks: dict[str, asyncio.Lock]`.
- [`orchestrator/chat/`](../../orchestrator/chat/) — chat / CIBA initiate paths check `Session.terminating` and 401 (BLOCK-G).
- [`client/app.js`](../../client/app.js) — `performSignOut()` follows `redirect_url`; phase-1 spinner (copy-deck 8.5); phase-2 spinner before redirect (copy-deck 8.9); 10 s timeout + error path (FIX-16, copy-deck 8.10).
- [`client/index.html`](../../client/index.html) — login page banner branch for `?reason=signed_out_partial` (copy-deck 1.13).

**Tests added:**
- Unit: logout handler ordering invariant (terminating-flag-first, cancel-before-fan-out-stub, Session-removal-last).
- Unit: `Session.terminating` rejects new chat/CIBA-initiate with 401.
- Unit: 10 s timeout handling on SPA fetch.
- N-test: R-LOGOUT-1 (cleared cookie → 401 on `/api/chat`), R-LOGOUT-2 (token-A introspect inactive within 2 s).

**Exit criteria:**
- `tools/run-tests.sh` green.
- Manual UC-09 walkthrough PARTIAL: sign in → click Sign Out → cookie cleared → IS consent screen renders → confirm → `/?reason=signed_out` shown. (Captured tokens still work — fan-out is stubbed.)

**Demo-visible outcome:** sign-out clears the user's session and IS session correctly. Captured tokens are NOT yet invalidated until 3A.2/3A.3.

---

### 3A.2 — Internal RPC fan-out (Day 3)

**Goal:** orchestrator fan-outs to all 4 receivers; receivers update denylists + drop cached tokens.

**Files touched:**
- NEW [`common/revocation/jti_denylist.py`](../../common/revocation/) — shared `JtiDenylist` class (FIX-3 single implementation).
- NEW [`common/revocation/__init__.py`](../../common/revocation/) — module exports.
- NEW [`orchestrator/agent_registry/revoke_client.py`](../../orchestrator/agent_registry/) — outbound RPC client (FIX-1 split from auth concerns).
- [`orchestrator/auth/logout_handler.py`](../../orchestrator/auth/) — wire fan-out call replacing the 3A.1 stub. Inline retry-once @ 200 ms per leg (FIX-22). `asyncio.gather(return_exceptions=True)`.
- [`hr-agent/main.py`](../../hr-agent/main.py), [`it-agent/main.py`](../../it-agent/main.py) — add `POST /internal/events` route + `RevocationState` lifespan wiring. Route validates `X-Internal-Auth` header (NIT-1).
- [`hr-agent/ciba/orchestrator.py`](../../hr-agent/ciba/), [`it-agent/ciba/orchestrator.py`](../../it-agent/ciba/) — add `_revocation: RevocationState`; add `revoke_jti(jti, user_sub, exp)` method; modify cached-token lookup to check `revoked_jtis` first.
- [`hr-server/main.py`](../../hr-server/main.py), [`it-server/main.py`](../../it-server/main.py) — add `POST /internal/events` route + `JtiDenylist` lifespan wiring. (No `_token_cache` to clear here.)
- [`docker-compose.yml`](../../docker-compose.yml) — `INTERNAL_REVOKE_SHARED_SECRET` env var per service. Add startup-time `expose` (not `ports`) for internal port. Document `UVICORN_WORKERS=1` invariant (BLOCK-I).
- Each service `main.py` — fail-fast assertion `assert int(os.getenv('UVICORN_WORKERS', '1')) == 1` (BLOCK-I).

**Tests added:**
- Unit: `JtiDenylist` add/contains/sweep semantics; hard cap eviction with WARN.
- Unit: `RevocationState` injection / FastAPI dependency wiring.
- Integration: orchestrator → 4 receivers fan-out; assert all 4 ack within budget.
- Integration: simulated 500 from one leg → retry once → succeed; second 500 → log WARN, proceed.
- N-test: R-LOGOUT-3 (HR-AGENT cache-bust ack), R-LOGOUT-4 (IT-AGENT ack).

**Exit criteria:**
- `tools/run-tests.sh` green; new `common/revocation/` module covered.
- Manual: Sign Out → trace panel shows `internal_event_sent` × 4 within 2 s. Cached token-B in HR-AGENT process is observably gone (`docker compose exec hr-agent python -c "..."` shows empty cache).

---

### 3A.3 — MCP server enforcement (Day 4)

**Goal:** captured token-B presented directly to `hr_server` returns 401 `ERR-MCP-002` after logout, even bypassing the agent.

**Files touched:**
- [`hr-server/auth/validators.py`](../../hr-server/auth/), [`it-server/auth/validators.py`](../../it-server/auth/) — extend `validate_token()` per tech-arch §4.2:
  - Step 7: denylist check (zero-latency security boundary).
  - Step 8: introspection cache + IS round-trip on miss.
  - FIX-10 / FIX-18: negative cache only on signed-JWS-valid response; permanent until exp; positive bounded by `min(20 s, exp)` per L-3 lock.
  - Network errors fail-open (signature already valid; denylist is the boundary).
- NEW `IntrospectionCache` class in same files.
- [`hr-server/`](../../hr-server/), [`it-server/`](../../it-server/) — IS introspection client (basic auth as the server's confidential client).
- Add `INTROSPECTION_POSITIVE_TTL` env var (default 20 per L-3 lock).
- Lifespan-wire the introspection sweeper alongside the denylist sweeper.

**Tests added:**
- Unit: cache_state × denylist_state matrix (per tech-arch §4.2 NIT-10) — 6 cases.
- Unit: negative cache permanence; positive cache TTL; network error fail-open.
- Integration: revoke flow end-to-end via mocked IS introspection.
- N-test: R-LOGOUT-5 (captured token-B → `hr_server` → 401 `ERR-MCP-002`).

**Exit criteria:**
- `tools/run-tests.sh` green.
- Manual: capture token-B before logout, sign out, paste token-B into `curl -H "Authorization: Bearer ..." http://hr-server:8000/...` → 401 `ERR-MCP-002`. (Latency measurement skipped per L-3 — TTL is fixed at 20 s.)

---

### 3A.4 — UC-09 demo polish (½ day, Day 5 morning)

**Goal:** the 90-second demo storyboard runs cleanly. UC-09 fully green.

**Files touched:**
- [`client/app.js`](../../client/app.js) / [`client/styles.css`](../../client/styles.css) — trace panel `DEMO_MODE` env gate (NIT-7); `aria-live` regions on spinner + login banner (NIT-8); polish.
- [`docs/demo-runbook.md`](../demo-runbook.md) — append UC-09 walkthrough section. Storyboard narration with Salesloft Drift framing (NIT-9). Operator-action note for SECURITY-DEGRADED row IF F-21 FAIL.
- [`tools/grep-trace.sh`](../../tools/grep-trace.sh) — verify it reconstructs the logout rid chain across orchestrator + 4 receivers.

**Tests added:**
- N-test: R-LOGOUT-6 (logout during active consent widget → `CIBATimeoutError(reason="cancelled")`).
- N-test: R-LOGOUT-8 (rid trace covers all hops).

**Exit criteria:**
- `tools/run-tests.sh` green.
- Manual: full UC-09 walkthrough in <90 s wall-clock. Trace panel cascade visible in DEMO_MODE; hidden in production mode.
- Sign-off: D3.1 acceptance criteria 1–6 from Stage 1 §7 all checkable in one run.

---

### 3B.1 — Admin-terminate (½ day, Day 5 afternoon → Day 6 morning)

**Goal:** D3.2 — IS Console terminate triggers the same cascade end-to-end.

**Files touched:**
- NEW [`orchestrator/auth/bcl_receiver.py`](../../orchestrator/auth/) — `POST /backchannel-logout` route. Full 9-check `logout_token` validation per tech-arch §3.3 / BLOCK-C. Reuses `common/auth/jwt_validator.py` (do NOT roll a new validator). `SeenLogoutTokens` class (FIX-3). `sub` resolution with `sid → sub` reverse-index fallback.
- [`orchestrator/auth/code_exchange.py`](../../orchestrator/auth/) (existing) — populate `sid → sub` reverse index from `id_token.sid` at code-exchange time.
- [`orchestrator/auth/logout_handler.py`](../../orchestrator/auth/) — extract the cascade logic into a callable invoked by both `POST /auth/logout` (UC-09) and `POST /backchannel-logout` (UC-10). Reason precedence rule (FIX-12) lives here.
- [`orchestrator/chat/sse.py`](../../orchestrator/chat/) — add SSE event type `session_terminated`. Add `flushed: asyncio.Event` (or equivalent ack mechanism) on the channel for BLOCK-H ordering.
- Token-bucket rate limiter middleware on `/backchannel-logout` (10 req/s per source) per BLOCK-D.
- IS Console action (manual): set `back_channel_logout_uri` on `orchestrator-mcp-client` app to `http://localhost:8123/backchannel-logout` (uses C12 reverse-SSH tunnel).

**Tests added:**
- Unit: `logout_token` validation — all 9 spec checks; negative tests for each missing/wrong claim.
- Unit: `_seen_logout_jtis` bound + sweep.
- Unit: SSE `session_terminated` emit-before-Session-drop ordering (BLOCK-H).
- Integration: forged BCL → 400 + cascade NOT triggered (R-LOGOUT-EX-3 negative test).
- N-test: R-LOGOUT-7 (half fan-out simulated 500 → IT-SERVER 401 within 60 s window).
- N-test: R-LOGOUT-7b (all-legs fail emits `SECURITY_DEGRADED` ERROR label).

**Manual verification:**
- Bring up C12 tunnel rig (`./scripts/spike-bcl-up.sh`).
- Sign in as `employee_user`.
- IS Console → User Management → Active Sessions → Terminate.
- Confirm orchestrator logs `bcl_received` within 5 s; trace panel on the still-open SPA shows `session_terminated`; SPA navigates to `/?reason=admin_terminated`.

**Exit criteria:**
- `tools/run-tests.sh` green.
- Forged-BCL negative test green (R-LOGOUT-EX-3).
- Live admin-terminate manual verification PASS.

---

### 3B.2 — binding_message + carries (½ day, Day 6 afternoon)

**Goal:** D3.4 + tie-up of carry items.

**Files touched:**
- [`common/auth/binding_messages.py`](../../common/auth/) — branch on `reason`: `user_signed_out` / `admin_terminated` / `token_expired` (FIX-17).
- [`client/index.html`](../../client/index.html) / [`client/app.js`](../../client/app.js) — login banner for `?reason=admin_terminated` (copy-deck 8.11).
- IF F-20 PASS (C10 capability test), wire `auth_req_id` revoke into the cancel path; otherwise document the "ghost approval" caveat in `demo-runbook.md`.

**Tests added:**
- Unit: `binding_messages` branch coverage for all 3 reasons.
- N-test: post-revoke re-CIBA produces visibly distinct binding_message vs. pre-revoke.

**Exit criteria:**
- `tools/run-tests.sh` green.
- Manual: re-login after admin-terminate → trigger CIBA → binding_message visibly says "your previous session was ended" (not "you signed out").
- D3.4 acceptance ticked.

---

### 3B.3 — R-LOGOUT suite + retro (Day 7)

**Goal:** D3.3 — full R-LOGOUT suite green in CI; retro draft ready.

**Tasks:**
- Author any R-LOGOUT-1..8 + R-LOGOUT-7b tests not yet covered by prior slices (~2–3 net-new).
- CI smoke: GitHub Actions or local CI pass.
- Draft `docs/architecture/sprint-3-retro.md` following Sprint 2's template.
- Update `docs/architecture/sprint-3-signoff.md` with all D3.x acceptance.

**Exit criteria:**
- `tools/run-tests.sh` green; full count up from Sprint 2's 822 by ~30–40 new tests.
- Retro doc covers what went well / what hurt / decisions confirmed (mirror Sprint 2 retro shape).

---

## §3. Day-by-day timeline

| Day | Morning | Afternoon |
|---|---|---|
| **1** (Mon) | 3A.0 spikes (C10 + C13) → F-20/F-21 | 3A.1 begin: routes.py + logout_handler skeleton |
| **2** (Tue) | 3A.1 finish: SPA wiring + tests + manual UC-09 partial | 3A.1 sign-off; 3A.2 begin: common/revocation + RPC clients |
| **3** (Wed) | 3A.2: receivers on 4 services, fan-out e2e | 3A.2 manual verification; sign-off |
| **4** (Thu) | 3A.3: MCP server denylist + introspection | 3A.3 latency measurement (Q4 lock); manual R-LOGOUT-5 |
| **5** (Fri) | 3A.4 demo polish; 90-second walkthrough | 3B.1 begin: BCL receiver + validation |
| **6** (Mon) | 3B.1 finish: live admin-terminate manual | 3B.2: binding_messages + C10 wiring (if PASS) |
| **7** (Tue) | 3B.3: R-LOGOUT suite + CI | Retro draft; sign-off |

(Days are working-day labels, not literal weekday — actual cadence depends on operator availability.)

---

## §4. Dependency graph

```
3A.0 (spikes) ──┐
                ├──→ 3A.1 (orch backbone) ──→ 3A.2 (fan-out) ──→ 3A.3 (MCP enforce) ──→ 3A.4 (polish)
                                                                                              │
                                                                                              ▼
                                                                                          3B.1 (admin-terminate)
                                                                                              │
                                                                                              ▼
                                                                                          3B.2 (binding_message)
                                                                                              │
                                                                                              ▼
                                                                                          3B.3 (R-LOGOUT + retro)
```

**Hard dependencies:**
- 3A.2 cannot start until 3A.1 has the cancel barrier + Session.terminating wired (the fan-out interacts with both).
- 3A.3 requires 3A.2 because the introspection-cache + denylist coexistence depends on the denylist primitive being deployed.
- 3B.1 depends on 3A.4 because the BCL receiver invokes the same `logout_handler.execute_cascade()` that 3A.1–3A.4 build.

**Soft dependencies (parallelizable if needed):**
- 3A.4 polish can start while 3A.3 manual verify is in progress.
- 3B.3 R-LOGOUT tests can be authored in parallel with 3B.1/3B.2 implementation if owner has bandwidth.

---

## §5. Spike day (3A.0) — focused checklist

| Task | Probe | Outcome | Affects |
|---|---|---|---|
| C10 — does IS support `auth_req_id` revoke? | `/oauth2/ciba/revoke` POST with `auth_req_id` from a pending CIBA | F-20 PASS / FAIL → drives 3B.2 wiring | Q-LOGOUT-4 ghost-approval mitigation |
| C13 — does IS introspection of OBO tokens reflect parent token-A revoke? | Per [`docs/spikes/sprint-3-c13-introspection-capability.md`](../spikes/sprint-3-c13-introspection-capability.md) | F-21 PASS / FAIL → drives §5 SECURITY-DEGRADED labels | tech-arch §5 row 1, 2, 3 + demo-runbook |

**Spike day fails forward.** If both probes FAIL, the design still ships — denylist is the primary security boundary; the failures just downgrade the backstop story (more SECURITY-DEGRADED rows in §5 + operator-action notes in runbook). No design change required.

---

## §6. Risk per slice

| Slice | Top risk | Mitigation |
|---|---|---|
| 3A.0 | C13 takes longer than 30 min if introspection requires unexpected setup | Hard cap 3 hours; if longer, escalate; both PASS-by-default outcomes are designed in already (just relabel rows) |
| 3A.1 | Concurrency invariants (BLOCK-F/G) hard to test | Author the ordering-invariant unit tests FIRST; they fail until the invariants are right |
| 3A.2 | 4-receiver fan-out fails on Docker network resolution | Day-1 smoke: `docker compose exec orchestrator curl http://hr-agent:8001/healthz` proves connectivity before writing fan-out logic |
| 3A.3 | Negative-cache permanence subtleties (FIX-18) hard to debug | Cover all 6 cells of the cache×denylist matrix in unit tests |
| 3A.4 | UC-09 walkthrough surfaces a bug that wasn't caught by tests (Sprint 2 had 13 such bugs) | Allocate 4 hours buffer; treat as expected, not exceptional |
| 3B.1 | C12 tunnel must be running for live admin-terminate | Add `make demo-up-with-bcl` target; document in runbook |
| 3B.2 | If F-20 FAIL, ghost-approval window remains (1 IS auth_req_id TTL = 300 s) | Already accepted as known limitation; documented in UC-09 EX-1 sub-case |
| 3B.3 | CI environment doesn't have the C12 tunnel | R-LOGOUT-7/7b uses mocked IS; CI smoke is mocked-only |

---

## §7. What ships at sprint close

When all 7 slices close, the demo runs:

1. **UC-09 demo (Sprint 3 Act II — 90 seconds):** sign in → trigger cached HR token → click Sign Out → cascade observable in trace panel + 1.5 s end-to-end → IS consent → land on signed-out page → operator pastes captured token-B → 401 `ERR-MCP-002`.
2. **UC-10 demo (Sprint 3 Act III — 60 seconds, defense narrative):** admin terminates session in IS Console → user's still-open SPA shows `session_terminated` → navigates to login banner.
3. **D3.4 demo (30 seconds, optional):** re-login after admin-terminate → trigger CIBA → binding_message visibly says "your previous session was ended."

**M3 sign-off criteria** (from milestone-plan §6 + Stage 1 §7):
- D3.1, D3.2, D3.4 acceptance demonstrated live.
- D3.3: `tools/run-tests.sh` green including R-LOGOUT-1..8 + R-LOGOUT-7b.
- F-20, F-21 captured in `sprint-1-fixes.md`.
- `sprint-3-retro.md` + `sprint-3-signoff.md` written.

---

## §8. Decisions locked (Stage 5 close — 2026-05-09)

| # | Question | Lock |
|---|---|---|
| L-1 | Spike day shape | **Serial.** C10 first, then C13. Single operator. |
| L-2 | If F-21 FAIL | **Ship with SECURITY-DEGRADED labels.** Demo path is sound; labels are honest about edge cases. |
| L-3 | Introspection cache TTL | **20 s flat** (user lock — middle ground between Stage 1's 60 s default and Stage 4's conditional 10 s). **Skip the Day-4 measurement.** This supersedes Stage 1 Q4. Tech-arch §4.2 `_INTROSPECTION_POSITIVE_TTL = 20.0`. |
| L-4 | 3B.3 retro attendance | **User only** (default; not asked). |
| L-5 | Time-box overrun policy | **Extend sprint by up to 2 days** (user override of "cut scope" recommendation). All R-LOGOUT tests stay in scope; timeline slips before scope cuts. Sprint window: 7 working days nominal, up to 9 with slack. |

Stage 5 closed. Stage 6 (implementation) starts with 3A.0 spike day on the next operator session.
