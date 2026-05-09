# Sprint 3 — build state (in-progress)

**Last updated:** 2026-05-09 evening.
**Branch:** `sprint-3-build` @ commit `3067074` (pushed to origin).
**Tests:** 843 / 46 files green (per `tools/run-tests.sh`).

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
| 3A.3 MCP server enforcement | ◯ pending | — | Validator denylist check + 20s introspection cache. |
| 3A.4 demo polish + UC-09 walkthrough | ◯ pending | — | Trace panel `DEMO_MODE` gate, a11y, demo-runbook UC-09 section. |
| 3B.1 admin-terminate (D3.2) | ◯ pending | — | Orchestrator BCL receiver with full 9-check spec validation. |
| 3B.2 binding_message + carries | ◯ pending | — | Reason-branched binding_message; admin-terminate banner. |
| 3B.3 R-LOGOUT suite + retro | ◯ pending | — | R-LOGOUT-1..8 + R-LOGOUT-7b automated; sprint-3-retro.md. |

## What works today (after 3A.2)

End-to-end UC-09 partial walkthrough is wired:

1. SPA Sign Out → `POST /auth/logout` (X-Request-ID required, SameSite=Strict cookie).
2. Orchestrator: per-`user_sub` Lock → `Session.terminating=True` (BLOCK-G fence) → snapshot pending CIBAs + completed_ciba_log → `cancel_event.set()` + await `cancelled_ack` ≤100 ms (BLOCK-F barrier) → `/oauth2/revoke` token-A → parallel fan-out `POST /internal/events` to HR-AGENT/IT-AGENT/hr_server/it_server with retry-once @ 200ms per leg → `Session` removed (LAST mutation per BLOCK-H).
3. Each agent receiver: jti added to in-process denylist + matching `_CachedToken` dropped (O(1) via `_jti_to_cache_key` index).
4. Each MCP server receiver: jti added to denylist (validator does NOT yet consult it — that's 3A.3).
5. Response `{redirect_url}` → SPA navigates to IS `/oidc/logout?id_token_hint=…&client_id=…&state=…` → IS consent screen → confirm → `/?reason=signed_out` banner.
6. Concurrent UC-09 ↔ UC-10 races serialised by per-`user_sub` Lock (FIX-12).
7. SECURITY_DEGRADED ERROR log emitted on all-legs fan-out failure (FIX-6 / R-LOGOUT-7b grep target).

## What is NOT yet enforced (3A.3 land)

- Captured token-B presented directly to `hr_server` after a logout still passes the validator. The denylist exists at the server but is not consulted on every request.
- No `/oauth2/introspect` calls from the MCP servers (Sprint 2 had them feature-flagged off). 3A.3 introduces the introspection cache (20 s TTL per L-3) that consults IS for validation.

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

3A.1 + 3A.2 have not yet been live-walked. The next opportunity is the 3A.4 manual-verification step (per Stage 5 slice exit criteria), which exercises the full UC-09 end-to-end against the AWS IS. Until then, the cascade is unit/integration-tested against mocks (httpx MockTransport for fan-out, asyncio fakes for the denylist).

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
