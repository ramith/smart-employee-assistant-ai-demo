# Sprint 3 — Mid-sprint multi-agent review

**Date:** 2026-05-09 (evening, post-3A.2).
**Branch state at review:** `sprint-3-build` @ `926cab4`. 3A.0/3A.1/3A.2 shipped; tests 843/46 green per harness.
**Reviewers (parallel, independent):** architect-reviewer (voltagent-qa-sec), python-pro (voltagent-lang), security-engineer, product-manager (voltagent-biz), business-analyst (voltagent-biz).
**Convention:** `BLOCK` (gates 3A.3), `FIX` (must patch before sprint close), `NIT` (deferable polish).

This is the AS-BUILT review (not a design re-review — Stage 4 covered that). Reviewers checked whether the Stage 4 design patches actually landed, plus anything new that surfaced during 3A.1 + 3A.2 implementation.

---

## §0. Summary table

| Reviewer | BLOCK | FIX | NIT |
|---|---|---|---|
| architect | 2 | 3 | 3 |
| python-pro | 3 | 4 | 4 |
| security | 2 | 3 | 5 |
| ux/PM | — | — | — *(scope/timeline only; see §3)* |
| BA | — | — | 5 *(gap analysis; see §4)* |
| **Dedup'd** | **6** | **8** | **9** |

The design itself stands. Findings are about implementation drift from the locked design — Stage 4 patches that didn't fully land in the code.

---

## §1. BLOCK — must resolve before 3A.3 starts

### BLOCK-A. `cancelled_ack` Event is never set; BLOCK-F barrier is non-functional

**Raised by:** architect-reviewer (BLOCK-1), python-pro (BLOCK-1).

**Issue:** [`orchestrator/auth/session_store.py`](../../orchestrator/auth/session_store.py) defines `PendingCIBA.cancelled_ack: asyncio.Event`. [`orchestrator/auth/logout_handler.py`](../../orchestrator/auth/logout_handler.py) `_cancel_pending_ciba` waits on it for 100 ms. **Nowhere does anything set it.** The Event lives on the orchestrator's `Session.pending_ciba`; the actual CIBA poll loop runs in the orchestrator's `chat/routes.py::_run_serial_fan_out`. The poll loop's `finally` block needs to call `pending.cancelled_ack.set()`.

The barrier currently always times out, logs `cancel_barrier_timeout`, and proceeds. The race that BLOCK-F was meant to close (poll completing with a fresh token-B between `cancel_event.set()` and fan-out) is wide open.

**Fix:** wire `cancelled_ack.set()` into the orchestrator's CIBA poll path (`_run_serial_fan_out` Phase 2 await_completion `try/finally`).

### BLOCK-B. `find_sessions_for_user` is dead code; multi-session-per-user fan-out broken

**Raised by:** architect-reviewer (BLOCK-2), security-engineer.

**Issue:** [`session_store.py:343`](../../orchestrator/auth/session_store.py) defines `find_sessions_for_user(user_sub) → list[Session]` (FIX-20 wiring). **It has zero callers.** The cascade in `logout_handler.py::execute` operates on the single Session resolved from the cookie. Multi-browser case: signing out from tab-A leaves tab-B's cached agent tokens alive.

Also a 3B.1 prerequisite — admin-terminate via BCL receives `sub=…` and must hit ALL of that user's orchestrator sessions.

**Fix:** rewrite `LogoutHandler._execute_locked` to iterate `session_store.find_sessions_for_user(user_sub)` after acquiring the user lock, dedupe `token_a` for IS revoke, fan-out per-(session, completed_jti).

### BLOCK-C. `_user_locks` race window: two coroutines can both create a Lock

**Raised by:** python-pro (BLOCK-2).

**Issue:** [`session_store.py:337-341`](../../orchestrator/auth/session_store.py) `get_user_lock`:

```python
lock = self._user_locks.get(user_sub)
if lock is None:
    lock = asyncio.Lock()
    self._user_locks[user_sub] = lock
return lock
```

Two coroutines racing on the same `user_sub` both read `None` from `.get()`, both construct their own Lock, the second overwrites the dict, the first holds an orphan Lock. They both proceed in parallel — FIX-12 broken.

**Fix:** `lock = self._user_locks.setdefault(user_sub, asyncio.Lock())`. `setdefault` is a single atomic dict op (CPython GIL). One-line fix.

### BLOCK-D. `/internal/events` exposed on host network, not just docker-internal

**Raised by:** security-engineer.

**Issue:** [`docker-compose.yml`](../../docker-compose.yml) all four receivers use `ports: - "8001:8001"` etc. — that publishes the full HTTP surface (including `/internal/events`) on host `0.0.0.0`. Stage 4 BLOCK-D + tech-arch §4.4 said receivers bind docker-internal only.

**Threat:** LAN attacker → forge `session-revoked` events → DoS arbitrary user sessions if they guess/leak the static shared secret. Combined with no rate limit (BLOCK-E), this is offline-capable.

**Fix:** change receiver host bindings from `"8001:8001"` to `"127.0.0.1:8001:8001"` (and same for 8000/8002/8004). Orchestrator stays exposed (browser needs it). Documented in build-state doc.

### BLOCK-E. No rate limit on `/internal/events`, despite Stage 4 BLOCK-D promising one

**Raised by:** security-engineer.

**Issue:** [`common/revocation/internal_events.py`](../../common/revocation/internal_events.py) has no rate-limit middleware. Stage 4 BLOCK-D ("trust boundary + rate limit") and tech-arch §3.2 both claim 100 req/min per source. Grep returns zero matches across the tree.

**Fix:** add per-source IP token-bucket on the receiver. Simple in-process implementation suffices for POC: `dict[ip, deque[float]]` with sliding window.

### BLOCK-F. `attach_revocation` only wired to router dispatcher, not lifespan dispatcher (hr_agent)

**Raised by:** python-pro (BLOCK-3).

**Issue:** [`hr_agent/main.py:243`](../../hr_agent/main.py) — `revocation = RevocationState()` is attached to `_dispatcher_for_router` but the lifespan creates a SECOND `dispatcher` (line 155, also stored as `app.state.dispatcher`) which never gets the revocation state. Both dispatchers share `pending` dict but have separate `_token_cache` and `_jti_to_cache_key`. If any future code path uses `app.state.dispatcher` (debug routes, observability), the denylist check is silently a no-op.

**Fix:** call `attach_revocation(revocation)` on BOTH dispatchers, OR consolidate into a single dispatcher (Sprint-1 carry-over wart; out of scope to fully fix here).

---

## §2. FIX — must patch before sprint close

| ID | Source | Summary | Suggested change |
|---|---|---|---|
| FIX-1 | architect (FIX-3) | `Session.terminating` check missing on `/api/ciba/cancel` and SSE-disconnect cancel | Add `if session.terminating: raise HTTPException(401)` at top of `post_ciba_cancel`; document SSE-disconnect path runs after Session removal so terminating is moot |
| FIX-2 | architect (FIX-5) | `getattr(cfg, "internal_revoke_shared_secret", "")` legacy fallback duplicated 4× silently disables receiver on env-var typo | Replace with `cfg.internal_revoke_shared_secret`; emit WARN on empty so operator sees the disable |
| FIX-3 | architect (NIT-4) | `_logout_state_nonces` issued but `_validate_logout_state` is dead code | Either complete the round-trip or remove issuance; for POC the simplest is removal + a comment |
| FIX-4 | python-pro (FIX-3) | `# type: ignore[var-annotated]` on `_revocation = None` in dispatchers | Annotate `self._revocation: RevocationState \| None = None`; drop ignore |
| FIX-5 | python-pro (FIX-2) | `fan_out` `for attempt in (1, 2)` has unreachable trailing return | Refactor to two explicit tries; cleaner to read |
| FIX-6 | security | Shared-secret comparison uses `!=` not constant-time | `hmac.compare_digest(x_internal_auth or "", shared_secret)` |
| FIX-7 | python-pro (NIT-4) | `_spa_base_url` duplicated between `routes.py` and `logout_handler.py` | Move to `orchestrator/auth/_util.py` or method on `OrchestratorConfig` |
| FIX-8 | BA (E-2) | Test docstring drift: `test_routes.py` test 8 says "SameSite=Lax" but asserts Strict | Fix docstring to match assertion |

---

## §3. PM verdict (scope/timeline)

**Demo-readiness: AMBER.** UC-09 wedge ("captured token-B → 401") still requires 3A.3. Live-walk today is valuable for QA (cookie/SPA/spinners) but does not close the wedge.

**Timeline: ahead.** 3A.0+3A.1+3A.2 done in effectively one operator-day vs nominal three. L-5 buffer (up to 9 days) intact.

**Sharpened narrative:**
- F-19 corrected (BCL fires WITH `id_token_hint`) is a *win* — strengthens the demo, doesn't require pivot.
- F-21 confirmed at source (no parent→child linkage in IS schema) gives the gateway-pattern argument **source-code receipts**: *"WSO2 IS confirms in source — token revocation is row-local, no parent→child linkage. The orchestrator IS the policy enforcement point because no OAuth provider ships cross-grant revocation."*
- Add a one-slide source-code receipt slide to demo deck (F-21 GitHub link).
- Demo step 3 becomes: live `tools/grep-trace.sh <logout-rid>` shows all 4 receivers acked. Audit chain visible.

**Recommendation:** continue 3A.3 immediately after BLOCK patches; live-walk after 3A.4 when wedge is fully demonstrable. (Operator override → live-walk now to validate ops/connectivity.)

---

## §4. BA verdict (gap closure)

**G-1..G-5, G-8, G-9, G-10:** CLOSED.
**G-6, G-7:** PARTIAL — receiver mounted + denylist populated, but validators don't yet consult. Owned by 3A.3.

**Pre-flight (Stage 1 §5):** zero of the 5 console items confirmed done. All required before live-walk. (Checklist surfaced below.)

**R-LOGOUT testability:**
- R-LOGOUT-7b: ✓ TESTABLE-NOW (`test_revoke_client.py::test_security_degraded_log_emitted_on_all_legs_failure`).
- R-LOGOUT-1, R-LOGOUT-3, R-LOGOUT-4, R-LOGOUT-5, R-LOGOUT-7: NEEDS-3A.3.
- R-LOGOUT-2: NEEDS-3B.* (live IS).
- R-LOGOUT-6: TESTABLE-NOW (primitives in place; missing the named assertion).
- R-LOGOUT-8: NEEDS-3A.4.

**New gaps surfaced in implementation (BA §E):**
- **E-1: silently-optional `INTERNAL_REVOKE_SHARED_SECRET`.** If operator forgets to set, all five services start with fan-out disabled and no startup warning. Folded into FIX-2.
- **E-3: harness masks failures.** `tools/run-tests.sh` greps "passed" in summary line; mixed-result files report as passed. 5 known-red tests carry over silently. Add to retro / Sprint 4 backlog.
- **E-4: dead state-nonce.** Folded into FIX-3.
- **E-5: server sweep loops always start.** When `INTERNAL_REVOKE_SHARED_SECRET` is empty, hr_server/it_server still start the denylist sweeper but the receiver is not mounted; benign but wasteful. Fold into FIX-2's WARN.

---

## §5. Patch plan

**Apply autonomously (this commit):** all 6 BLOCKs + FIX-1, FIX-2, FIX-4, FIX-6, FIX-8.

**Defer:** FIX-3 (dead state-nonce removal — easy but spans 3 files; keep paired with the "rebuild auth/_util.py" cleanup in Sprint 4); FIX-5 (cosmetic), FIX-7 (refactor with import-cycle implication).

**Live-walk readiness checklist** (BA §F applied verbatim, surfaced in build-state doc).

---

## §6. Decisions for record

- F-19 corrected at source means **D3.2 admin-terminate (3B.1) gets BCL fan-out as designed** — this had been ambiguous post-spike. 3B.1 BCL receiver work proceeds with confidence.
- **L-3 lock (introspection TTL = 20 s flat) stands.** Skip Day-4 measurement gate; ship as locked.
- **Live-walk timing: continue 3A.3 first, walk after 3A.4.** PM recommendation accepted.
