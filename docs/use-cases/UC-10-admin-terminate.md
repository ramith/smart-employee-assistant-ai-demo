# UC-10 — Admin terminates user session via IS Console

**Sprint:** 3 (Stage 2 deliverable, Sprint 3B)
**Priority:** High (D3.2 — the "kill switch" defense story; complements UC-09)
**Maps to R-tests:** R-LOGOUT-1..R-LOGOUT-5, R-LOGOUT-8 (with admin-terminate trigger)
**Maps to D3.2** ([`milestone-plan.md`](../milestone-plan.md) §3 Sprint 3)
**Stage 1 decisions referenced:** Q6 (4-receiver fan-out)

## Actors

- **Primary:** IT/IAM administrator (any user with WSO2 IS Console access).
- **Secondary:** WSO2 IS, Orchestrator (BCL receiver), HR-AGENT, IT-AGENT, hr_server, it_server.
- **Affected:** Authenticated end-user (offline or online — both must be cleaned).

## Preconditions

- End-user has an active orchestrator session (token-A valid at IS, `Session` entry in `session_store`, possibly cached OBO tokens at HR-AGENT / IT-AGENT).
- Admin has IS Console credentials with permission to terminate user sessions.
- Orchestrator is registered as a BCL receiver in IS Console: `Application "orchestrator-mcp-client" → Inbound Authentication → OIDC → Logout URLs → Back-channel logout URL = http://orchestrator:8090/backchannel-logout`. (IS reaches it via docker-compose internal network OR via the same reverse-SSH tunnel from the C12 spike rig — TBD at Stage 4.)

## Trigger

Admin opens IS Console → User Management → selects user (e.g. `employee_user`) → **Active Sessions** tab → clicks **Terminate** on the active session row.

## Main flow

1. Admin clicks **Terminate**. IS Console returns confirmation.
2. IS internally:
   - Marks the user's session as terminated.
   - Walks the user-session table for RP applications with `back_channel_logout_uri` registered.
   - **Per F-19, only the orchestrator app receives the BCL POST** (CIBA-issued grants for HR-AGENT and IT-AGENT are NOT in the user-session table). Agent-side BCL receivers are intentionally not registered.
3. IS POSTs to `http://orchestrator:8090/backchannel-logout` with body:
   ```
   logout_token=<JWT>
   ```
   where the JWT carries claims `iss`, `aud=orchestrator-mcp-client`, `iat`, `jti`, `events: {"http://schemas.openid.net/event/backchannel-logout": {}}`, and at least one of `sub` / `sid`.
4. Orchestrator's BCL receiver (the new D3.2 endpoint):
   - Validates the `logout_token` per OIDC BCL spec §2.6 (signature against IS JWKS, `iss`, `aud`, `iat` ≤300 s old, `events` claim, no `nonce`).
   - Resolves the user from `sub` (or `sid` → `sub` mapping if present).
   - Returns 200 to IS within 5 s (BCL spec hint; IS doesn't normally retry).
5. Orchestrator runs the **same cascade as UC-09 steps 2–8** but trigger and SPA notification differ. **Stage 4 ordering invariants apply** (BLOCK-F/G/H, FIX-12):
   - Acquire per-`user_sub` `asyncio.Lock` (FIX-12 — serialises against concurrent UC-09 sign-out for same user).
   - Look up `Session` entries for `user_sub` (multi-browser → multiple).
   - For each session: set `Session.terminating = True` (BLOCK-G); snapshot `(agent_id, jti, exp)` pairs and `pending_ciba`.
   - `cancel_event.set()` + await `cancelled_ack` barrier ≤100 ms (BLOCK-F).
   - `POST /oauth2/revoke` for token-A (defense-in-depth; IS already revoked on admin-click).
   - Fan-out `POST /internal/events` (FIX-7 rename) to all 4 receivers (Q6) with inline retry-once @ 200 ms (FIX-22). Body carries `reason: "admin_terminated"` (FIX-12 reason precedence — wins over concurrent `user_signed_out` for audit/banner).
6. **If the user is online** (open SPA with active SSE), with **flush-before-drop ordering** (BLOCK-H):
   - SSE channel emits `session_terminated` event with `reason: "admin_terminated"`.
   - Orchestrator awaits an SSE flush ack OR a 50–100 ms drain (whichever is implemented by `common/sse.py`).
   - **Then** removes the `Session` entry from `session_store`. (Removing before flush would tear down the channel before the message reached the client — UC-10 banner would never fire.)
   - SPA receives the event, clears `localStorage.orch_session_id`, navigates to `/?reason=admin_terminated`.
   - SPA shows banner *"Your session has ended. Sign in again to continue."* (FIX-15 — neutral copy; cause attribution lives in admin audit log, not user banner).
7. **If the user is offline** (no open SPA tab):
   - All cleanup completes server-side. Next time the user opens the SPA, the cookie is stale → 401 on first API call → SPA redirects to login. The user does not see the admin-terminated banner; they see the standard login page. (UX trade-off accepted; user discoverability via audit log is the admin's recourse.)
8. Release the per-`user_sub` lock.

**Total budget:** D3.2 acceptance is ≤5 s end-to-end (IS BCL latency + orchestrator fan-out). Orchestrator's portion (steps 4–6) ≤2 s.

## Exception flows

### EX-1 — IS does NOT fire BCL to the orchestrator (would invalidate F-19 understanding)

1. Admin clicks Terminate; orchestrator BCL endpoint receives **nothing** within 5 s.
2. **F-19 was specifically about CIBA-issued tokens to agent apps** — the orchestrator app uses authorization_code (Pattern C), so it should be in IS's user-session table and SHOULD receive BCL. But this needs explicit verification in Sprint 3B.1 before locking the design.
3. **3B.1 verification step:** trigger admin-terminate against an authenticated `employee_user` while the C12 spike rig is up; confirm orchestrator BCL receiver logs `bcl_received`. If FAIL, escalate as F-20 — D3.2 must fall back to polling-based detection (orchestrator periodically introspects token-A; on `active=false`, runs the cascade) which is materially worse and bumps D3.2 to a Sprint 3 stretch goal.
4. **Acceptance for 3B.1:** R-LOGOUT-with-admin-trigger passes. Document outcome in `sprint-1-fixes.md` as F-20 (PASS or FAIL).

### EX-2 — User has multiple active SPA sessions

1. Step 5 of main flow finds `[Session-1, Session-2]` for `user_sub`.
2. Orchestrator iterates both, calling fan-out per session (FIX-20 multi-session loop — N×4 calls).
3. Both SSE channels emit `session_terminated`; both browsers clean up.
4. If the user has the same browser with two tabs sharing one cookie → one `Session` entry; one SSE channel; both tabs receive the same event via the shared channel.
5. **UX note (FIX-7 from ux review):** when two separate browsers/devices receive the event, SSE delivery may be 50–300 ms apart — visible cascade between tabs. **This is expected and acceptable.** Demo runbook narrator note: *"Both tabs clean up — the slight delay between them is the SSE delivery window, not a failure."*

### EX-3 — `logout_token` validation fails (replay / tampered / expired)

1. Step 4 of main flow: signature verification fails OR `iat` >300 s old OR `aud` mismatch.
2. Orchestrator returns 400 to IS with body `{"error": "invalid_logout_token", "detail": "<reason>"}`.
3. Logs `WARN bcl_invalid_token reason=… iss=…`.
4. No fan-out runs. The supposed admin-terminate event is *not honoured*.
5. This protects against an attacker forging BCL POSTs to forcibly log users out.

### EX-4 — User is mid-CIBA (consent widget visible) at admin-terminate

1. Same as UC-09 EX-1 (Pending CIBA at logout) — `cancel_event.set()` terminates the poll cleanly.
2. The SSE `session_terminated` event arrives at the SPA before the user can interact with the consent widget.
3. SPA navigates away; widget is destroyed.

## Postconditions

- **Success:**
  - User's `Session` removed from `session_store`.
  - All cached OBO tokens for the user dropped from HR-AGENT, IT-AGENT.
  - jti present in denylists across all 4 receivers.
  - IS audit log records `Logout` triggered by admin (not user).
  - Online user: SPA on login page with admin-terminated banner. Offline user: next visit lands on login page.
- **Failure modes:**
  - EX-1 (BCL not fired): D3.2 acceptance fails; escalate to 3B.1 verdict.
  - EX-3 (token validation fails): event is silently ignored; admin must verify the IS audit log.

## Design notes for downstream stages

### UX (Stage 3 — wireframes; Stage 4 amendment FIX-15)

- **Online user banner (FIX-15):** *"Your session has ended. Sign in again to continue."* — neutral about cause. The amber styling stays (signals "something unusual"); the copy doesn't. Cause attribution (routine cleanup vs. account suspension vs. security incident) belongs in the admin audit log, not the user-facing banner. The previous draft *"ended by an administrator"* implied personal action and read as punitive for routine IT cleanup.
- **No widget animation** — the SPA navigation is direct.
- **Offline path** is invisible to the user; they'll see the standard login page next visit. No discoverability of the admin-terminate event from the user side; admins use the audit log for follow-up.
- **No demo storyboard hook** — admin-terminate is a defense narrative, not a UX moment. Demonstrated via IS Console + watching log lines.

### Binding_message after admin-terminate re-login (D3.4, FIX-17)

If the user re-logs in immediately after admin-terminate and triggers a new CIBA, the binding_message branches on `reason`:
- `user_signed_out` → *"New session — you signed out at HH:MM"*
- `admin_terminated` → *"New session — your previous session was ended"* (no false memory of "you signed out" — they didn't)
- `token_expired` → *"New session — your previous access expired"*

Shared logic in `common/auth/binding_messages.py`. The `reason` is propagated from the `/internal/events` payload through to the next CIBA initiation.

### Architecture (Stage 4)

- New endpoint: `POST /backchannel-logout` on the orchestrator.
- New SSE event type: `session_terminated` (in addition to existing `consent_required`, `tool_result`, `tool_error`, etc.).
- BCL receiver implementation requires: JWKS fetch + cache (Sprint 1 already has this in `common/auth/jwt_validator.py`), token-spec validation per OIDC BCL §2.6, idempotency on `jti` (handle duplicates).
- **Open question (Stage 4):** does IS reach the orchestrator's BCL endpoint over the docker-compose internal network (since IS is on AWS)? Probably **no** — IS at `13.60.190.47:9443` cannot reach `http://orchestrator:8090` directly. Need either (a) reuse the C12 reverse-SSH tunnel rig (now permanent), or (b) document admin-terminate as requiring the rig to be up. Recommendation: reuse the tunnel rig — it's already in `docker-compose.yml` under profile `spike-bcl` and has a stable URL. Stage 4 to lock the deployment shape.

### Testing (Stages 7–8)

- **R-LOGOUT-with-admin-trigger:** the same R-LOGOUT-1..5 + R-LOGOUT-8 acceptance criteria, with the trigger being an admin terminate via IS Console (manual) or a forged-but-valid logout_token POST (automated test).
- **3B.1 verification (manual):** Day 1 of 3B — bring up the C12 rig, set `back_channel_logout_uri` on `orchestrator-mcp-client`, sign in as `employee_user`, terminate session via IS Console, observe `bcl_received` in orchestrator logs. Result documented as F-20 PASS or FAIL in `sprint-1-fixes.md`.
- **EX-3 negative test:** craft a logout_token with bad signature / expired iat / mismatched aud; assert orchestrator returns 400 and does NOT run the fan-out.

## Demo storyboard hook (Sprint 3 demo Act III — defense-in-depth)

> *"What if the user's laptop is stolen and they can't sign themselves out?"*
>
> Operator opens IS Console → User Management → `employee_user` → Active Sessions → Terminate.
>
> Trace panel on the user's still-running browser scrolls a `session_terminated` event. SPA navigates to login page.
>
> *"That's the kill switch. Same cascade as user-driven sign out, but triggered by the IdP. The orchestrator is the gateway whether the trigger is the user or the admin."*
