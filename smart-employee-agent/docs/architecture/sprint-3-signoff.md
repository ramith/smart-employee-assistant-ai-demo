# Sprint 3 — M3 Sign-off

**Sprint:** 3 (revocation: UC-09 user-sign-out cascade + UC-10 admin-terminate)
**Branch:** `sprint-3-build`
**Closed:** 2026-05-10
**Tests:** 898 / 49 files green under the strict-mode runner
**Sign-off basis:** browser walkthrough against live WSO2 IS 7.x RC at `13.60.190.47:9443`

## Acceptance — Sprint 3 D3.x deliverables

| Ref | Acceptance | Status | Evidence |
|---|---|---|---|
| **D3.1** | User clicks Sign Out → orchestrator runs cascade (cancel pending CIBAs → revoke token-A at IS → fan-out denylist push to all 4 receivers → drop session) → captured token-B replay against any receiver returns 401 ERR-MCP-002 | ✓ | Live-walk 2026-05-10 (jti `042ebb99…`): pre-signout 200 → 4-leg fan-out clean → captured token replay 401 ERR-MCP-002 reason=denylist_hit. R-LOGOUT-5 manual walk green. Hop coverage all 5 ✓ via `tools/grep-trace.sh`. |
| **D3.2** | Admin clicks Terminate in IS Console → IS POSTs OIDC `logout_token` to orchestrator's `/backchannel-logout` → 9-check validator passes → cascade runs end-to-end | ✓ | Live-walk 2026-05-10 (rid `bcl-1778395951738`): admin DELETE via Applications API → IS POSTed logout_token via C12 reverse-SSH tunnel → `bcl_received user_sub=… sub_present=True sid_present=True` → cascade fan-out to all 4 receivers acked → SSE `session_terminated` pushed BEFORE session.delete (BLOCK-H). |
| **D3.3** | R-LOGOUT-1..8 + R-LOGOUT-7b green in CI | ✓ | R-LOGOUT-5 (denylist hit on captured token) — `tests/{hr,it}_server/auth/test_validators.py` V-HR-11/12, V-IT-11/12. R-LOGOUT-6 (logout cancels in-flight CIBA, BLOCK-F barrier) — `tests/orchestrator/auth/test_logout_handler.py`. R-LOGOUT-7 (partial fan-out → WARN per leg, no SECURITY_DEGRADED) and R-LOGOUT-7b (all-legs failure → SECURITY_DEGRADED label) — `tests/orchestrator/agent_registry/test_revoke_client.py`. R-LOGOUT-8 (rid threading) — `test_logout_handler.py`. R-LOGOUT-EX-3 (forged BCL → 400, cascade NOT triggered) — `tests/orchestrator/auth/test_bcl_receiver.py`. R-LOGOUT-1..4 covered by composite tests + 3A live walks. |
| **D3.4** | Re-login after admin-terminate → next CIBA's binding message visibly distinct from a routine re-CIBA, reflecting the admin-terminated reason | ✓ | Backend propagation verified by orchestrator log trace: `logout_cascade_start reason=admin_terminated` → `auth_exchange_logout_reason_consumed` → `chat_fan_out propagating_logout_reason rid=… agent_id=hr_agent` → `hr_dispatcher_binding_reason_applied reason=admin_terminated is_refresh=False`. SPA-side: reason copy rendered as a warm-tinted inline callout above Approve regardless of what IS displays on its consent screen (`61912a0`). 14 unit tests in `test_binding_messages.py` pin the precedence rules. |

## Acceptance — Sprint-3 architectural invariants

| Ref | Invariant | Status | Evidence |
|---|---|---|---|
| **BLOCK-B** | All sessions for a `user_sub` get the cascade (multi-browser case) | ✓ | `LogoutHandler._execute_locked` calls `find_sessions_for_user`; tests pin one + multi-session shapes. |
| **BLOCK-C #9** | OIDC `logout_token.sid` resolved to `user_sub` via reverse index | ✓ | `SessionStore.register_sid` populated at code-exchange from `id_token.sid`; resolved by `bcl_receiver` when `sub` absent. |
| **BLOCK-D** | `/internal/events` receiver bound loopback only; trust boundary = shared secret + per-IP rate limit | ✓ | `127.0.0.1:8001`/`8002`/`8000`/`8004` host bindings; HMAC `X-Internal-Auth` constant-time compare. |
| **BLOCK-F** | Pending CIBA cancel-event set + `cancelled_ack` barrier observed BEFORE fan-out | ✓ | `_cancel_pending_ciba` in logout_handler; R-LOGOUT-6 happy + timeout tests pin both branches. |
| **BLOCK-G** | `Session.terminating = True` is the FIRST state mutation in the cascade | ✓ | Set in `_execute_locked` before any other action. |
| **BLOCK-H** | `session_terminated` SSE pushed BEFORE `session.delete` (LAST mutation) | ✓ | `test_block_h_sse_event_enqueued_before_session_delete`. |
| **BLOCK-I** | Single uvicorn worker per service (in-process denylist correctness) | ✓ | `assert UVICORN_WORKERS == 1` at every receiver's startup. |
| **F-15 / N28** | `denylist_enforcement=on` startup line on both MCP servers | ✓ | `log_startup_assertion` extended in 3A.3 follow-up; SIEM-grep target. WARNING fires when `attach_revocation()` was skipped (regression alarm). |
| **FIX-3** | `SeenLogoutTokens` bound + sweep | ✓ | 10k FIFO cap; sweep-once + sweep_loop; tests pin both. |
| **FIX-6** | All-legs fan-out failure emits `SECURITY_DEGRADED` ERROR label | ✓ | R-LOGOUT-7b test pins the literal log string. |
| **L-2** | Ship with SECURITY-DEGRADED labels for the user-driven `/oauth2/revoke` paths | ✓ | F-21 didn't change this; SECURITY_DEGRADED label is for the fan-out, not the IS revoke. |

## Use-case coverage at sign-off

| UC | Status | Path |
|---|---|---|
| UC-01..08 | ✓ | All carried over from Sprint 2; spot-checked again during 3A live walks. |
| UC-09 (user sign-out cascade + captured-token-replay rejection) | ✓ | Live (R-LOGOUT-5 walk + grep-trace.sh) |
| UC-10 (admin-terminate via BCL) | ✓ | Live (admin DELETE via API → BCL → cascade) |

## Carry-overs to Sprint 4

Documented in `sprint-3-retro.md`. Highlights:
- **Introspection consultation** — `project_introspection_deferred.md` (F-21 follow-up with WSO2 IS expert).
- **IS consent-screen UX questions** — `project_is_consent_ux_deferred.md` (per-scope screen + binding_message visibility).
- **Persistent denylist** for multi-worker support (Q5 single-worker is a POC accommodation).
- **Branch hygiene** — merge `sprint-3-build` to `main` or rename.

## Notes on the WSO2 IS RC

This sprint shipped against an unreleased WSO2 IS RC (`wso2is-7.3.0` packaging path; release-candidate per `project_wso2is_is_release_candidate.md`). Three RC-specific accommodations are now part of the codebase, all behind small documented switches:

- `scripts/set-bcl-url.sh` — sets two logout-related fields on `orchestrator-mcp-client` via the Applications REST API because the modern Console UI doesn't expose either for the MCP Client Application template: (1) `backChannelLogoutUrl` (needed for admin-terminate BCL), and (2) ensures `callbackURLs` covers `http://localhost:8090/` via a `regexp=` pattern so IS accepts `post_logout_redirect_uri` at RP-initiated logout without returning `access_denied / Post logout URI does not match`. Script is idempotent (GET → merge → PUT). `check-is-config.py` Section 4b now FAILs if either URL is absent.
- `bcl_receiver.validate_logout_token` — accepts absent `typ` header (RC emits BCL without it; spec REQUIRES `logout+jwt`). Soft-check; categorical separator is the events claim (#7).
- SPA-side `binding_message` callout — visible regardless of whether IS surfaces it on the in-browser consent screen (RC behaviour unconfirmed).

When IS GAs and these RC quirks are clarified, the workarounds either become no-ops or get formalised. Tracked in Sprint 4 backlog.
