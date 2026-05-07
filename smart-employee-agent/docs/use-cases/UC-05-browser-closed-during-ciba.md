# UC-05 — Browser closed during CIBA polling

**Sprint:** 2 (initial Sprint 1 implements path; Sprint 2 polishes detection + cleanup)
**Priority:** High (security-relevant — must not leave zombie polling or orphan tokens)
**Maps to N-tests:** N20 (auth_req_id timeout), N23 (browser closed mid-CIBA)
**Maps to scenarios:** [user-experience.md](../user-experience.md) Scenario D-7

## Actors
- Same as UC-02.

## Preconditions
- A CIBA flow is in progress (Consent Widget visible on the SPA, specialist polling /oauth2/token).

## Trigger
The user closes the browser tab, closes the entire browser, or loses network — while the Consent Widget is awaiting their action OR while the specialist is still polling after they clicked Approve.

## Main flow

### Variant A — User closes browser BEFORE clicking Approve
1. SPA's SSE connection to `<orch>/events/<session_id>` drops.
2. Orchestrator detects the dead SSE connection (within 10–30s, depending on TCP keepalives).
3. Orchestrator marks the session's pending CIBA state as abandoned: `{status: "user_disconnected"}`.
4. Orchestrator signals the specialist to stop polling (in-process call by auth_req_id).
5. Specialist cancels its polling loop.
6. Optionally (Sprint 3 — gated on capability test C10), orchestrator/specialist also signals IS to invalidate the auth_req_id.
7. If no signal to IS, the auth_req_id naturally expires after `expires_in` (default 300s).
8. **No token is ever issued** because the user never approved.

### Variant B — User closes browser AFTER clicking Approve, before specialist finishes polling
1. User clicked Approve on IS; IS recorded the consent.
2. User closes browser. SPA SSE drops.
3. **Specialist will receive a token from polling** before it knows the user is gone.
4. Specialist returns A2A response with `{type: "result", ...}` (or `{type: "consent_required"}` if the polling already returned the token).
5. Orchestrator notes the SSE channel is dead; **does not retry** to push results.
6. The token is in the session map but unused; it will expire naturally after its TTL (default 1h).
7. **Sprint 3 hooks:** on session timeout, orchestrator revokes the unused token to be tidy. (Out of Sprint 1 scope.)

## Exception flows

### EX-1 — SSE detection is slow / TCP keepalive is delayed
- Worst case: orchestrator doesn't detect disconnection for ~60s. During that time, the polling loop continues uselessly.
- Acceptable for a demo. Production: configure shorter TCP keepalive on the orchestrator side.

### EX-2 — User reopens the same tab quickly (refresh / re-navigate)
1. SPA reconnects SSE with the same `session_id` (cookie persisted).
2. Orchestrator looks up: was there a pending CIBA for this session? Yes.
3. Orchestrator re-emits the most recent `ciba_url` SSE event with current `expires_in - elapsed_time` countdown.
4. Widget reappears mid-flight; user can complete approval if time remains.

### EX-3 — User reopens but on a different device / browser
1. New session. The pending CIBA on the old session is unaffected.
2. Old session's pending CIBA times out at `expires_in`.

### EX-4 — Orchestrator process is killed mid-flow
- Memory state lost (single-process, in-memory per Q5 decision).
- All pending CIBA flows orphaned. Tokens issued to dead sessions remain in IS until natural expiry.
- **Document as known limitation** in Sprint 1 ops notes. Production: persistent state in Redis (out of scope).

## Postconditions
- **Success:** specialist polling loop stopped; no token issued OR issued-and-unused token will expire naturally; orchestrator's session map cleaned up; logs show clean cancellation entries.
- **Failure modes:** zombie polling threads consuming CPU; tokens orphaned with no record; stale SSE channels.

## Design notes for downstream stages

### UX (Stage 3)
- **No special UX needed for the user** in Variant A — they closed the browser, they don't see anything.
- **Variant B (re-open)**: the widget reappears with the remaining `expires_in`. UX must show this clearly: amber banner *"Resuming previous request — 2:34 left to approve."*

### Architecture (Stage 4)
- **SSE keepalive:** orchestrator emits a no-op SSE comment line every 15s; the SPA's `EventSource` API automatically detects disconnection on TCP-level errors.
- **Server-side cancellation primitive:** specialist's CIBA poll task is an `asyncio.Task` stored under `auth_req_id` in the orchestrator-shared map. Cancellation is `task.cancel()` from the orchestrator's session-cleanup hook.
- **Idempotent cleanup:** cancelling a task that's already done or already cancelled must be a no-op.
- **Re-attach hook (EX-2):** session lookup `pending_ciba.get(auth_req_id)` returns the live state if it exists, including a remaining-time computation.

### Testing (Stages 7–8)
- **N23 automated:** integration test — start a CIBA flow, drop the SSE connection programmatically, assert the polling task transitions to cancelled within 30s.
- **N20 automated:** start CIBA, sleep `expires_in + 5`, assert IS responds with `expired_token` and the orchestrator surfaces a graceful timeout message.
- **Manual:** during demo rehearsal, deliberately close the tab mid-CIBA. Verify no error appears in the orchestrator log beyond an INFO "session disconnected; cancelling pending CIBA flow."
