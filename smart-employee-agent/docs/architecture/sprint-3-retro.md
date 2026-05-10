# Sprint 3 Retrospective

**Sprint:** 3 (revocation: UC-09 user-sign-out + UC-10 admin-terminate)
**Branch:** `sprint-3-build`
**Started from:** Sprint 2 sign-off (`docs/architecture/sprint-2-signoff.md`, 822/43 green)
**Closing tests:** 898 / 49 files green under the strict-mode runner

## Slices delivered

| Slice | Deliverable | Commit | Status |
|---|---|---|---|
| Stage 1 | PM/BA review; Q1–Q6 locked (`sprint-3-stage-1-product-review.md`) | `86cd815` | ✓ |
| Stages 2–4 | UX, tech arch §1–§7, multi-agent review (8 BLOCKs, 17 FIXes, 9 NITs) | `f63c516` + `6b09fb7` | ✓ |
| Stage 5 | 7-slice plan locked, L-1..L-5 (`sprint-3-stage-5-slice-plan.md`) | `a909996` | ✓ |
| 3A.0 | C13 introspection + C14 auth_req_id revoke spikes + IS source dive (F-19/20/21) | `6e75764` + `6b09fb7` | ✓ |
| 3A.1 | Orchestrator logout cascade backbone, X-Request-ID CSRF, BLOCK-F barrier | `d22064a` | ✓ |
| 3A.2 | `common/revocation/` shared module + 4-receiver fan-out + mid-sprint patches | `3067074` + `fb6dc87` + `380b4ad` + `7f2719d` + `8f15189` + `1d84112` | ✓ |
| 3A.3 | MCP server validators — Step 7 denylist enforcement (introspection deferred) | `69e0e8e` + `25cc01e` | ✓ |
| 3A.4 | UC-09 demo polish (rescoped lean): grep-trace.sh + R-LOGOUT-6/8 + SPA aria-live + UC-09 runbook | `f751e05` + `bf78992` | ✓ |
| 3B.1 | Admin-terminate via BCL receiver (D3.2): 9-check validator + sid reverse index + `set-bcl-url.sh` REST PATCH + WSO2 IS typ accommodation | `73af5b1` + `e8b113f` + `da47e3a` + `5a8e596` + `7ed071c` | ✓ |
| 3B.2 | Reason-branched binding messages (D3.4): cascade → SessionStore → Pattern C → A2A → dispatcher → SPA inline callout + multi-tool widget race fix + 8 pre-existing test-debt fixes | `a68c55a` + `8201f7b` + `61912a0` + `87a3405` | ✓ |
| 3B.3 | This retro + R-LOGOUT-7/7b explicitly tagged + vestigial `spa_client_id` cleanup + sign-off | (this commit) | ✓ |

## Live-verified end-to-end (browser walkthroughs)

As `employee_user` (sub `2048ad8c-…`) against WSO2 IS 7.x RC at `13.60.190.47:9443`:

- **R-LOGOUT-5 (UC-09 captured-token-replay):** captured token-B (jti `042ebb99…`) returned 200 pre-signout, 401 ERR-MCP-002 reason=denylist_hit post-signout. All 4 fan-out legs acked. `tools/grep-trace.sh` shows ✓ on all 5 hops (orchestrator + hr_agent + it_agent + hr_server + it_server). 2026-05-10.
- **D3.2 admin-terminate (UC-10):** admin DELETEd session via Applications API → IS POSTed `logout_token` to `localhost:8123/backchannel-logout` (via C12 reverse-SSH tunnel + new orchestrator host port 8123 binding) → BCL receiver accepted → cascade fan-out → SSE `session_terminated` pushed BEFORE session.delete (BLOCK-H) → SPA auto-logged-out and showed admin_terminated banner. 2026-05-10.
- **D3.4 reason-branched binding message:** end-to-end propagation verified via orchestrator log trace (`logout_cascade_start reason=admin_terminated` → `auth_exchange_logout_reason_consumed` → `chat_fan_out propagating_logout_reason` → `hr_dispatcher_binding_reason_applied`). SPA consent widget renders the admin_terminated copy as a warm-tinted inline callout above the Approve button regardless of what IS displays.

## What went well

- **Source-dive saved a sprint.** The 7f2719d / 6b09fb7 source-code analysis of `OAuth2Service.revoke*()`, `DefaultLogoutTokenBuilder`, and the `IDN_OAUTH2_ACCESS_TOKEN` schema produced *empirical evidence* for F-19 (corrected — IS does walk session participants when given `id_token_hint`), F-20 (confirmed — `auth_req_id` revoke is a no-op at IS), and F-21 (confirmed — token-A revoke does NOT propagate to OBO). Each finding shaped a different slice. F-21 alone collapsed introspection from "Sprint 3 backstop" to "Sprint 4 reconsideration item" (`project_introspection_deferred.md`) — saved at minimum a day of building a primitive that wouldn't have fired for the failure modes it was designed to catch.
- **Multi-agent reviews caught real problems before live walks.** The 3A.3 review (architect / python-pro / security-engineer in parallel) flagged the spurious "avoid cycle" import + the unobservable `attach_revocation()` invariant + the `*.pem` gitignore gap. All three landed as `25cc01e` follow-up patches. Same pattern at 3B.1 (architect / explorer / security) caught the orchestrator-app vestigial drift and the synth-bcl key-handling guidance gap.
- **Stage-gated cadence held.** PM review → tech arch → multi-agent review → slice-by-slice implementation → test → manual verify → commit. No mid-sprint scope drift; the only rescope was 3A.4 (lean GUI per audience awareness, decided 2026-05-10 — the user "this POC isn't shipping to prod" framing was the lever).
- **`set-bcl-url.sh` worked first try after the GET → merge → PUT fix.** WSO2 IS Applications API requires a full-replacement PUT body, not a partial patch. Once that landed (and after dropping `state` + `clientSecret` per spec readonly/scope notes), one curl idempotently sets `back_channel_logout_uri` on `orchestrator-mcp-client`. This bypasses the modern Console UI gap entirely.

## What hurt

- **WSO2 IS browser-SSO trap.** Halfway through a UC-10 walk, the SPA login silently used the IS Console admin's existing browser session via SSO and minted token-B for `admin` (sub `a954fd6d…`) instead of `employee_user` (sub `2048ad8c…`). Symptom surfaced as ERR-MCP-003 missing-`hr_self_rest`-scope on hr_server because admin doesn't have the employee scope. Diagnostic time: ~15 min. Memory: live finding logged into 3B.1 retro commentary; recommend incognito/private window for every demo run.
- **WSO2 IS 7.x RC packaging quirks.**
  1. The "MCP Client Application" template doesn't expose `back_channel_logout_uri` in the modern Console UI even though the underlying `IDN_OAUTH2_APP_OIDC_PROPS` schema supports it. Worked around by Applications REST API.
  2. WSO2 IS emits BCL `logout_token` with no `typ` header (spec REQUIRES `logout+jwt`). Soft-checked in our receiver: absent OK, present-and-wrong rejected. Memory: `project_wso2_is_bcl_typ_omission.md`.
  3. Whether IS actually surfaces CIBA `binding_message` on its in-browser consent screen is unknown for this RC. Workaround: SPA-side inline callout. Memory: `project_is_consent_ux_deferred.md`. Open question for IS-expert consultation.
- **Multi-tool consent widget dismiss-timer race.** After HR's DONE, a `setTimeout(... 300ms)` dismiss timer was firing AFTER the next agent's widget had rendered, hiding it. Symptom from the user's perspective: "IT Agent doesn't start." From the orchestrator: it_agent polled IS for 4 minutes waiting for an approval that the user couldn't give. Fix: track dismiss-timer + cancel on next render. (`8201f7b`).
- **Test runner hid 8 real failures behind a regex bug.** `tools/run-tests.sh` matched any line containing "passed" — including "X failed, Y passed". Eight tests had been failing since well before Sprint 3 started: stale `/auth/callback` route assertions, `spa_client_id`/`orchestrator-app` legacy strings, and a fixture skill-count drift. The strict-mode fix surfaced them; we cleaned them up in 3B.2 (`a68c55a`). Lesson: validate the validator. Cost: probably ~30 min of confused diff between strict vs lax counts before realising.
- **Container rebuild cycle wears the operator down.** Every code change to `common/*` requires `docker compose build` of every dependent service and a manual hard-refresh of the SPA. By 3B.2 we'd done this dozens of times. Sprint 4 candidate: a faster dev-server mode or `mount: source` for python code so the container picks up edits without rebuild.
- **Compose secret drift.** Selectively rebuilding one container while others stayed on a prior shell-set `INTERNAL_REVOKE_SHARED_SECRET` produced silent fan-out failures (`logout_fanout_partial body='{"detail":"invalid_secret"}'`). Memory: `feedback_compose_secret_alignment.md`. Operator now runs the alignment loop pre-flight.

## Bug ledger (manual-test surface area)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| B-25 | `OAuthToken` had no `jti` attribute → fan-out + cache eviction silently broken | The Pydantic model didn't carry it; `_jti_to_cache_key` index never populated | Decode jti from token-B JWT body via PyJWT (no sig-verify) — `8f15189` |
| B-26 | `post_logout_redirect_uri` rejected by IS as "Post logout URI does not match" | Trailing `?reason=signed_out` query string broke IS's exact-match | Drop the query string; remember just-signed-out via `sessionStorage` instead — `5045e90` + `ca41150` |
| B-27 | Live walk #1: cascade fired but receivers returned 401 invalid_secret on half the legs | `INTERNAL_REVOKE_SHARED_SECRET` not aligned across services after partial rebuild | Recreate all 5 services in one pass; persist secret in gitignored `.env` for compose to pick up — recorded in memory `feedback_compose_secret_alignment.md` |
| B-28 | Captured token-B replay returned 200 instead of 401 after sign-out | Orchestrator session had no `completed_ciba_log` record (cache served the request) | Restart hr_agent to wipe its in-memory cache before re-running R-LOGOUT-5 |
| B-29 | BCL POST got 400 `typ_not_logout_jwt` | WSO2 IS RC emits BCL with no `typ` header | Soft-check: accept absent typ, still reject wrong typ — `5a8e596` |
| B-30 | BCL POST never arrived on first attempt despite C12 tunnel up | `bcl-listener` spike-profile container was bound to host port 8123 | Stop bcl-listener; add `127.0.0.1:8123:8080` to orchestrator port mapping — `da47e3a` |
| B-31 | UC-10 BCL fired but cascade had nothing to revoke (`logout_cascade_already_run` warning) | Cascade ran ahead of `find_sessions_for_user` finding the matching session | (Intentional idempotent short-circuit; not a bug, but noted as a confusing log line on the first walk.) |
| B-32 | Multi-tool fan-out: only HR Agent appeared in SPA; IT widget vanished | Stale 300 ms dismiss-timer hid the next agent's widget after HR DONE | Track + cancel dismiss-timer on next render; race-guard the timeout callback — `8201f7b` |
| B-33 | Reason-aware binding message rendered correctly server-side but never visible to the user | WSO2 IS RC may not surface CIBA `binding_message` on its consent UI; SPA widget displayed only `binding_code`, not the full text | Forward `binding_message` through SSE `CibaUrlEvent`; SPA renders inline callout when text contains "previous session" — `61912a0` |

## Architectural decisions confirmed in Sprint 3

- **Denylist on receivers is the only working revocation primitive for OBO tokens.** F-21 source dive proved IS's `revokeAccessTokens(String[])` is a single-row UPDATE; `IDN_OAUTH2_ACCESS_TOKEN` has no parent linkage. So token-A revoke does NOT propagate to OBO. The orchestrator-driven cascade + per-receiver in-process denylist is the gateway pattern, not a preference.
- **Step 7 denylist check after F-04 steps 1–6** (signature, iss, exp, aud, act, scope) — placement matters. Earlier placement would have been a forged-jti information leak; post-validation placement requires an attacker to first present a *signed-by-IS, scope-correct, audience-correct* token before they can probe denylist membership, which they already have. This was the architect's call (3A.3 review).
- **WSO2 IS 7.x RC config quirks documented and worked around at the protocol layer.** None of the workarounds (synth-bcl.py, set-bcl-url.sh REST PATCH, soft typ check, SPA-side binding_message callout) requires changes to IS itself. All four are ready for upstream when the RC ships.
- **Pattern C confidential client for user login is correct.** The MCP Client Application template IS the right home for an app that brokers downstream tokens. The vestigial v3 `orchestrator-app` SPA registration is now removed from config (3B.3 cleanup); the Console-side app can be deleted whenever an operator wants to.

## Carry-overs to Sprint 4

| Item | Rationale | Where it lives |
|---|---|---|
| Introspection consultation | F-21 broke the backstop story; need IS-expert confirmation on whether session-revocation events propagate, then decide ship-introspection vs. formalise-denylist-only | `project_introspection_deferred.md` |
| WSO2 IS expert consultation: per-scope consent screen + binding_message visibility on RC | Two open UX questions parked from 3B.2 | `project_is_consent_ux_deferred.md` |
| Persistent denylist (Redis or similar) | Required for multi-worker uvicorn; current Q5 single-worker invariant is a POC accommodation | tech-arch §6 |
| Orchestrator-app SP cleanup in IS Console | Code references gone (3B.3); operator action: delete SP or clear its Front-channel-logout URL field | Operator |
| Cross-process session store | Sprint 2 carry-over; orchestrator restart = forced re-login during dev | Backend |
| `bcl_listener` spike profile retirement | C12 capture tool no longer needed now that real BCL plumbed; can stay under spike-bcl profile or be removed | Cleanup |
| Branch rename `sprint-3-build` → merge to `main` (or rename) | End-of-sprint hygiene | Git wrangler |

## Numbers

- 27 commits across Sprint 3 (Stages 1–5 prep + 8 implementation slices + ops/observability/cleanup commits).
- 898 / 49 files of green tests at close (up from 822 / 43 at Sprint 2 sign-off; +76 new tests across BCL receiver, R-LOGOUT, binding_messages, logout_handler).
- 3 architectural memos saved for future-us:
  - `project_introspection_deferred.md`
  - `project_orchestrator_app_vestigial.md`
  - `project_wso2_is_bcl_typ_omission.md`
  - `project_is_consent_ux_deferred.md`
  - `feedback_compose_secret_alignment.md`
