# Sprint 3 — Stage 1: Product Team Review

**Live status (2026-05-09 evening):** 3A.0 spikes + 3A.1 backbone + 3A.2 fan-out shipped on `sprint-3-build` @ `3067074`. Test counts 843/46. Next slice: 3A.3 MCP server enforcement. See [`sprint-3-stage-5-slice-plan.md`](sprint-3-stage-5-slice-plan.md) for live slice status. Decisions below remain locked.

**Date:** 2026-05-09
**Reviewers:** PM (voltagent-biz:product-manager), BA (voltagent-biz:business-analyst)
**Inputs:** [`docs/milestone-plan.md`](../milestone-plan.md) §3 Sprint 3 (D3.1–D3.4), [`docs/spikes/sprint-3-logout-design-brainstorm.md`](../spikes/sprint-3-logout-design-brainstorm.md) §-1 verdict + §6 Q-LOGOUT-1..9 + §9 industry context, [`docs/architecture/sprint-1-fixes.md`](sprint-1-fixes.md) §F-19, [`docs/architecture/sprint-2-retro.md`](sprint-2-retro.md), live code state on branch `sprint-1-build` @ `c4a0b9b`.

**Design status entering Stage 1:** locked to **Option A** (orchestrator-driven cache-bust). The C12 BCL capability spike (2026-05-09) empirically established that WSO2 IS 7.2.0 does NOT register CIBA-issued tokens as user sessions and does NOT fire OIDC Back-Channel Logout to agent applications when those tokens are revoked. Triple-confirmed (empty Active Sessions, listener captured 0 POSTs, IS audit log records `ServiceProviderName: "null"`). Full evidence in [§F-19](sprint-1-fixes.md). Sprint 3 implementation begins from a closed design question, not an open one.

---

## §1. Demo arc (PM)

**The single most compelling Sprint 3 demo:** *"One click kills every agent's access — in under two seconds."*

Three-minute story:
1. Employee logs in, asks for a two-part task: "Get my leave balance AND list available laptops."
2. Consent widget for HR Agent appears → Approve → token-B issued, cached in HR-AGENT, leave balance returned.
3. Before IT consent arrives, **employee clicks Sign Out** in the SPA.
4. Within ~1.5 seconds, observable in the in-app trace panel and in `docker compose logs` — **before any user interaction**:
   - Orchestrator clears `orch_sid`, calls IS `/oauth2/revoke` for token-A.
   - Orchestrator fan-outs `POST /internal/revoke?jti=…` to HR-AGENT, IT-AGENT, hr_server, it_server (4 receivers per Q6).
   - All four drop their `_CachedToken` / cache entry and add the jti to in-process denylists.
   - Pending IT CIBA poll receives `cancel_event.set()` and exits cleanly.
5. Browser is then redirected (302) to IS `/oidc/logout?id_token_hint=…&post_logout_redirect_uri=…`. IS renders its **"Yes, sign me out"** consent page (Q3 lock — spec-pure RP-initiated logout). User confirms → IS clears its session → 302 back to `/?reason=signed_out`.
6. Stakeholder asks "what if I'd already captured a token before sign-out?" — paste a captured token-B as `Authorization: Bearer …` directly to `hr_server` → 401 `ERR-MCP-002` (denylist hit, before introspection cache TTL).
7. Audit chain: `tools/grep-trace.sh <logout-rid>` reconstructs the entire cascade across orchestrator + both agents + both servers.

**Industry wedge for stakeholders:** this is the **Salesloft Drift** breach (Aug 2025, 700+ companies, 10 days uncontrolled). Our orchestrator-as-gateway pattern is the same shape ScaleKit, Okta, and AWS Bedrock AgentCore independently converged on. F-19 is the empirical receipt — we proved the gateway pattern is *required* (not just preferred) on our specific WSO2 IS 7.2 + CIBA stack. **Production-roadmap nod:** the internal cache-bust RPC is morally equivalent to a CAEP `session-revoked` event over Shared Signals Framework; one slide in the demo deck suffices.

---

## §2. Slice recommendation (PM)

`docs/milestone-plan.md` §3 Sprint 3 specifies ~7 working days for D3.1–D3.4. F-19 simplifies scope (no agent-side BCL receivers); the trade-off is that introspection + denylist primitives are now first-time additions in the MCP servers. Net effort comparable.

**Sprint 3A (demo-critical, ~3.5 days):**
- D3.1 — Orchestrator `POST /api/logout` handler executing the locked five-step flow (G-1, G-2, G-3, G-8, G-10).
- D3.1 — Internal `/internal/revoke?jti=…` receiver on HR-AGENT and IT-AGENT (G-4).
- D3.1 — Per-agent jti denylist primitive + `_CachedToken` invalidation by jti (G-5).
- D3.1 — MCP server hybrid enforcement: in-process denylist + 60 s introspection cache, hard-fail on `active=false` (G-6, G-7).
- D3.1 — UC-09 end-to-end walkthrough manually verified.

**Sprint 3B (defense-in-depth + R-tests + carries, ~2.5 days):**
- D3.2 — Admin-terminate path. Orchestrator registers as a BCL receiver (orchestrator app supports BCL; agents do not per F-19); receives `logout_token`, validates it (full 9-check spec coverage per Stage 4 BLOCK-C), executes the same fan-out as 3A.
- D3.4 — `binding_message` distinct on re-issued tokens after partial revoke. Branches on `reason` per Stage 4 FIX-17 (one extra branch in `binding_messages.py`).
- C14 capability test — does IS support `auth_req_id` revocation? Run Day 1 of 3B; if PASS, wire revoke into the cancel path; if FAIL, document the "ghost approval" caveat in the runbook.
- R-LOGOUT-1..R-LOGOUT-8 (per §6 below) authored, passing in CI; **R-LOGOUT-7b added** per Stage 4 FIX-6 (assert SECURITY-DEGRADED ERROR label fires on all-legs fan-out failure).
- Carry from Sprint 2 retro: branch rename done as Q5 lock; persistent session store (Redis) — defer per Q5; demo-runbook update for cache-bust friction.

**Already done:** S1.11 (orchestrator session-map skeleton, recording `(session_id, agent_id, jti, exp, auth_req_id)` on every CIBA completion). The revocation logic is the new layer.

**Stage 4 capability spikes added (3A Day 1, parallel):**
- **C14** — `auth_req_id` revocation support at IS (gates Q-LOGOUT-4 ghost-approval mitigation).
- **C13** (NEW per BLOCK-A) — does IS introspect CIBA-issued OBO tokens as inactive after parent token revoke? Determines whether §5 error-matrix backstop story holds. Operator runbook: [`docs/spikes/sprint-3-c13-introspection-capability.md`](../spikes/sprint-3-c13-introspection-capability.md). PASS = ship as-is; FAIL = SECURITY-DEGRADED label on half-fan-out + crash rows. Outcomes documented as F-20 (C14) and F-21 (C13) in `docs/architecture/sprint-1-fixes.md`.

---

## §3. Top risks (PM)

| # | Risk | Mitigation |
|---|---|---|
| R-1 | **Orchestrator down at logout time.** If orchestrator crashes between session lookup and RPC fan-out, the jti denylist never reaches the agents and token-B/token-C remain valid until natural TTL (1 h default). | Tokens expire on their own (document the constraint); MCP introspection is the backstop — IS's own revocation of token-A propagates via introspect-on-next-call within ≤60 s; production roadmap: persist denylist in Redis. R-LOGOUT-7 codifies the degraded path. |
| R-2 | **"Ghost approval" — IS issues a token after logout (Q-LOGOUT-4).** User logs out at T=0; orchestrator fan-out completes by T=1.5 s. The CIBA polling loop in HR-AGENT was already cancelled, but if the user separately approves at IS at T=1 s (out-of-band), IS still completes the grant and writes a "successful CIBA" audit row after logout. The token has no consumer (poll loop is dead) but the audit reads weird. | Day 1 of 3B = C14 capability probe (does IS support `auth_req_id` revoke?). If PASS, call it on logout. If FAIL, document the caveat in the use-case runbook and accept it. R-LOGOUT-6 covers the cancel-event path regardless. |
| R-3 | **Introspection-cache TTL makes the cascade look slow on demo day.** With a 60 s positive cache, an MCP server that introspected token-B 30 s before logout will see `active=true` for another 30 s, masking the revocation. The denylist short-circuits this — but only if cache-bust delivered first. | Hybrid is the answer (Q-LOGOUT-6): denylist check **before** introspection lookup. R-LOGOUT-5 asserts denylist hit returns 401 within 1 s. Day 4 of 3A: measure end-to-end latency manually; if >2 s, drop introspection cache TTL to 10 s for demo-day. |
| R-6 (Stage 4) | **Introspection backstop for OBO tokens after parent revoke is unverified (BLOCK-A).** ~~Hypothesis to test on 3A Day 1.~~ **Resolved 2026-05-09 — F-21 confirmed empirically AND at source** (see [`sprint-1-fixes.md`](sprint-1-fixes.md) §F-21 + [`sprint-3-is-source-analysis.md`](../spikes/sprint-3-is-source-analysis.md) §3). `revokeAccessTokens(String[])` is single-row UPDATE; schema has no parent/actor/request_id linkage; `act` claim is JWT-body only. Architectural, not a bug. | Per Stage 5 L-2: ship with **SECURITY-DEGRADED labels** for user-driven `/oauth2/revoke` paths. Denylist on the 4 receivers is the **only** revocation primitive for OBO tokens — the gateway pattern is not just preferred, it's *required*, with source-code receipts. **D3.2 admin-terminate is unaffected** — F-19 corrected at source (BCL fan-out works with `id_token_hint`, which the locked Q3 design uses). |
| R-4 | **Topology ambiguity: are MCP server and agent the same process or separate?** G-7 — `hr_server` (port 8000) and `hr_agent` (port 8001) run as separate containers today. The denylist must live in the process that validates the token (server), but the fan-out arrives at the agent. Bridging them needs either a second internal endpoint on the server or a process collapse. | Stage 4 (tech arch) decision. Recommended path: keep the topology, add `POST /internal/revoke` on each MCP server as well — orchestrator fans out to both `agent` AND `server` per user. Doubles the fan-out count but keeps separation of concerns. |
| R-5 | **SPA navigation race vs IS consent screen (Q3).** Today `client/app.js` `performSignOut()` POSTs `/auth/logout` via `fetch()` then hard-navigates to `/?reason=signed_out` regardless of response. With Q3 locked to IS consent screen, orchestrator must return a JSON `{redirect_url: "https://13.60.190.47:9443/oidc/logout?id_token_hint=…&post_logout_redirect_uri=…"}`; SPA replaces `window.location.href` with that URL. The SPA's hard-coded post-logout navigation bypasses both IS cleanup and the consent screen — must be removed. | G-10 fix: orchestrator returns JSON containing the IS logout URL; SPA does `window.location.href = response.redirect_url`. After IS confirms, the user lands on `/?reason=signed_out` via IS's `post_logout_redirect_uri`. Stage 6 task; UC-09 wireframe drives the timing. |

---

## §4. BA gaps (concrete blockers, code+config)

Verified against branch `sprint-1-build` @ `c4a0b9b` on 2026-05-09. Each row has a real file location and is not satisfied by existing code.

| # | Gap | Owner | Stage |
|---|---|---|---|
| G-1 | Existing [`orchestrator/auth/routes.py`](../../orchestrator/auth/routes.py) `POST /auth/logout` handler does cookie-clear + `session_store.delete()` only. No `/oauth2/revoke` call, no fan-out, no `cancel_event` set, no IS RP-initiated redirect. The locked D3.1 sign-out flow has zero implementation here. | python-pro | Stage 6 |
| G-2 | No orchestrator endpoint executing the locked five-step flow. Either extend `/auth/logout` or add `/api/logout`. SPA's [`client/app.js`](../../client/app.js) `performSignOut()` already POSTs to `/auth/logout` with `credentials: "include"` — endpoint path is fine; the handler body is the gap. | python-pro | Stage 6 |
| G-3 | No outbound RPC caller in the orchestrator for `/internal/revoke` fan-out. [`orchestrator/main.py`](../../orchestrator/main.py) wires `A2AClient` instances for HR-AGENT and IT-AGENT, but `A2AClient` has no `revoke()` method. Add an HTTP call (or a method on `A2AClient`). | python-pro | Stage 6 |
| G-4 | Neither [`hr-agent/main.py`](../../hr-agent/main.py) nor [`it-agent/main.py`](../../it-agent/main.py) exposes a `/internal/revoke` endpoint. Authentication method for this endpoint is open (shared secret? mTLS? loopback-trust?) — must be decided at Stage 4. | python-pro | Stage 4 → Stage 6 |
| G-5 | Per-agent `_CachedToken` (commit `c040ff9`, Sprint 2.1b) is keyed by `(user_sub, ciba_scope)`. There is no `jti`-keyed index. The `/internal/revoke?jti=…` handler cannot locate the matching cache entry without a full scan or a secondary index. The denylist `set[jti]` (S3.3) is also absent — no primitive exists yet in either agent process. | python-pro | Stage 6 |
| G-6 | MCP servers [`hr-server/auth/validators.py`](../../hr-server/auth/validators.py) and [`it-server/auth/validators.py`](../../it-server/auth/validators.py) implement the F-04 6-step JWT validation but contain no `/oauth2/introspect` call, no positive cache, no `active=false` hard-fail, no denylist check. Sprint 3 is the first time introspection enters either MCP server's codebase. | python-pro | Stage 6 |
| G-7 | No in-process jti denylist primitive in `hr_server` or `it_server`. Topology: MCP server (port 8000) and agent (port 8001) are separate processes. Under Q5 single-process per service, the canonical answer is in-process denylist on the *server* (where token validation happens), populated by either a second internal endpoint or a process collapse. **Stage 4 decision needed.** | python-pro | Stage 4 |
| G-8 | [`orchestrator/main.py`](../../orchestrator/main.py) `_cancel_pending_ciba_for_session()` (the SSE-disconnect hook from Sprint 2.2) must also be called from the new logout path. The `cancel_event.set()` primitive (`PendingCIBA.cancel_event` from `session_store.py`) already exists — what is missing is the second call-site. | python-pro | Stage 6 |
| G-9 | `id_token` capture is **already in place** — `OAuthToken.id_token` is non-optional in [`common/auth/models.py`](../../common/auth/models.py) and `Session.token_a` is stored at code-exchange time. So `session.token_a.id_token` is available for `id_token_hint`. **Not a gap** — flagged here so the dev does not re-derive. (Pre-flight #4 below verifies live behaviour.) | — | — |
| G-10 | SPA `performSignOut()` POSTs `/auth/logout` via `fetch()` then hard-navigates to `/?reason=signed_out` regardless of server response. With Q3 = IS consent screen, the orchestrator response must carry `{redirect_url: <IS /oidc/logout URL with id_token_hint + post_logout_redirect_uri>}`; SPA must `window.location.href = redirect_url` and drop the hard-coded navigation. Today's behaviour bypasses both IS cleanup and the consent screen entirely. | python-pro + front-end | Stage 6 |

---

## §5. WSO2 IS pre-flight checklist (BA)

Console actions that must be verified before Sprint 3 manual verification can run. None require code; each is a configuration confirmation.

1. **Front-Channel Logout URL on orchestrator SPA app (`BO4LfSkkUOWnl7YgJNZcGiABW5ka`).** Sprint 2 sign-off captured this field as set. Re-open the app in IS Console → Inbound Authentication → OIDC → Logout URLs and confirm. This field is the trigger for D3.2's admin-terminate cleanup signal to the orchestrator.
2. **BCL fields on agent apps are inert per F-19 — leave them, do not rely on them.** They were configured during the C12 spike. They cause no harm in place; if IS behaviour changes in a patch they'd start working. Sprint 3 design assumes they fire zero times.
3. **`/oauth2/revoke` reachable from the orchestrator container.** Endpoint at `https://13.60.190.47:9443/oauth2/revoke` was exercised by M0 capability tests. Sprint 3 promotes it to production code. Smoke-test before writing the handler: from the orchestrator container, `curl -k -X POST https://13.60.190.47:9443/oauth2/revoke -u <client_id>:<secret> -d "token=<any_access_token>"` should return 200. If 5xx or unreachable, stop — D3.1 cannot pass without it.
4. **`id_token` captured for the orchestrator's confidential code-exchange.** Code review (G-9) confirms the field is captured. Live-trace verification: tail `docker compose logs orchestrator` during a fresh login and confirm `token_a.id_token` is non-null. If IS does not return `id_token` for the `orchestrator-mcp-client` flow, RP-initiated `id_token_hint` is `None` and the IS `/oidc/logout` redirect may silently fail — that becomes a code task.
5. **Both test accounts smoke-pass UC-09.** `hr_admin_user` and `employee_user` should each complete a fresh login + at least one CIBA approval as the first action of Day 1 — catches expired passwords, MFA lockouts, missing group membership before the test surface gets confusing.

---

## §6. R-tests scope (BA — R1–R16 inventory + recommended replacement)

Per [`docs/milestone-plan.md`](../milestone-plan.md) §5, the R-tests entry is "R1–R13 — Sprint 2 → moved to Sprint 3 (revocation tests)" plus explicit R14, R15, R16.

**R1–R13 (v3 plan rollup):** Inherited placeholder from the pre-CIBA-pivot RFC 8693 plan, deferred wholesale when Sprint 3 scope was set. They have never been broken into named, one-acceptance-criterion-per-test cases. With the design now locked to Option A (orchestrator-driven cache-bust), the original v3 token-exchange revocation patterns no longer apply. **Recommendation:** formally deprecate the R1–R13 rollup; replace with a smaller, design-bound set R-LOGOUT-1..R-LOGOUT-8 below. Carrying thirteen unnamed tests forward would fail D3.3 ("R-tests pass") by ambiguity alone.

**R14 (pending CIBA at logout):** real, well-scoped — sign out while consent widget visible; `cancel_event` fires; subsequent IS-issued token is rejected at denylist or never lands. Mapped to R-LOGOUT-6.

**R15 (half fan-out):** orchestrator successfully revokes for HR-AGENT but receives 5xx from IT-AGENT's `/internal/revoke`. Recommended behaviour: retry-once with 200 ms back-off, then log `WARN` with jti and proceed. Introspection cache (60 s) backstops on next call. Mapped to R-LOGOUT-7.

**R16 (audit chain):** post-logout `grep-trace.sh <rid>` returns log lines from all hops without missing any. Mapped to R-LOGOUT-8.

**Recommended replacement set — R-LOGOUT-1..R-LOGOUT-8:**

| ID | Name | Acceptance criterion (one line) |
|---|---|---|
| R-LOGOUT-1 | SPA sign-out clears orchestrator session | After `POST /auth/logout`, `GET /api/chat` with the cleared cookie returns 401 within 1 s. |
| R-LOGOUT-2 | Token-A revoked at IS | `/oauth2/introspect` on the captured token-A returns `active=false` within 2 s of logout. |
| R-LOGOUT-3 | HR-AGENT cache-bust acknowledged | `hr_agent` logs `internal_revoke_received jti=…` within 2 s; subsequent MCP call with token-B returns 401 `ERR-MCP-002`. |
| R-LOGOUT-4 | IT-AGENT cache-bust acknowledged | Same as R-LOGOUT-3 for `it_agent` / token-C. |
| R-LOGOUT-5 | MCP denylist blocks revoked token-B | Direct `Bearer <captured-token-B>` to `hr_server` tool endpoint returns 401 with `error_id: ERR-MCP-002` (denylist) before introspection TTL expires. |
| R-LOGOUT-6 | Pending CIBA cancelled on logout (covers R14) | Logout during active consent widget → `CIBATimeoutError(reason="cancelled")` in `hr_agent` logs; no `ResultPayload` written to state. |
| R-LOGOUT-7 | Half fan-out degrades gracefully (covers R15) | Simulated 500 from IT-AGENT `/internal/events`; IT-SERVER returns 401 on next tool call within introspection window (≤60 s, IF F-21 PASS); `WARN` with jti present in orchestrator log. |
| R-LOGOUT-7b | All fan-out legs fail emits SECURITY-DEGRADED label (Stage 4 FIX-6) | Simulated 500 from all 4 receivers; orchestrator emits `ERROR logout_fanout_total_failure SECURITY_DEGRADED`. Test asserts the literal string `SECURITY_DEGRADED` is present in logs (operator grep target). |
| R-LOGOUT-8 | Audit chain reconstructs via rid (covers R16) | `tools/grep-trace.sh <logout-rid>` returns log lines from orchestrator + hr_agent + it_agent; zero missing hops. |

---

## §7. Acceptance criteria for Sprint 3 sign-off

A stakeholder verifies in ~90 seconds using the demo runbook, IS Console, and `docker compose logs`.

1. **D3.1 — SPA sign-out cascade ≤2 s.** `orch_sid` deleted; orchestrator logs `auth_logout`, `token_a_revoked`, `internal_revoke_sent` for both agents, `cancel_event_set` for any pending CIBA. (R-LOGOUT-1 through R-LOGOUT-4)
2. **D3.2 — Admin-terminate cascade ≤5 s.** IS Console → User Management → active session → Terminate; orchestrator's BCL receiver logs `bcl_received` and runs the same fan-out. (R-LOGOUT-1..4 with admin-terminate trigger)
3. **D3.1/D3.3 — Captured token-B → 401 at hr_server.** Token-B sniffed pre-logout, presented post-logout as `Authorization: Bearer …` directly to `hr_server`, returns 401 `ERR-MCP-002` (denylist) before 60 s introspection TTL, or `active=false` (introspection) after. (R-LOGOUT-5)
4. **D3.1 — Pending CIBA terminated cleanly.** Race condition: user approves at IS *after* logout has begun. The OBO token is rejected at denylist OR never lands a successful MCP result because `cancel_event` killed the poll. SPA shows the sign-in page either way. (R-LOGOUT-6)
5. **D3.1 — Post-logout cookie reuse → login redirect.** Navigating to `/` with the old `orch_sid` (or stale `localStorage` `orch_session_id`) lands on `/?reason=signed_out`, not chat. (R-LOGOUT-1)
6. **D3.3 — Audit chain via rid reconstructs cascade.** `tools/grep-trace.sh <logout-rid>` returns timestamped lines from orchestrator + hr_agent + it_agent covering: logout received, IS revoke called, fan-out dispatched, agents' denylist updated. (R-LOGOUT-8)
7. **D3.3 — `tools/run-tests.sh` green including R-LOGOUT-1..8.** Mocked-IS layer; full run under 3 min; R-LOGOUT-7 uses simulated 500 from IT-AGENT.
8. **D3.4 — `binding_message` distinct on post-revoke re-issue.** After a partial revoke + re-CIBA, the binding message visibly differs from the pre-revoke message (e.g., includes "since you signed out earlier" or a fresh correlation-id segment). Manually verified.

---

## §8. Decisions needed before Stage 2 (UX)

| # | Decision | Default | Recommendation |
|---|---|---|---|
| Q1 | Adopt 3A/3B slice split, or attempt full D3.1–D3.4 in one sprint? | full | **Adopt 3A/3B.** Mirror Sprint 2's cadence. 3A delivers the demo wedge; 3B carries quality gates + admin-terminate. |
| Q2 | Re-run C12 (BCL spike) for stakeholder verification, or lock the verdict and ship? | lock | **Lock.** F-19 is triple-confirmed (Active Sessions empty, listener captured zero POSTs, audit-log `ServiceProviderName: null`). C14 (auth_req_id revoke) is a *different* probe and is Day 1 of 3B. |
| Q3 | Silent sign-out (cookie-clear + RPC fan-out + redirect) or IS consent screen? | silent | **IS consent screen** (locked by user 2026-05-09, overrides recommendation). Spec-compliant RP-initiated path; user explicitly accepts the extra click for production-defensible audit narrative. UX must surface the redirect cleanly — see UC-09 in Stage 2. |
| Q4 | Introspection-cache TTL: 60 s as brainstormed, or tune tighter? | 60 s | **Start 60 s; measure on Day 4 of 3A.** If end-to-end propagation looks ≤2 s, lock 60 s. If the cache makes the cascade observably slow (>5 s on demo run), drop to 10 s. R-3 mitigation. (Q-LOGOUT-6.) |
| Q5 | Branch — keep `sprint-1-build` or start `sprint-3-build`? | keep | **New `sprint-3-build` branch — non-destructive** (locked by user 2026-05-09). Branch off from current `sprint-1-build` HEAD (`c4a0b9b` + this Stage 1 commit). `sprint-1-build` preserved locally + on origin as audit history. No rebase, no force-push, no branch deletion. |
| Q6 | MCP server jti denylist receiver — second `/internal/revoke` on each server, or process collapse? | second endpoint | **Second endpoint per server.** Keeps existing topology and separation of concerns. Doubles fan-out call count (orchestrator → 4 receivers instead of 2) — still ≤200 ms total. (Resolves G-7.) |

---

## §9. Stage hand-off

**Decisions locked (2026-05-09):**

- **Q1:** Adopt 3A/3B split. 3A is demo-critical (~3.5 days). 3B carries admin-terminate + R-LOGOUT-1..8 + C14 probe (~2.5 days). *(default accepted)*
- **Q2:** F-19 verdict locked; no re-spike. C14 probe = Day 1 of 3B. *(default accepted)*
- **Q3:** **IS consent screen** for sign-out (RP-initiated logout to `/oidc/logout?id_token_hint=…` rendering IS's confirmation page). User explicitly chose spec-pure path over single-click silent flow. Implication: orchestrator's `/api/logout` performs revoke + fan-out + cancel, then returns 302 to IS `/oidc/logout`; IS shows "Yes, sign me out"; on confirm, IS redirects to `post_logout_redirect_uri` = `/?reason=signed_out`. Drives the UC-09 wireframe in Stage 2.
- **Q4:** Introspection cache TTL = ~~60 s, measured on Day 4 of 3A~~ → **superseded by Stage 5 L-3: 20 s flat, no Day-4 measurement**. User locked at 20 s as a middle ground between Stage 1 default (60 s) and Stage 4 conditional drop-to-10 s.
- **Q5:** **New `sprint-3-build` branch — non-destructive.** Branch off from `sprint-1-build` HEAD after the Stage 1 commit lands. `sprint-1-build` is preserved on local + origin as Sprint 2 audit history. No rebase. No force-push. No branch deletion.
- **Q6:** MCP server gets its own `/internal/revoke` — orchestrator fans out to 4 receivers (HR-AGENT, IT-AGENT, hr_server, it_server). Resolves G-7 topology ambiguity in favour of preserving existing two-process-per-domain shape.

**Out of scope for Sprint 3:**
- Persistent session/denylist store (Redis). Q5 single-process accepted; carry to Sprint 4+.
- Full CAEP wire format on the cache-bust RPC. Use simple JSON `{"jti": "...", "user_sub": "...", "reason": "user_signed_out"}`. CAEP migration is the production roadmap nod.
- Agent-side BCL receivers. F-19 — IS will not call them.

**Final slice plan (proposed; subject to user lock at Stage 1 close):**

| Slice | Scope | Why first | Demo-visible outcome |
|---|---|---|---|
| **3A.1** | Foundation: G-1/G-2 orchestrator `/api/logout` shell + token-A revoke + IS redirect path; G-9/G-10 SPA navigation fix | Backbone for 3A.2/3A.3; no agent fan-out yet | Sign Out clears orchestrator session and lands on IS-cleaned `/?reason=signed_out` |
| **3A.2** | Internal RPC: G-3 caller + G-4 receivers on agents and (per Q6) servers; G-5 jti denylist primitive + cache-bust | Unblocks demo wedge | Fan-out logs visible on `docker compose logs`; cached tokens dropped |
| **3A.3** | MCP enforcement: G-6/G-7 introspection + denylist hybrid in hr_server, it_server | Closes the loop on captured-token defense | Captured token-B → 401 `ERR-MCP-002` |
| **3A.4** | UC-09 use case + demo runbook walkthrough; G-8 logout calls `_cancel_pending_ciba_for_session()` | End-to-end story | 90-second stakeholder demo runs clean |
| **3B.1** | C14 capability probe + (if pass) `auth_req_id` revoke wiring | Closes Q-LOGOUT-4 ghost-approval risk | "Ghost approval" caveat documented or eliminated |
| **3B.2** | D3.2 — orchestrator BCL receiver for admin-terminate; UC-10 use case | D3.2 acceptance | Console terminate triggers same cascade |
| **3B.3** | R-LOGOUT-1..8 written + green; D3.4 binding_message branch | Quality gate | `tools/run-tests.sh` green; binding_message visibly distinct on re-issue |

Stage 1 closes once Q1–Q6 are locked. Proceeding to Stage 2 (UX) for **UC-09 (logout cascade)** and **UC-10 (admin-terminate)** drafting.
