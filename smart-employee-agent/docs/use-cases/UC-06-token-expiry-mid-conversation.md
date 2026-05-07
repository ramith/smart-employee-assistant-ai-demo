# UC-06 — Token expiry mid-conversation (re-CIBA with Session Refresh treatment)

**Sprint:** 2 (Sprint 1 lays the cache + expiry detection; Sprint 2 implements the polished re-CIBA UX)
**Priority:** Medium (occurs only after 1+ hour of activity; demo-time risk is low)
**Maps to N-tests:** D2.5 in milestone-plan; new N29 to be added in Sprint 2 backlog
**Maps to scenarios:** [user-experience.md](../user-experience.md) §7.3

## Actors
- Same as UC-02.

## Preconditions
- A specialist has previously issued an OBO token for the user (e.g., HR's token-B from a UC-02 ~1 hour ago).
- The user's session with the orchestrator is still active.
- The user issues a new request that needs the same specialist.
- Token-B's TTL has expired (default 3600s).

## Trigger
User asks: *"What's my leave balance?"* — for the second time, more than 60 minutes after the first.

## Main flow
1. UC-02 main flow restarts. Orchestrator's LLM routes to HR Agent.
2. HR Agent receives the A2A request, validates token-A (orchestrator's session token, still alive — it has its own 3600s TTL but assume it's been refreshed via a fresh login OR is still within window).
3. HR Agent's session-state cache check: *"do I have a valid token-B for `(user_sub, hr.read scope set)`?"*
4. Cache lookup returns: token-B exists, but `exp < now`. **Expired.**
5. HR Agent must mint a fresh OBO. This means a fresh CIBA flow.
6. HR Agent calls `POST <IS>/oauth2/ciba` with the same shape as UC-02 step 7, but with a different `binding_message`:
   - **Standard binding_message** (UC-02): *"HR Agent wants to view your leave balance for request abc-1234"*
   - **Session refresh binding_message** (UC-06): *"HR Agent's previous access has expired — re-approve to view your leave balance for request abc-9876"*
7. IS returns a new `{auth_req_id, auth_url}`.
8. HR Agent returns A2A response with a new field: `{type: "consent_required", auth_url, ..., is_refresh: true, prior_consent_at: <timestamp of token-B's iat>}`.
9. Orchestrator forwards to SPA via SSE: `{type: "ciba_url", is_refresh: true, prior_consent_at, ...}`.
10. SPA renders the **Session Refresh variant** of the Consent Widget — see [`consent-widget-spec.md`](../consent-widget-spec.md) §4. Distinct visual treatment (amber banner, "You approved this 1h 12m ago"), button labeled **Re-approve** instead of **Approve**.
11. User clicks Re-approve. The IS consent screen appears as usual; user confirms.
12. From here, identical to UC-02 steps 13–18. New token-B' is issued and used.

## Exception flows

### EX-1 — User clicks Skip on the Session Refresh widget
1. SPA `POST /api/ciba/cancel`.
2. Same downstream as UC-04 Variant B.
3. Orchestrator returns: *"Request cancelled — your access expired and you chose not to refresh."*

### EX-2 — Token-A (orchestrator session token) has also expired
1. Step 2 fails — HR Agent rejects token-A as expired.
2. Orchestrator detects this and surfaces: *"Your session has expired. Please sign in again."*
3. SPA redirects to sign-in.
4. **NOT a re-CIBA case** — it's a full re-login (UC-01).

### EX-3 — User never confirmed the original consent (token-B never existed)
- This isn't really UC-06; it's UC-02. The cache lookup at step 4 returns "no record" not "expired."
- Standard UC-02 main flow applies; UC-06 doesn't trigger.

## Postconditions
- **Success:** new token-B' replaces token-B in the session map; user sees their answer in chat as if nothing happened (other than the brief Session Refresh widget); orchestrator logs note the refresh event with `prior_jti -> new_jti` correlation.
- **Skip:** no new token; chat shows "request cancelled" graceful message; user can re-issue the request later.

## Design notes for downstream stages

### UX (Stage 3) — DEPENDS ON consent-widget-spec.md §4
- **Distinct from a fresh consent.** Amber banner color (not teal/purple).
- Copy: *"HR Agent's previous access has expired — re-approve to continue."*
- Show prior consent timestamp: *"You approved this 1h 12m ago"* — humanized relative time.
- Button labels: **Re-approve** (primary) and **Skip** (secondary).
- **DO NOT** show this widget if the user has never approved this specialist before in the current session — fall back to the standard fresh widget.

### Architecture (Stage 4)
- **Session map needs `iat` field per token entry** so the SPA widget can compute prior-consent age.
- HR Agent (and IT Agent) maintain a per-`(user_sub, scope_set)` token cache with TTL awareness.
- **Triggering condition for UC-06:** cache hit but `exp - now < buffer` (where buffer = 30s, the same buffer used in `_archive/agent.before-v3/agent_auth.py`).
- **Per Q3+T8 decision: refuse `offline_access` scope on CIBA initiation.** No refresh tokens; every UC-06 trigger requires a real CIBA round-trip with user click.

### Testing (Stages 7–8)
- **N29 (new) automated:** set the test app's token TTL to 60s; run UC-02; wait 70s; submit the same query; assert the Session Refresh widget appears and re-CIBA completes successfully.
- **Manual:** for demo prep, NOT in the canonical demo (60–90s budget doesn't accommodate a 1-hour wait). Demonstrated separately in extended scenarios or in Sprint 2 video.
