# Sprint 3 — build state (in-progress)

**Last updated:** 2026-05-10 — **Sprint 3 SIGNED OFF.** D3.1 / D3.2 / D3.3 / D3.4 all green (per `sprint-3-signoff.md`). Retro at `sprint-3-retro.md`. 898 / 49 tests green strict-mode.
**Branch:** `sprint-3-build` (final commit pending).
**Parked for Sprint 4 (expert chat):** WSO2 IS consent-screen UX (per-scope consent toggle on MCP Client template + CIBA binding_message visibility on IS-side) + introspection consultation. See `project_is_consent_ux_deferred.md` + `project_introspection_deferred.md`.
**Stack:** all 5 services healthy with `denylist_enforcement=on` startup line on both MCP servers. Cascade fan-out clean (4/4 receivers acked). Captured token-B replay → 401 ERR-MCP-002 with `reason=denylist_hit`. **3A.3 ships denylist-only — introspection deferred to Sprint 4 per the 2026-05-10 lock (see `project_introspection_deferred.md`).**

This file mirrors `sprint-1-signoff.md` / `sprint-2-signoff.md` in shape but tracks an **in-progress** sprint so the repo always reflects ship-status without requiring memory consultation.

## Slice status

| Slice | Status | Commit | Description |
|---|---|---|---|
| Stage 1 (PM/BA) | ✓ done | `86cd815` | Q1–Q6 locked; L-3 supersedes Q4 (TTL = 20s flat). |
| Stage 2 (UX) | ✓ done | within `f63c516` | UC-09 + UC-10 drafted, copy-deck rows 1.13/8.5/8.9/8.10/8.11/10.14/15. |
| Stage 3 (tech arch) | ✓ done | `f63c516` | `sprint-3-tech-arch.md` with sequence diagrams + locked invariants. |
| Stage 4 (multi-agent review) | ✓ done | `f63c516` + `6b09fb7` | 8 BLOCKs / 17 FIXes / 9 NITs after dedup; source-code dive resolved BLOCK-A. |
| Stage 5 (slice lock) | ✓ done | `a909996` | 7-slice plan, L-1..L-5 locked. |
| **Stage 6 implementation** | in progress | (slice commits below) | |
| 3A.0 spikes (C13 + C14) | ✓ done | `6e75764` + source dive `6b09fb7` | F-19 corrected; F-20 + F-21 confirmed at source. |
| 3A.1 orchestrator logout backbone | ✓ done | `d22064a` | Cascade backbone, X-Request-ID CSRF, redirect_url JSON, SPA spinner phases. |
| 3A.2 internal RPC fan-out | ✓ done | `3067074` | `common/revocation/` shared module + receivers wired in 4 services. |
| 3A.2.1 mid-sprint review patches | ✓ done | `fb6dc87` | 6 BLOCKs + 5 FIXes from 5-reviewer audit applied. See `sprint-3-mid-sprint-review.md`. |
| 3A.2.2 live-walk fix #1 | ✓ done | `5045e90` + `ca41150` | `POST_LOGOUT_REDIRECT_URI` env var + drop query string + sessionStorage banner. Operator registered `http://localhost:8090/` on `orchestrator-mcp-client` Callback URLs in IS Console. |
| 3A.2.3 observability pass | ✓ done | `7f2719d` + `380b4ad` | ~38 DEBUG lines across 12 files (validators / MCP tools / CIBA dispatchers / fan-out / BCL / auth-exchange) plus tightened catch-block logging (`str(exc)` + `details=`). `LOG_LEVEL` env override on `install_logging`. |
| 3A.2.4 jti decode + #1/#3/#4 | ✓ done | `8f15189` + `1d84112` | jti decoded from token-B JWT (OAuthToken has no jti field — fan-out + cache eviction were broken without this). X-Request-ID on `/auth/exchange` relay. JWKS prewarm at lifespan startup. httpx INFO muted at non-DEBUG. |
| **Live-walk #5 (full UC-09 + UC-02)** | ✓ all-green | — | 2026-05-09 10:41. Cascade wall-clock 898 ms sequential. All 4 receivers acked. hr_agent cache evicted. Zero anomalies. JWKS prewarm shaved ~1 s off first chat request. |
| 3A.3 MCP server enforcement | ✓ done | `69e0e8e` + `25cc01e` | **Denylist check only** (Step 7). Introspection deferred to Sprint 4 — F-21 confirmed at source means revoke-at-IS doesn't propagate to OBO. `attach_revocation()` + Step 7 in both validators; wired in both `main.py` lifespans. F-15 startup line carries `denylist_enforcement=on` (warns on `=off`). Tests: V-HR-11/12 + V-IT-11/12 green. **R-LOGOUT-5 manual walk: pre-signout 200 → 4-leg fan-out clean → captured token-B replay 401 ERR-MCP-002 reason=denylist_hit (jti `042ebb99…`).** |
| 3A.4 demo polish + UC-09 walkthrough | ✓ done | `f751e05` | Lean rescope 2026-05-10: SPA trace panel + DEMO_MODE gate **dropped**; receipts moved to operator terminal. New `tools/grep-trace.sh` (rid chain + ✓/✗ hop coverage), `COPY.signedOut` tightened to *"Signed out. Agent sessions cleared."*, `showSigninNotice` routes through `announce()` (NIT-8). 3 new tests: R-LOGOUT-6 happy + barrier timeout + R-LOGOUT-8 rid threading (853 / 47 green). UC-09 runbook section with pre-flight, 7-step storyboard, captured-token-replay walkthrough, failure matrix. Manual walk verified by operator. |
| 3B.1 admin-terminate (D3.2) | ✓ done | `73af5b1` + `da47e3a` + `e8b113f` + `5a8e596` | Receiver, 9-check (relaxed typ for WSO2 IS RC), admin-terminate path, SSE session_terminated, sid reverse index. **Live walk evidence (2026-05-10):** admin terminated employee_user's session via Console API → IS POSTed logout_token to `localhost:8123/backchannel-logout` (via C12 reverse-SSH tunnel + new orchestrator host port 8123 binding) → `bcl_typ_header_absent` warning logged + token accepted → cascade fan-out to all 4 receivers acked → SSE pushed BEFORE session delete (BLOCK-H). `tools/grep-trace.sh bcl-…` shows ✓ on all 5 hops. BCL URL was registered via `scripts/set-bcl-url.sh` (Applications API) since the WSO2 IS 7.x Console UI doesn't expose the field for MCP Client Application template. `scripts/synth-bcl.py` retained as deterministic demo backstop. R-LOGOUT-7/7b deferred to 3B.3. |
| 3B.2 binding_message + carries | ✓ done | `a68c55a` + `8201f7b` + `61912a0` | Reason-branched binding_message plumbed end-to-end (cascade → SessionStore → Pattern C → chat fan-out → A2A → dispatcher → `select_template` → render). SPA: admin_terminated banner on `/?reason=…` + SSE `session_terminated` handler. Multi-tool widget race fixed (`8201f7b` — dismiss timer cancel). Reason-aware copy now also rendered as a SPA-side inline callout above Approve (`61912a0`) — the dispatcher already shipped the right `binding_message` to IS via CIBA, but WSO2 IS 7.x RC may not surface it on its consent UI; SPA callout guarantees the user sees the reason regardless. **Open question (parked for IS expert chat tomorrow):** does this RC suppress the per-scope consent screen on MCP Client Application template, and does it render CIBA `binding_message` on its in-browser consent page? See memory `project_is_consent_ux_deferred.md`. R-LOGOUT-7/7b deferred to 3B.3. |
| 3B.3 R-LOGOUT suite + retro | ✓ done | (this commit) | R-LOGOUT-7/7b explicitly tagged in `test_revoke_client.py`. Vestigial cleanup landed: `spa_client_id` field + `ORCHESTRATOR_APP_CLIENT_ID` env var + misleading kwarg gone (memory: `project_orchestrator_app_vestigial.md`). Retro: `sprint-3-retro.md`. Sign-off: `sprint-3-signoff.md`. 898 / 49 tests green strict-mode. |

## What works today (after 3A.2)

End-to-end UC-09 partial walkthrough is wired:

1. SPA Sign Out → `POST /auth/logout` (X-Request-ID required, SameSite=Strict cookie).
2. Orchestrator: per-`user_sub` Lock → `Session.terminating=True` (BLOCK-G fence) → snapshot pending CIBAs + completed_ciba_log → `cancel_event.set()` + await `cancelled_ack` ≤100 ms (BLOCK-F barrier) → `/oauth2/revoke` token-A → parallel fan-out `POST /internal/events` to HR-AGENT/IT-AGENT/hr_server/it_server with retry-once @ 200ms per leg → `Session` removed (LAST mutation per BLOCK-H).
3. Each agent receiver: jti added to in-process denylist + matching `_CachedToken` dropped (O(1) via `_jti_to_cache_key` index).
4. Each MCP server receiver: jti added to denylist (validator does NOT yet consult it — that's 3A.3).
5. Response `{redirect_url}` → SPA navigates to IS `/oidc/logout?id_token_hint=…&client_id=…&state=…` → IS consent screen → confirm → `/?reason=signed_out` banner.
6. Concurrent UC-09 ↔ UC-10 races serialised by per-`user_sub` Lock (FIX-12).
7. SECURITY_DEGRADED ERROR log emitted on all-legs fan-out failure (FIX-6 / R-LOGOUT-7b grep target).

## What 3A.3 changed (2026-05-10)

- `hr_server/auth/validators.py` + `it_server/auth/validators.py`: added `_revocation: RevocationState | None` field, `attach_revocation()` method, and a Step 7 denylist check immediately after the existing F-04 steps 1–6. On hit: `ScopeError(error_id="ERR-MCP-002", details={"jti": ..., "reason": "denylist_hit"})`. Without `attach_revocation()` (test fakes), Step 7 is a no-op.
- `hr_server/main.py` + `it_server/main.py`: added `validator.attach_revocation(revocation)` immediately after constructing the shared `RevocationState`, so the same denylist that the `/internal/events` receiver populates is the one the validator consults.
- Tests: V-HR-11/12 + V-IT-11/12 (denylist hit / miss). Mock validators in `tests/{hr,it}_server/test_main.py` got a no-op `attach_revocation` stub so `create_app()` smoke tests still pass.

## Deliberately NOT in 3A.3 (deferred to Sprint 4)

- `/oauth2/introspect` cache + per-server confidential clients. F-21 (confirmed at IS source 2026-05-09) shows token-A revoke does not propagate to CIBA-issued OBO tokens, so introspection of token-B returns `active=true` even after the parent is revoked — the exact failure mode the backstop was designed to handle. Denylist is the only revocation primitive that actually works on this code path. Sprint 4 does a focused F-21 follow-up (source dive + IS-expert consult + production-roadmap design) before deciding whether introspection re-enters the picture. See `project_introspection_deferred.md`.

## Architectural verdicts captured this sprint

From `docs/spikes/sprint-3-is-source-analysis.md` (subagent dive of WSO2 IS source):

- **F-19 corrected:** WSO2 IS DOES walk session participants and fire BCL when `/oidc/logout` receives `id_token_hint` (or `client_id`). The original C12 spike URL had no `id_token_hint`, hitting the empty-cache branch in `OIDCLogoutServlet.java:272-291`. Our locked Q3 design uses `id_token_hint` so D3.2 admin-terminate gets BCL fan-out as designed.
- **F-20 confirmed:** `/oauth2/revoke` returns 200 for `auth_req_id` but treats it as a no-op. `OAuth2Service` only handles access_token + refresh_token; `AuthReqStatus` enum has no `REVOKED` state. RFC 7009-compliant. 3B.2 will not wire `auth_req_id` revoke; document the Q-LOGOUT-4 ghost-approval caveat.
- **F-21 confirmed:** revoking token-A does NOT propagate to OBO token-B at IS. `revokeAccessTokens(String[])` is a single-row UPDATE; `IDN_OAUTH2_ACCESS_TOKEN` schema has no parent/actor/request_id columns. Architectural choice. **The orchestrator-driven cascade is the only revocation primitive for OBO tokens — gateway pattern required, not preferred.** Demo narrative now has source-code receipts.

`docs/architecture/sprint-1-fixes.md` §F-19 / §F-20 / §F-21 carry the empirical evidence + source addendum.

## Pre-existing test carry-overs (NOT 3A.* regressions)

These were red before Sprint 3 started; do not touch in Sprint 3 unless explicitly asked:

- `tests/orchestrator/auth/test_routes.py::test_login_redirects_to_is_authorize`
- `tests/orchestrator/auth/test_routes.py::test_callback_*` (3 fixtures)
- `tests/orchestrator/chat/test_routes.py::test_chat_single_specialist_ciba_flow`

(`tools/run-tests.sh` reports them as "passing files" because of how the harness greps the summary line — known harness limitation.)

## Manual verification status

3A.1 + 3A.2 have not yet been live-walked. The user requested a live UC-09 walk before continuing 3A.3. Pre-flight checklist below — operator runs each step before the walk, then reports outcomes.

### Live-walk readiness checklist (BA §F + mid-sprint review)

Before starting the UC-09 live walk against the AWS IS:

1. **Set the shared secret in your shell:**
   ```bash
   export INTERNAL_REVOKE_SHARED_SECRET=$(openssl rand -hex 20)
   ```
   Confirm same value across all 5 services:
   ```bash
   docker compose config | grep INTERNAL_REVOKE_SHARED_SECRET
   ```
   Expect 5 identical lines.

2. **Start the stack with the secret active:**
   ```bash
   docker compose up -d --build
   ```
   Tail orchestrator + agent logs and watch for `internal_events_receiver_disabled` WARN lines — these mean a service didn't see the secret. (FIX-2 from mid-sprint review.)

3. **Smoke-test IS `/oauth2/revoke`:**
   ```bash
   docker compose exec orchestrator curl -sk -X POST \
     https://13.60.190.47:9443/oauth2/revoke \
     -u "$ORCHESTRATOR_MCP_CLIENT_ID:$ORCHESTRATOR_MCP_CLIENT_SECRET" \
     -d "token=bogus" -w "%{http_code}\n"
   ```
   Expect HTTP 200 (RFC 7009 200-on-unknown).

4. **Verify `id_token` capture:**
   - Sign in fresh as `employee_user` in the SPA.
   - `docker compose logs orchestrator | grep auth_exchange_success | tail -1` — confirm no errors.
   - The new `id_token_hint` path uses this; F-19 corrected at source means BCL fan-out from IS depends on it.

5. **Smoke-pass UC-02:** ask "What's my leave balance?" → approve consent → confirm reply renders. This populates `completed_ciba_log` so the cascade has something to fan out.

6. **Run the UC-09 walk:** click Sign Out. Watch:
   - SPA: phase-1 spinner *"Revoking access for all agents…"* → phase-2 *"Redirecting to complete sign-out at your identity provider…"* → IS consent screen → confirm → `/?reason=signed_out` banner.
   - `docker compose logs --since 30s` for: `logout_cascade_start`, `token_a_revoked`, `internal_event_sent → hr-agent/it-agent/hr-server/it-server`, `internal_event_received` × 4 in receivers, `hr_dispatcher_revoke_jti`/`it_dispatcher_revoke_jti` cache_dropped=true, no `cancel_barrier_timeout` warnings (BLOCK-A patched).

7. **Captured-token-replay test (will FAIL until 3A.3):** before signing out, capture token-B from `docker compose logs hr_agent | grep -i obo`. After sign-out, `curl http://hr_server:8000/mcp/tools/get_leave_balance -H "Authorization: Bearer <token-B>"` from inside any container. **Expected today: 200 OK** (validator doesn't yet check denylist; that's 3A.3). **Expected after 3A.3: 401 ERR-MCP-002.** This is the demo wedge.

### Known gap (acknowledged)

UC-09 demo step 7 above (captured-token-replay → 401) cannot be demonstrated yet. 3A.3 lands the validator denylist + introspection cache. Live-walk today is QA validation of cookie/SPA/spinners/cascade ordering; the wedge demo waits for 3A.3.

### Live-walk #2 outstanding diagnosis

Operator triggered UC-02 (*"What is my leave balance?"*) → consent approved → `hr_agent` received the OBO token → `hr_server` rejected with HTTP 401, `error_id=ERR-MCP-003`. CIBA flow itself was clean end-to-end at IS. Pre-3A.2.3 logs only emitted the error_id without the underlying reason, hence the observability pass.

After running `LOG_LEVEL=DEBUG docker compose up -d --build`, retry UC-02 and grep:

```bash
docker compose logs --since 2m hr_server | grep -E "validator_entry|claims_decoded|scope_fail|validation failed|tool_entry"
docker compose logs --since 2m hr_agent | grep -E "DEBUG|ciba|mcp_call|token_b|actor_token" | tail -25
docker compose logs --since 2m orchestrator | grep -E "chat_fan_out|tool_iteration|await_completion" | tail -15
```

The new DEBUG output should reveal which check failed:
- `aud` mismatch → `expected_aud` vs `claims.aud`
- scope subset → `required` vs `present` vs `missing` set
- peer-trust → `act.sub` vs `trusted_act_subs`
- signature/iss → JWT validation step 1-2

After diagnosis, patch root cause, retry, then proceed to 3A.3.

## Decisions still in force

| ID | Lock |
|---|---|
| Q3 | IS consent screen on sign-out (spec-pure RP-initiated). |
| Q5 | New `sprint-3-build` branch (non-destructive); `sprint-1-build` preserved as audit. |
| Q6 | 4-receiver fan-out (HR-AGENT, IT-AGENT, hr_server, it_server). |
| L-2 | Ship with SECURITY-DEGRADED labels (F-21 FAIL means denylist is the only OBO revocation primitive). |
| L-3 | Introspection cache TTL = 20 s flat; no Day-4 measurement. |
| L-5 | Time-box overrun → extend up to 2 days, don't cut R-LOGOUT scope. |

## Next slice (3A.3) at a glance

Tech-arch §4.2 + Stage 5 §2 are the source of truth. Headline tasks:

- Extend `hr_server/auth/validators.py` and `it_server/auth/validators.py` with:
  - Step 7 (NEW): denylist check (zero-latency security boundary).
  - Step 8 (NEW): introspection cache + IS round-trip on miss. Negative cache permanent until `exp` (FIX-18). Network errors fail-open, never write a negative cache entry (FIX-10).
- New `IntrospectionCache` class.
- IS introspection client (basic auth as the server's confidential client; the dev IS uses default `admin/admin` credentials per the C13 spike — confirm the production client_credentials path before relying).
- Tests: cache_state × denylist_state correctness matrix (NIT-10) — 6 cases.
- N-test: R-LOGOUT-5 (captured token-B → `hr_server` → 401 `ERR-MCP-002`).

Time-box per Stage 5: ~1 day.
