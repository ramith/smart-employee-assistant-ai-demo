# Sprint 3 Stage 4 — Multi-agent design review

**Date:** 2026-05-09
**Reviewers (parallel, independent):** architect-reviewer, security-engineer, ux-researcher, ai-engineer.
**Inputs reviewed:** [Stage 1](sprint-3-stage-1-product-review.md), [UC-09](../use-cases/UC-09-logout-cascade.md), [UC-10](../use-cases/UC-10-admin-terminate.md), [tech-arch sketch](sprint-3-tech-arch.md), [F-19](sprint-1-fixes.md), [brainstorm](../spikes/sprint-3-logout-design-brainstorm.md), live code state on `sprint-3-build`.
**Convention:** `BLOCK` (Stage 5 cannot lock), `FIX` (must patch before Stage 6 implementation), `NIT` (deferable polish).

This document consolidates all findings, deduplicates cross-reviewer overlap, lays out the patch plan, and surfaces the decisions the product owner must make before Stage 5.

---

## §0. Summary

| Reviewer | BLOCK | FIX | NIT |
|---|---|---|---|
| architect | 1 | 5 | 3 |
| security | 3 | 7 | 3 |
| ux | 2 | 5 | 3 |
| ai-engineer | 4 | 5 | 3 |
| **Total raw** | **10** | **22** | **12** |
| After dedup (below) | **8** | **17** | **9** |

The strongest signal is in **concurrency / ordering** (4 of 8 BLOCKs) and **security hardening of `/internal/revoke`** (2 of 8 BLOCKs).

The reviewers agree on the design's overall coherence: Option A is the right shape, the orchestrator-as-gateway pattern is structurally sound, and the F-19 finding justifies the choice. The findings are all about *making the implementation hold under realistic stress*, not about reshaping the design.

---

## §1. BLOCK — must resolve before Stage 5 lock

### BLOCK-A. Capability gap: does IS introspection return `active=false` for CIBA-issued OBO tokens after token-A revoke?
**Raised by:** architect (BLOCK-7), security (FIX-4 — escalated to BLOCK due to load-bearing nature).
**Risk:** the entire backstop story for EX-2 (half fan-out) and EX-4 (orchestrator crash mid-flow) depends on the assumption that revoking token-A at IS makes child OBO tokens (token-B/C) introspect as inactive. F-19 specifically established that IS does NOT treat CIBA tokens as session-bound. If introspection of OBO tokens is *also* unaffected by token-A revoke, then the denylist becomes the only line of defense for D3.2 and the captured-token-replay window in EX-3/EX-4 is **the full token TTL (1 h)**, not 60 s.
**Mitigation:** add a **C13 capability spike** to 3A Day 1, parallel with C14 (`auth_req_id` revoke). Probe: complete a CIBA flow, capture token-B + jti, revoke token-A via `/oauth2/revoke`, then introspect token-B. PASS = `active=false`; FAIL = `active=true`. Document as F-21 in `sprint-1-fixes.md`. If FAIL, the §5 error matrix in tech-arch must be rewritten to remove the introspection backstop claim, and the demo runbook must surface the 1-hour replay window as a **SECURITY-DEGRADED** state.

### BLOCK-B. `/internal/revoke` shared-secret design: no rotation, no scope, single point of cross-tenant revocation.
**Raised by:** security (BLOCK-1).
**Risk:** secret in env var visible to any compromised container, all `docker inspect` users, build-tooling. Compromise = arbitrary cross-user denylist pollution + DoS-by-revocation + cancel of pending CIBAs. Receivers cannot tell orchestrator from a compromised peer.
**Mitigation (must do before Stage 5):**
1. **Bind receiver socket to docker-internal network only** — uvicorn `--host` on a non-routable internal interface, OR a unix-socket bind-mount per receiver. Defense in depth with the shared secret.
2. **Rotate per `docker compose up`** — entrypoint script generates a fresh random secret and injects only into the orchestrator + 4 receivers. Never commit; never log.
3. **Per-call HMAC** over `(jti, user_sub, reason, timestamp)` with 30 s freshness window — leaked log lines containing the bearer header cannot be replayed.
4. **Document production upgrade** — OAuth client_credentials with scope `revoke:jti` on a dedicated internal IS app, OR mTLS via compose-issued CA. POC accepts shared secret; the production roadmap must call this out explicitly.

### BLOCK-C. BCL `logout_token` validation missing required spec checks.
**Raised by:** security (BLOCK-2).
**Risk:** without `typ: logout+jwt` header check, **any** RS256-signed JWT issued by IS for `aud=orchestrator-mcp-client` (id_token, access_token, even leaked id_token from a different login) can be replayed as a logout_token to forcibly log users out. Without `alg` allow-list, the classic `alg: none` / `alg: HS256-with-RSA-key` forgery is wide open. Without `sid` resolution, IS configurations that send `sid` only will null-deref.
**Mitigation:** patch tech-arch §4.3 to specify all six validations:
1. JWS sig via JWKS (with `alg` allow-list reusing `common/auth/jwt_validator.py`).
2. `typ` header MUST be `logout+jwt`.
3. `iss` exact-match.
4. `aud = orchestrator-mcp-client`.
5. `iat ≤ now ≤ iat + 300s`.
6. `events` claim present.
7. `nonce` MUST be absent.
8. At least one of `sub` or `sid`; if `sid` only, resolve via reverse index `sid → sub` populated at code-exchange time from `id_token.sid`.
9. `jti`-replay protection (already drafted; tighten per FIX-3 below).

### BLOCK-D. C12 reverse-SSH tunnel binds `127.0.0.1:8123` on AWS VM with no authentication on the BCL endpoint.
**Raised by:** security (BLOCK-3).
**Risk:** any process on the AWS VM (other tenants, compromised IS, cron jobs, SSH users) can `curl -X POST http://127.0.0.1:8123/backchannel-logout` with a crafted form. The OIDC signature check on `logout_token` is the only gate — anyone with read access to the IS keystore (default JKS password `wso2carbon` if not hardened) can sign valid tokens for any sub. Even without keystore access, garbage POSTs cause CPU-burn DoS via JWKS verification.
**Mitigation (lighter-weight option recommended for POC):**
- **Per-IP rate limit** on the BCL receiver (token bucket, e.g. 10 req/s) to prevent CPU-burn DoS — minimal change.
- **Document the trust boundary** — `13.60.190.47` shell access list = trusted population. If unknown, escalate; for the demo this is the user's own VM.
- *Optional / heavier:* bind tunnel to a unix socket on the AWS VM with `chmod 0600` so only the IS process user can write. Tighter but requires IS-side config change to POST to a unix socket (verify support).

### BLOCK-E. UX EX-5 copy is misleading; user has no path to fully sign out after cancelling at IS consent.
**Raised by:** ux (BLOCK-1, BLOCK-2 — combined).
**Risk:** "To complete sign-out at the identity provider, click Confirm on the next page" — there is no next page; the user is back on the SPA login screen. Misreads as "broken software" or "I need to sign in again to fix this." On shared/kiosk machines, the next user can SSO into any other federated SP as the previous user. This is an audit-defensibility regression — UC-09's whole demo wedge is "one click kills every agent's access," but it doesn't kill IS SSO if the user fat-fingers Cancel.
**Mitigation:** replace EX-5 with a new flow:
- Route to `?reason=signed_out_partial` (new login-page banner).
- Banner copy: *"You have been signed out of this application. Note: your sign-in at the identity provider may still be active. To fully sign out everywhere, visit your organization's sign-out page or close your browser."*
- Add as new copy-deck row 1.13.
- **Add a second-phase spinner** before the IS redirect (during the ≤200 ms before `window.location.href`): *"You will be redirected to complete sign-out at your identity provider."* Mirrors the established Scenario A pattern (copy-deck row 1.4). Add as copy-deck row 8.9.

### BLOCK-F. UC-09 main flow ordering wrong: `cancel_event.set()` must precede fan-out.
**Raised by:** ai-engineer (BLOCK-1).
**Risk:** the poll task is concurrent with the logout handler. Today's draft order — clear session → revoke at IS → fan-out → cancel — leaves a window where a still-running poll task can complete a CIBA flow and issue an MCP call before the denylist propagates. The token's jti isn't in the orchestrator session map yet (the poll just minted it), so even the fan-out cannot ban it.
**Mitigation:** rewrite UC-09 main flow + tech-arch §1.1 to: **clear session → set Session.terminating flag → cancel_event.set() + await cancelled_ack barrier (≤100 ms) → revoke at IS → fan-out**. Add `cancelled_ack: asyncio.Event` to `PendingCIBA`; poll loop sets it in `finally`. The barrier ensures any in-flight CIBA completion observes the cancel before its jti exists.

### BLOCK-G. Orchestrator-internal race: in-flight handlers can start CIBA after session-clear but before fan-out.
**Raised by:** ai-engineer (BLOCK-2).
**Risk:** request authenticated via the cookie that was already past auth middleware can hold a captured Session reference; a new tool-call mid-handler could initiate CIBA on the orphaned session, racing the fan-out.
**Mitigation:** add `Session.terminating: bool = False` flag. Set it as the **first action** of the logout handler (before any data is captured). Chat / CIBA-initiate paths check the flag and reject with 401 once set. This makes the snapshot in subsequent steps authoritative.

### BLOCK-H. SSE `session_terminated` message can be lost on admin-terminate due to channel teardown ordering.
**Raised by:** ai-engineer (BLOCK-12), architect (OQ-6 verdict).
**Risk:** UC-10 step 6 emits the SSE event then immediately removes the Session. SSE channel is tied to Session; teardown happens in the same coroutine on the next line. Kernel may not flush before the channel closes — client receives an unceremonious disconnect, falls back to reconnect logic, never sees the structured `session_terminated` event. UC-10 banner copy never fires.
**Mitigation:** reorder UC-10 step 6 + tech-arch §1.2 to: **emit SSE event → await flush + small drain (50–100 ms) OR await `channel.flushed` event → remove Session**. Architect's lighter-weight version: `await asyncio.sleep(0)` to yield once. AI-engineer's version: explicit ack from SSE writer. Pick one; document the ordering as an invariant in code review.

### BLOCK-I. uvicorn `--workers > 1` silently breaks the in-process denylist.
**Raised by:** ai-engineer (BLOCK-10).
**Risk:** `set[str]` per-process means worker A's denylist doesn't see worker B's revoke. A future operator scaling the service breaks correctness silently — no error, just stale tokens succeed. Exactly the kind of production migration trap the demo narrative exists to prevent.
**Mitigation:** add §4.4 "Process model assumptions" to tech-arch with explicit fail-fast: `assert int(os.getenv('UVICORN_WORKERS', '1')) == 1` at each receiver's startup. Single-worker is a Q5 invariant; make it observable.

---

## §2. FIX — must patch before Stage 6 implementation

| ID | Source | Summary | Suggested change |
|---|---|---|---|
| FIX-1 | architect | `revocation.py` mixes auth + RPC + lifecycle; split. | `auth/is_revoke.py` (calls IS) + `agent_registry/revoke_client.py` (fan-out RPC) + reuse existing `_cancel_pending_ciba_for_session()`. |
| FIX-2 | architect | Module-level globals couple lifecycle to import order; break testability. | Wrap denylist + introspection cache in a `RevocationState` class injected via FastAPI dependency. |
| FIX-3 | architect | `_seen_logout_jtis` unbounded forever; sweepers duplicated 4×. | Move to `common/revocation/jti_denylist.py`. TTL-bounded `dict[jti, iat]`; sweep alongside denylist. Hard cap 10k + WARN. |
| FIX-4 | architect | `_jti_to_cache_key` + `_denylist` + `_introspection_cache` — three shapes; rename for intent. | Agents: `_token_cache_by_jti` + `_revoked_jtis`. Servers: `_revoked_jtis` + `_introspection_results`. Same `_revoked_jtis` prefix exposes the cross-process invariant. |
| FIX-5 | architect | Q6's 4-receiver fan-out hides asymmetry: agent denylist defends cache-reuse; server denylist defends token replay. | Document the asymmetry in tech-arch §2 + UC-09 step 5. |
| FIX-6 | architect | "All fan-out legs fail" row in §5 says "tokens expire naturally" — soft language for a 1-hour replay window. | Rename outcome to **SECURITY-DEGRADED**; add R-LOGOUT-7b for all-legs failure (assert ERROR log emitted with known label). |
| FIX-7 | architect | `/internal/revoke` route name not CAEP-friendly; rename now to ease migration. | Use `/internal/events` with body `{type: "session-revoked", subject: {sub, jti}, reason}`. Pre-aligned with CAEP `subject` shape. |
| FIX-8 | security | IS `/oauth2/revoke` client secret storage/rotation/blast-radius unspecified. | Add §4.4 to tech-arch: `ORCHESTRATOR_IS_CLIENT_SECRET` env-only; rotation cadence; pre-flight curl smoke test on 3A Day 1. Verify IS-side that this app cannot revoke other clients' tokens. |
| FIX-9 | security | `POST /auth/logout` lacks CSRF protection. | Require `X-Request-ID` header (already sent by SPA per `client/app.js`); reject without. Belt-and-braces with `SameSite=Strict` (was Lax). |
| FIX-10 | security | Captured-token replay window wider than 60s on three real edge cases (MCP restart wipes cache, etc.). | Strengthen tech-arch §4.2: "denylist is fast-path; introspection is the security boundary." Negative cache only on signed-JWS-valid IS response (never on network errors). |
| FIX-11 | security | Logout_token validation does not pin JWKS endpoint or specify rotation handling. | Tech-arch §4.3: state JWKS URI = `https://13.60.190.47:9443/oauth2/jwks`; reuse `jwt_validator._jwks_cache`; HTTPS verification posture documented; `kid`-not-in-cache → refetch once before failing. |
| FIX-12 | security | Concurrent UC-09 + UC-10 race; user banner says "you signed out" while audit says "admin terminated." | Per-`user_sub` `asyncio.Lock` around revocation cascade. First wins; second sees Session removed and short-circuits but emits its reason for audit. Reason precedence: `admin_terminated` > `user_signed_out`. |
| FIX-13 | security | Denylist + introspection-cache memory bounds unmodelled across all 5 processes. | Supervisor wrapping sweep loop; hard-cap 10k + FIFO eviction + WARN; rate-limit `/internal/events` per source (100 req/min); validate jti shape (UUID/ULID) before adding. |
| FIX-14 | ux | "Signing out…" passive; rename to action-grounded copy. | Phase 1 (during POST): *"Revoking access for all agents…"* Phase 2 (before redirect): *"Redirecting to complete sign-out…"* Update copy-deck row 8.5; add row 8.9. |
| FIX-15 | ux | UC-10 banner "ended by an administrator" implies personal action; punitive for routine cleanup. | Replace with: *"Your session has ended. Sign in again to continue."* Amber styling stays. Cause goes in admin audit log, not user banner. |
| FIX-16 | ux | No error path when orchestrator unreachable at sign-out click. | 10-sec timeout on `POST /auth/logout`; on timeout/5xx, banner: *"Sign-out could not be completed right now. Close your browser to end your session, or try again."* Add copy-deck row 8.10. Clear cookie client-side regardless. |
| FIX-17 | ux | binding_message "since you signed out" wrong for admin-terminate re-login. | Branch on `reason`: `user_signed_out` → "you signed out at HH:MM"; `admin_terminated` → "your previous session was ended"; `token_expired` → "your previous access expired". |
| FIX-18 | ai-engineer | Negative introspection cache should be permanent (or pinned to `exp`), not 60s TTL. | Encode `(active, fetched_at, exp)`; sweeper drops on `now > exp`. Stated invariant in §4.2: "denylist takes precedence; negative-cache monotonic; positive-cache bounded by min(TTL, exp)." |
| FIX-19 | ai-engineer | `revoke_jti` for unknown jti: spec ambiguous; sweeper assumes `jwt_exp(jti)` decodable from jti alone (it isn't). | Revoke body must carry `exp` so receivers don't need to derive. Sweeper: `for jti, exp in items if exp < now: drop`. |
| FIX-20 | ai-engineer | Multi-session per user: fan-out body sends ONE jti, but agents may have multiple cached tokens for `user_sub`. | Tech-arch §3.2 add `jtis: list[str]` field, OR explicitly state: orchestrator loops "for each session, fan-out 4 receivers." Update §1.2 sequence to show the loop. |
| FIX-21 | ai-engineer | Sweeper task lifecycle not specified; risks leaking across pytest event loops. | Tech-arch §2 add per-service row: wire sweeper via FastAPI `lifespan` context manager. Verify existing JWKS-refresh task follows lifespan; if not, fix that first. |
| FIX-22 | ai-engineer | Retry-once semantics ambiguous for `asyncio.gather` fan-out. | Specify: **inline retry within each fan-out coroutine**; gather with `return_exceptions=True`; partition only for logging, not retry scheduling. |

---

## §3. NIT — deferable polish

| ID | Source | Summary |
|---|---|---|
| NIT-1 | architect | `Authorization: Bearer` for shared secret conflates with OAuth bearer routing. Use `X-Internal-Auth: <secret>`. |
| NIT-2 | architect | Tunnel deployment leaks into demo runbook; factor behind `IS_BCL_PUBLIC_URL` env var for production swap-in. |
| NIT-3 | architect | OQ-6 fix one-liner: "`Session` removal must be the LAST statement in cascade; add a code-review assertion." |
| NIT-4 | security | `/internal/events` (renamed per FIX-7) propagates `X-Request-ID` on every accept/reject including 401 — completes audit chain. |
| NIT-5 | security | RP-initiated logout `state` parameter must be validated on return, else decorative. Store + verify per-session. |
| NIT-6 | security | Negative introspection cache: cap at 1h (matches token TTL) so transient IS hiccups are recoverable without restart. |
| NIT-7 | ux | Trace panel during sign-out should be `DEMO_MODE=true` scoped — production users should not see it. |
| NIT-8 | ux | Accessibility: add `aria-live=polite` for sign-out spinner; `aria-live=assertive` for login-page banner on arrival. New copy-deck rows 10.14/10.15. |
| NIT-9 | ux | UC-09 storyboard narration "F-19 spike" too technical for stakeholder audience; lead with Salesloft Drift framing. |
| NIT-10 | ai-engineer | Add (cache_state × denylist_state) correctness invariant comment to §4.2. |
| NIT-11 | ai-engineer | Reframe OQ-4: 60s positive cache is a *propagation backstop*, not a load shedder. |
| NIT-12 | ai-engineer | CAEP "morally equivalent" is hand-wavy; show the SET shape in §3.2 alternate sub-section so the migration isn't underestimated. |

---

## §4. Patch plan

**Patches I propose to apply autonomously (unambiguous, low-controversy):**

- All FIX items except FIX-9 (CSRF mechanism choice — see §5 below) and FIX-7 (route rename — see §5).
- All NIT items.
- BLOCK-C, BLOCK-F, BLOCK-G, BLOCK-H, BLOCK-I — concrete code/spec changes.
- BLOCK-E — UX copy is a clear improvement; apply.
- Add C13 capability test to spike list (BLOCK-A mitigation) — schedule for 3A Day 1 alongside C14.

**Patches that need your call before I apply:**

| # | Decision | Recommendation |
|---|---|---|
| D-1 | **BLOCK-B** — `/internal/revoke` hardening level. Three options: (a) shared secret only + document upgrade path (POC tier); (b) shared secret + per-call HMAC + rotate-per-up (recommended); (c) full mTLS (production-grade). | **(b) recommended.** Per-call HMAC is ~30 lines; rotation script is trivial. (c) is for Sprint 4+. |
| D-2 | **BLOCK-D** — tunnel hardening level. Two options: (a) per-IP rate limit only + document trust boundary (lightest); (b) bind tunnel to unix socket on AWS VM + IS-side config change. | **(a) recommended for POC.** You own the VM; trust boundary is yourself. (b) is fastidious for the demo. |
| D-3 | **FIX-7** route rename `/internal/revoke` → `/internal/events`. CAEP-friendly but adds change surface. | **Apply.** Saves a future migration; trivial today. |
| D-4 | **FIX-9** CSRF mechanism. Two options: (a) require `X-Request-ID` header server-side + `SameSite=Strict` (lighter); (b) full CSRF token round-trip. | **(a) recommended.** SPA already sends the header; no SPA work. |
| D-5 | **C13 capability test** — adds ~30 min to 3A Day 1. PASS/FAIL determines whether the introspection backstop story holds for half-fan-out + orchestrator-crash scenarios. | **Run it.** The Stage 1 doc already locked C14 for Day 1; C13 is the same shape and sits next to it. |

---

## §5. Files affected by patches (overview)

- `docs/architecture/sprint-3-tech-arch.md` — major: §1.1, §1.2 sequencing; §2 module split (FIX-1, FIX-21); §3.2 route rename (FIX-7) + body shape (FIX-19, FIX-20); §3.3 BCL validation completeness (BLOCK-C, FIX-11); §4.1, §4.2, §4.3 data structures (FIX-2, FIX-3, FIX-4, FIX-18); §4.4 NEW process-model assumptions (BLOCK-I) + IS client secret storage (FIX-8); §5 error matrix (FIX-6, FIX-22, FIX-13); §6 OQs resolved/escalated.
- `docs/use-cases/UC-09-logout-cascade.md` — main flow ordering (BLOCK-F, BLOCK-G); EX-5 copy (BLOCK-E); spinner copy (FIX-14); error path (FIX-16); demo storyboard (NIT-9); design notes UX (NIT-7, NIT-8).
- `docs/use-cases/UC-10-admin-terminate.md` — main flow step 6 ordering (BLOCK-H); banner copy (FIX-15); EX-2 narration (FIX-7 from ux); concurrency lock note (FIX-12).
- `docs/use-cases/copy-deck.md` — rows 1.13 (NEW partial sign-out banner), 8.5 (rewrite), 8.9 (NEW pre-redirect spinner), 8.10 (NEW error path), 10.14/15 (NEW a11y rows).
- `docs/architecture/sprint-3-stage-1-product-review.md` — §3 risk table addendum (R-1, R-2 backstop nuance per BLOCK-A); §6 acceptance criteria (R-LOGOUT-7b added per FIX-6); §8/§9 add C13 to Day-1 spike list.
- `docs/spikes/sprint-3-c13-introspection-capability.md` — NEW operator runbook for the OBO-introspection probe (BLOCK-A mitigation).

---

## §6. What's not changing

- The **locked design (Option A)** remains correct; F-19 stands.
- Q1–Q6 from Stage 1 are **not invalidated**. Q3 (IS consent screen) is reinforced by BLOCK-E's UX patches.
- The **3A/3B slice shape** is unchanged. C13 just slots in alongside C14 on 3A Day 1.
- `sprint-3-build` branch tracking is unchanged.

---

## §7. Decision summary table

| ID | Severity | Decision needed | Recommendation |
|---|---|---|---|
| BLOCK-A | BLOCK | C13 capability test on 3A Day 1? | Yes |
| BLOCK-B | BLOCK | `/internal/events` hardening level (D-1) | (b) per-call HMAC + rotate-per-up |
| BLOCK-C | BLOCK | Apply BCL validation completeness fix? | Yes — apply |
| BLOCK-D | BLOCK | Tunnel hardening level (D-2) | (a) rate limit + document trust boundary |
| BLOCK-E | BLOCK | Apply UX EX-5 + pre-redirect spinner fix? | Yes — apply |
| BLOCK-F..I | BLOCK | Apply concurrency/ordering fixes? | Yes — apply |
| FIX-7 | FIX | Route rename `/internal/revoke` → `/internal/events` (D-3) | Yes — apply |
| FIX-9 | FIX | CSRF mechanism (D-4) | (a) require X-Request-ID + SameSite=Strict |
| All other FIX/NIT | FIX/NIT | Apply autonomously | Yes |

Decision required from product owner: **D-1, D-2, D-3, D-4, BLOCK-A schedule (C13 on Day 1)**. The remaining ~25 patches are unambiguous and ready to apply once the four above are confirmed.
