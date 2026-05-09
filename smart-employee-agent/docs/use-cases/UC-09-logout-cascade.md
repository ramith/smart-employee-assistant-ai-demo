# UC-09 — User signs out (logout cascade)

**Sprint:** 3 (Stage 2 deliverable)
**Priority:** Critical (D3.1 — the signature Sprint 3 demo)
**Maps to R-tests:** R-LOGOUT-1, R-LOGOUT-2, R-LOGOUT-3, R-LOGOUT-4, R-LOGOUT-5, R-LOGOUT-6
**Maps to D3.1, D3.4** ([`milestone-plan.md`](../milestone-plan.md) §3 Sprint 3)
**Stage 1 decisions referenced:** Q3 (IS consent screen), Q4 (60 s introspection cache TTL), Q6 (4 fan-out receivers)

## Actors

- **Primary:** Authenticated user (`employee_user` or `hr_admin_user`).
- **Secondary:** SPA, Orchestrator, HR-AGENT, IT-AGENT, hr_server, it_server, WSO2 IS.

## Preconditions

- User completed UC-01 (Pattern C login). Session has at least one CIBA-issued OBO token cached on a specialist (i.e. user has done at least one tool action — UC-02, UC-03, or UC-07 — so a real cascade is observable).
- Orchestrator session map records `(session_id → [(agent_id, jti, exp, auth_req_id)…])` per S1.11.
- IS at `https://13.60.190.47:9443` is reachable.

## Trigger

User clicks the **Sign Out** button in the SPA top bar.

## Main flow (locked design — IS consent screen, 4-receiver fan-out, Stage 4 ordering)

**Stage 4 corrections applied:** ordering rewritten per BLOCK-F (cancel-before-fan-out), BLOCK-G (`Session.terminating` flag), and FIX-12 (per-`user_sub` lock serialises UC-09/UC-10 races). Spinner phases per FIX-14 + BLOCK-E. Asymmetry note per FIX-5.

1. SPA `performSignOut()` calls `POST /auth/logout` with `credentials: "include"` and the existing `X-Request-ID` header (CSRF defense per FIX-9 — server rejects 400 if absent). SPA shows phase-1 spinner: *"Revoking access for all agents…"* (copy-deck row 8.5).
2. Orchestrator handler acquires the **per-`user_sub` `asyncio.Lock`** (FIX-12). Concurrent UC-10 admin-terminate would block here.
3. Orchestrator sets `Session.terminating = True` as the **first state mutation** (BLOCK-G). Any in-flight chat/CIBA-initiate request that has already passed auth middleware will now be rejected with 401 by the new mutation guard.
4. Orchestrator snapshots the session: `token_a` (with `id_token`), list of `(agent_id, jti, exp)` triples (FIX-19 — `exp` is needed by receivers' sweepers), set of `pending_ciba` entries.
5. Orchestrator **cancels pending CIBAs first** (BLOCK-F): for each pending, `cancel_event.set()`, then `await asyncio.wait_for(pending.cancelled_ack.wait(), timeout=0.1)`. The poll loop sets `cancelled_ack` in its `finally`. The barrier ensures any in-flight CIBA completion observes the cancel before its jti exists.
6. Orchestrator calls `POST https://13.60.190.47:9443/oauth2/revoke` with `token=<token_a>`, basic-auth as `orchestrator-mcp-client`. Logs `token_a_revoked rid=… jti=…`.
7. Orchestrator fan-outs `POST /internal/events` (renamed per FIX-7; CAEP-friendly route) in parallel to **four receivers** (Q6) with **inline retry-once** @ 200 ms per leg (FIX-22 — retries are per-coroutine, not post-`gather`):
   - `http://hr-agent:8001/internal/events` body per tech-arch §3.2.
   - `http://it-agent:8002/internal/events`
   - `http://hr-server:8000/internal/events`
   - `http://it-server:8004/internal/events`

   **Asymmetry (FIX-5):** agent-side receivers' job is "drop cached token + ban jti" — defends against agent reusing its `_CachedToken` for a new MCP call. Server-side receivers' job is "ban jti at the validator" — defends against captured-token replay (R-LOGOUT-5). Both layers are required; they defend different threats.

   Multi-session loop (FIX-20): if the user has multiple `Session` entries (multi-browser), the orchestrator iterates and fan-outs once per (session, agent) — N×4 calls.
8. Orchestrator clears `orch_sid` cookie and **then** removes the in-memory `Session` entry (this is the LAST mutation per BLOCK-H ordering invariant). Releases the per-user_sub lock.
9. Orchestrator constructs the IS RP-initiated logout URL with bound `state` (NIT-5 — stored in a 60 s per-session map, validated when user returns to `/?reason=signed_out`) and returns JSON:
   ```json
   {
     "redirect_url": "https://13.60.190.47:9443/oidc/logout?id_token_hint=<token_a.id_token>&post_logout_redirect_uri=http://localhost:8090/?reason=signed_out&client_id=BO4LfSkkUOWnl7YgJNZcGiABW5ka&state=<csrf-nonce>"
   }
   ```
10. SPA reads `redirect_url` and shows the **phase-2 spinner** (BLOCK-E + FIX-14): *"Redirecting to complete sign-out at your identity provider…"* (copy-deck row 8.9). After ~200 ms, `window.location.href = redirect_url`.
11. Browser navigates to IS `/oidc/logout`. IS renders its **"Yes, sign me out"** confirmation page (Q3 lock — spec-pure RP-initiated logout).
12. User clicks **Confirm**.
13. IS clears its own user session and 302s to the registered `post_logout_redirect_uri` (= the orchestrator's `/?reason=signed_out`). Orchestrator validates the bound `state` (NIT-5) before rendering.
14. SPA loads `/`, sees `?reason=signed_out` with valid `state`, shows the login page with a banner: *"You have been signed out."* (copy-deck row 1.7; styled neutral, like the welcome banner.)

**Ordering invariants (Stage 4 lock):**
- Step 3 (`terminating = True`) precedes step 4 (snapshot).
- Step 5 (cancel + barrier) precedes step 7 (fan-out).
- Step 8 (Session removal) is the LAST mutation in the cascade.

**Total wall-clock budget:** steps 1–9 complete in ≤2 s (R-LOGOUT-1..4). Steps 10–14 are user-paced (consent-screen click).

## Exception flows

### EX-1 — Pending CIBA at logout (R14 / R-LOGOUT-6)

1. User has IT consent widget visible when they click Sign Out.
2. Step 6 of main flow: `cancel_event.set()` on the IT poll task.
3. The polling loop returns `CIBATimeoutError(reason="cancelled")`; no `ResultPayload` is written to state.
4. **Race sub-case (Q-LOGOUT-4 "ghost approval"):** if the user manages to click **Approve** at IS *after* logout begins but *before* IS revokes the auth_req_id (assuming C10 capability test PASS — see 3B.1), IS still completes the grant and emits a token. The orchestrator session is gone, the polling task is cancelled, the token has no consumer. If C10 PASS, orchestrator also calls IS to invalidate the `auth_req_id` on logout, eliminating this case. If C10 FAIL, the "ghost token" can be presented directly to the MCP server — denylist (added in step 5 of main flow) returns 401 `ERR-MCP-002`.
5. Postcondition: no successful tool call after logout.

### EX-2 — Half fan-out failure (R15 / R-LOGOUT-7)

1. Step 5 of main flow: fan-out to IT-AGENT returns 5xx.
2. Orchestrator retries once with 200 ms back-off. If still 5xx, log `WARN logout_fanout_partial jti=… target=it-agent` and proceed.
3. The IT-SERVER fan-out (also in step 5) succeeded → IT-SERVER's denylist already has the jti.
4. If both IT-AGENT *and* IT-SERVER fan-outs failed (correlated): on the next MCP call attempt with token-C, IT-SERVER's introspection cache (60 s TTL) will detect `active=false` (since token-A was revoked at IS — see step 4 of main flow; introspection by jti also returns inactive once the parent grant is gone) and return 401 within ≤60 s.
5. R-LOGOUT-7 acceptance: IT-SERVER returns 401 within one introspection window of the failed fan-out.

### EX-3 — Captured token replay after logout (R-LOGOUT-5)

1. Attacker captured token-B from logs / network before logout.
2. After logout completes, attacker presents `Authorization: Bearer <token-B>` directly to `hr_server`.
3. `hr_server`'s validator checks the in-process denylist first (step 5 added the jti).
4. Returns 401 with body `{"error_id": "ERR-MCP-002", "detail": "token revoked"}`.
5. If denylist had been wiped by a process restart (cold cache), introspection (60 s positive cache, hard-fail on `active=false`) catches the same condition since the parent grant is dead at IS.

### EX-4 — Orchestrator crashes mid-flow (R-1 from Stage 1 §3)

1. Orchestrator restarts between step 6 (token-A revoked at IS) and step 7 (fan-out).
2. Fan-out never happens; agents and servers don't have the jti in their denylists.
3. Backstop: introspection on the next MCP call detects `active=false` (token-A revoked at IS implies child grants are also dead) — **but this depends on F-21 / C13 outcome (BLOCK-A)**. F-21 PASS → backstop holds within ≤60 s. F-21 FAIL → captured token-B remains valid up to TTL (1 h); demo runbook surfaces this as **SECURITY-DEGRADED**.
4. Document this in the demo runbook as a known limitation; Sprint 4+ roadmap = persistent denylist (Redis).

### EX-6 — Orchestrator unreachable at sign-out click (FIX-16)

1. SPA `POST /auth/logout` times out (10 s client-side fetch timeout) or returns 5xx.
2. SPA renders error banner (copy-deck row 8.10): *"Sign-out could not be completed right now. Close your browser to end your session, or try again."*
3. SPA clears the local cookie regardless (best-effort; the orchestrator session may or may not survive depending on where the failure occurred).
4. Acceptable degradation; the user has a real recovery path (close browser).

### EX-5 — User cancels at IS consent screen (BLOCK-E rewritten)

1. Steps 1–10 complete normally. Browser is at IS `/oidc/logout`.
2. User clicks **Cancel** (or closes tab) instead of **Confirm**.
3. IS does NOT clear its own session. IS does NOT redirect.
4. **Orchestrator state is already cleaned** (step 7, agents cache-busted; step 5, pending CIBAs cancelled; step 8, cookie + Session removed). The user is already signed out of *this application*.
5. From the user's perspective: they are signed out (SPA cookie is gone; reload returns to login). From IS's perspective, the user has an active SSO session that could be reused if they hit IS for any other federated SP.
6. **UX path:** if the user navigates back to the SPA (e.g., reloads the tab), the SPA detects `?reason=signed_out_partial` (set by the orchestrator on the cookie-cleared response when no IS-redirect-confirmation was observed within a 60 s window — orchestrator emits this label by tracking `state`-not-validated returns), and shows the login page with a **distinct banner**:

   > *"You have been signed out of this application. Note: your sign-in at the identity provider may still be active. To fully sign out everywhere, visit your organization's sign-out page or close your browser."*
   > (copy-deck row 1.13)

   This is honest about the partial state and gives a real recovery path. **Important for shared/kiosk machines**: the next user can SSO into other federated SPs as the previous user until IS's session expires.
7. Audit: the orchestrator logs `signed_out_partial state=<nonce> reason=is_consent_skipped` for the rid; admin can grep this if a user reports unexpected behaviour.

## Postconditions

- **Success:**
  - `orch_sid` cookie absent in browser.
  - `Session` entry removed from orchestrator's `session_store`.
  - `_CachedToken` for the user dropped from HR-AGENT and IT-AGENT.
  - jti present in denylists of HR-AGENT, IT-AGENT, hr_server, it_server.
  - `/oauth2/introspect` for the captured token-A returns `active=false` at IS.
  - All `pending_ciba` for the session show `cancel_event.is_set() == True`.
  - IS user-session cleared (after step 11) — IS's audit log records `Logout` for the orchestrator app.
- **Partial-success acceptable:** EX-2, EX-4 — defended by introspection backstop within 60 s.
- **Failure (CRITICAL):** any captured token-B/token-C remains usable >60 s post-logout. Triggers Sprint 3 stop and incident review.

## Design notes for downstream stages

### UX (Stage 3 — wireframes; Stage 4 amendments applied)

- Before click: "Sign Out" button is in the top bar of the SPA, right of the user name. Same position as today.
- During cascade (≤2 s): SPA shows phase-1 inline spinner with text *"Revoking access for all agents…"* (FIX-14, copy-deck 8.5). Trace panel (introduced in Sprint 2.4) records the cascade — but **trace panel visibility is gated on `DEMO_MODE=true`** (NIT-7). In production mode the trace panel is hidden during sign-out so end-users don't see scrolling internal logs.
- Pre-redirect (BLOCK-E + FIX-14): SPA transitions to phase-2 spinner *"Redirecting to complete sign-out at your identity provider…"* (copy-deck 8.9) for ~200 ms before `window.location.href`. Mirrors the established Scenario A "you will be redirected to your identity provider" pattern.
- IS consent screen: rendered by IS, out of our control. User clicks **Confirm**.
- After IS redirects: SPA shows the login page with banner *"You have been signed out."* (copy-deck 1.12; styled neutral, like the existing welcome banner).
- **Cancel-at-IS path (EX-5):** banner is *"You have been signed out of this application…"* (copy-deck 1.13; distinct styling — informational, not error).
- **Error path (EX-6):** banner *"Sign-out could not be completed right now…"* (copy-deck 8.10; styled amber/warning).
- Accessibility (NIT-9): phase-1 spinner uses `aria-live=polite` (copy-deck 10.14). Login-page banners on arrival use `aria-live=assertive` (copy-deck 10.15). No programmatic focus-stealing.

### Architecture (Stage 4 — sequence + components)

- See `docs/architecture/sprint-3-tech-arch.md` §1 for the full sequence diagram.
- New endpoints:
  - `POST /auth/logout` (orchestrator) — body modified per locked design.
  - `POST /internal/revoke` (× 4: HR-AGENT, IT-AGENT, hr_server, it_server).
- Auth method for `/internal/revoke`: shared secret in env var (`INTERNAL_REVOKE_SHARED_SECRET`), bound to docker-compose internal network. **Stage 4 to confirm** vs alternatives (mTLS, loopback-trust).
- `_CachedToken` gains a secondary jti index for O(1) lookup on revoke.
- `pending_ciba.cancel_event` already exists; logout becomes a second call-site.

### Testing (Stages 7–8)

- **R-LOGOUT-1:** post-`POST /auth/logout`, `GET /api/chat` with the cleared cookie returns 401 within 1 s. Mocked-IS layer.
- **R-LOGOUT-2:** mock `/oauth2/introspect` returns `active=false` for token-A within 2 s of logout.
- **R-LOGOUT-3, R-LOGOUT-4:** assert `internal_revoke_received` in agent logs within 2 s.
- **R-LOGOUT-5:** direct token-B presentation to hr_server returns 401 `ERR-MCP-002` (denylist).
- **R-LOGOUT-6:** logout during active consent widget produces `CIBATimeoutError(reason="cancelled")`.
- **Manual (live IS):** sign in as `employee_user`, run UC-02, click Sign Out, observe trace panel. Capture rid, run `tools/grep-trace.sh <rid>`, confirm full chain.

## Demo storyboard hook (Sprint 3 demo Act II)

**Stage 4 (NIT-9) revision:** lead with Salesloft Drift framing (concrete, named breach) rather than internal spike IDs.

> *"Watch what happens when I click Sign Out."*
>
> Browser shows the phase-1 spinner *"Revoking access for all agents…"* for ~1 second. Trace panel (demo mode) scrolls: `token_a_revoked`, `internal_event_sent → hr-agent`, `internal_event_sent → it-agent`, `internal_event_sent → hr-server`, `internal_event_sent → it-server`. Phase-2 spinner *"Redirecting to complete sign-out…"* for ~200 ms. Then the IS consent screen.
>
> *"And here's the receipt: I captured an HR token before signing out. Let me try to use it now."*
>
> Operator pastes a captured token-B as `Authorization: Bearer …` to hr_server with `curl`. Returns 401 `ERR-MCP-002`.
>
> *"This is the same failure pattern as the Salesloft Drift breach last year — agent OAuth tokens that couldn't be killed after the session ended. We proved on our own WSO2 stack that the standard back-channel logout would never have reached these agents. Our orchestrator owns the kill switch because the protocol can't."*

(Technical reviewers: full F-19 spike evidence in [`docs/architecture/sprint-1-fixes.md`](../architecture/sprint-1-fixes.md) §F-19.)
