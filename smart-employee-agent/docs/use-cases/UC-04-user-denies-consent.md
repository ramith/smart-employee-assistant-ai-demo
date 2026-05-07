# UC-04 — User denies a CIBA consent mid-flow

**Sprint:** 2 (initial Sprint 1 builds the path; Sprint 2 polishes the messaging)
**Priority:** High
**Maps to N-tests:** N12 (single deny), N18 (mid-flow deny — HR ok, IT denied), N19 (all denied)
**Maps to scenarios:** [user-experience.md](../user-experience.md) Scenario B-1

## Actors
- Same as UC-02/UC-03.

## Preconditions
- Same as UC-02/UC-03.

## Trigger
At any point during a UC-02 or UC-03 flow, the user clicks **Deny** on a Consent Widget — either in the SPA card or on the IS consent screen itself.

## Main flow

### Variant A — User denies in the IS consent screen (most common)
1. UC-02/UC-03 reaches step 11 (Consent Widget rendered) and step 12 (user opens auth_url).
2. On the IS consent page, user clicks **Deny**.
3. IS records the denial, redirects browser to a "consent denied" page, closes the popup tab.
4. Specialist's polling loop hits `<IS>/oauth2/token` → response `{error: "access_denied"}`.
5. Specialist returns A2A response: `{type: "error", reason: "user_denied_consent", agent_label, request_id}`.
6. Orchestrator's LLM is informed via tool error: *"the agent's authorization was denied by the user."*
7. Orchestrator continues with the rest of the planned routing (if any) — see EX-1, EX-2, EX-3 below for combinations.

### Variant B — User clicks Deny on the SPA Consent Widget directly (without going to IS)
1. SPA emits `POST <orch>/api/ciba/cancel` with the auth_req_id.
2. Orchestrator forwards a cancellation signal to the specialist (in-process, by auth_req_id).
3. Specialist stops polling, signals IS to invalidate the auth_req_id (best-effort — IS may not expose this primitive on 7.2; document in Sprint 3 capability test C10).
4. Same downstream steps as Variant A from step 5.

## Exception flows

### EX-1 — Single-specialist query, user denies (UC-02 base)
- Orchestrator's reply: *"I couldn't access HR information for you (you declined the authorization). Try again or rephrase if you'd like to retry."*
- N12 verifies.

### EX-2 — Two-specialist serial, user denies the FIRST (HR)
- Orchestrator skips IT? **Decision:** YES, skip — if user denied the very first, it's likely they want the whole request cancelled.
- Reply: *"Request cancelled — you declined HR access. Ask again if you'd like to retry."*
- (Alternatively: orchestrator could continue with IT only. **Default for Sprint 1 = abort the whole request** to avoid surprising the user.)
- N19 verifies.

### EX-3 — Two-specialist serial, user denies the SECOND (IT) after approving HR
- HR's answer is already rendered in the chat (the HR consent + call already completed).
- Orchestrator's final composition includes the HR data + a graceful note:
  - *"You have 12 days of leave. I couldn't pull asset info — you declined IT access."*
- N18 verifies.

### EX-4 — Specialist's polling loop is interrupted by the cancel from Variant B
- The polling loop must handle being cancelled cleanly: cancel the asyncio task, free resources, log the cancellation. No zombie polls.

### EX-5 — User denies in IS consent screen but specialist's poll loop hasn't picked up yet
- User sees the SPA widget still showing "Verifying with your identity provider…"
- Within `interval` seconds (≤2s default), polling lands on `access_denied` and the widget transitions to **DENIED** state.
- Maximum lag visible to user = `interval` seconds (typically 2s).

## Postconditions
- **Success (i.e., denial cleanly handled):** no token issued for the denied agent; orchestrator session map has no record for that agent in this request; chat shows graceful explanation; user can ask again.
- **Failure (denial not handled):** zombie polling, stack trace shown to user, or token mistakenly cached. **All MUST be tested against in N-tests.**

## Design notes for downstream stages

### UX (Stage 3)
- **Widget DENIED state** (per [`consent-widget-spec.md`](../consent-widget-spec.md) §3): card collapses to a transcript line with `⊘ Declined — HR Agent will not run for this request`.
- **Chat copy:**
  - Single deny: *"I couldn't access HR information (declined). Ask again if you'd like to retry."*
  - Mid-flow deny (HR ok, IT denied): *"You have 12 days of leave. I couldn't pull asset info — you declined IT access."*
  - All deny: *"Request cancelled — you declined the requested access."*
- **NO modal / no popup** for denial confirmation. The widget's DENIED state IS the feedback.

### Architecture (Stage 4)
- A2A response shape needs a discriminated union: `{type: "result" | "consent_required" | "error", ...}`.
- Specialist's poll-loop cancellation must be cooperative (asyncio task) and idempotent.
- LLM-facing tool-call result for a denied auth: structured error that the LLM can incorporate gracefully ("the agent could not be authorized").
- **Sprint 1 default for EX-2:** abort whole request on first denial. Document this; revisit in Sprint 2 if stakeholder feedback says otherwise.

### Testing (Stages 7–8)
- **N12, N18, N19 automated:** scripts that simulate IS returning `access_denied` to a polling specialist; assert chat copy + session map state.
- **Manual:** during demo rehearsal, deliberately deny once on widget #1, once on widget #2, and once on both. Verify chat copy reads naturally on stage.
