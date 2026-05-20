# UC-08 — Employee requests a scope beyond their role (denied at IS)

**Sprint:** 2 (the doc lands now; the polished denial-UX is Sprint 2's signature work)
**Priority:** High (signature security demonstration; shows role enforcement is at the IdP, not the app)
**Maps to N-tests:** N30, N31
**Maps to scenarios:** [`user-experience.md`](../user-experience.md) §7.4 (permission missing)

## Actors
- **Primary:** Employee user (`probe.user` in dev)
- **Secondary:** SPA, Orchestrator, HR Agent (or IT Agent), WSO2 IS

## Preconditions
- UC-01 succeeded with `probe.user` (Employee role — has only `hr_basic_rest`, `hr_self_rest`, `it_assets_read_rest`).
- User asks a query that the LLM routes to a tool requiring `hr_approve_rest` or `it_assets_write_rest`.

## Trigger
- **Variant A:** `probe.user` types *"Approve the leave request for Bob."*
- **Variant B:** `probe.user` types *"Issue a laptop to Carol."*

## Main flow (Variant A — Employee asks to approve leave)
1. Orchestrator routes to HR Agent, tool `approve_leave_request`.
2. HR Agent initiates CIBA with `scope=openid hr_approve_rest`, `login_hint=probe.user UUID`.
3. IS evaluates: `probe.user` has Employee role; `hr_approve_rest` is not in Employee's granted scopes.
4. IS returns `invalid_scope` (or `access_denied` if IS enforces at consent screen rather than at initiation) on `/oauth2/ciba`.
5. HR Agent catches the error; classifies as `ERR-CIBA-003`.
6. HR Agent returns A2A error to orchestrator.
7. Orchestrator's LLM composes from copy-deck §7.17: *"I don't have permission to look up HR information for you. If that seems wrong, contact your administrator."*
8. SPA widget transitions to ERROR state (copy-deck §5.24 ERR-CIBA-003 body).

## Exception flows

### EX-1 — IS enforces at consent screen (not at initiation)
1. `/oauth2/ciba` returns `{auth_req_id, auth_url}` (scope accepted because the agent's OAuth App is subscribed to the scope).
2. Consent screen renders but IS's role check denies the scope during user interaction — IS returns `access_denied` on the CIBA poll.
3. Downstream: ERR-CIBA-005 path (same as UC-04 EX-1).
4. Orchestrator: *"I couldn't access HR information for you (you declined the authorization)."*

> **Note:** both behaviours (initiation-time vs. consent-time denial) must be documented; the **N30/N31 tests verify EITHER satisfies the security property** (token with `hr_approve_rest` never issued to `probe.user`).

### EX-2 — LLM does not route to the approve tool for a plain employee
- If the LLM / keyword router correctly declines to route `probe.user` to `approve_leave_request` at the orchestrator level, the IS check is never reached. This is an acceptable defence-in-depth outcome.
- The N30/N31 tests **bypass the LLM** and call CIBA directly to verify the IS layer independently.

## Postconditions
- **Success (denial cleanly handled):** no `hr_approve_rest` or `it_assets_write_rest` token ever issued to `probe.user`; orchestrator session map has no record for the denied tool call; chat shows the §7.17 copy.
- **Failure:** token issued despite role check — **CRITICAL security bug**; triggers immediate sprint stop and IS configuration audit.

## Design notes for downstream stages

### UX (Stage 3)
- Copy §7.17 is the chat surface message. No widget is shown if denial occurs at CIBA initiation (no auth_url was returned; the orchestrator returns a chat message directly).
- If denial occurs at consent screen (EX-1): widget shows DENIED state (§5.16).
- The two paths look different to the user. Stage 3 must decide which IS enforces in 7.2 (empirical Sprint 2 probe needed — see Architecture below).

### Architecture (Stage 4)
- HR Agent and IT Agent must distinguish `ERR-CIBA-003` (IS rejected scope at initiation) from `ERR-CIBA-005` (user denied at consent). Different log lines, different widget paths.
- **Sprint 2 probe (`c11_role_denial.py`):** call `/oauth2/ciba` with `scope=hr_approve_rest` as `probe.user`; log IS's response to confirm which enforcement path IS 7.2 uses. Document the finding in `docs/spikes/`.

### Testing (Stages 7–8)
- **N30** (Employee → `hr_approve_rest` denied): `probe.user` triggers a CIBA flow requesting `scope=openid hr_approve_rest`. IS must deny. Acceptance: `ERR-CIBA-003` emitted, hr_server never reached.
- **N31** (Employee → `it_assets_write_rest` denied): same pattern with `it_assets_write_rest`. Acceptance: ERR-CIBA-003 emitted, it_server never reached.
- These are Sprint 2 automated tests (mocked-IdP layer simulating IS's `invalid_scope` or `access_denied`).
- **Manual:** sign in as `probe.user`; attempt "Approve Bob's leave" in chat; confirm no approval token appears in IS audit log.

## Demo storyboard hook (Sprint 1 Act II — optional narration)

While UC-08 is a **Sprint 2 build** for full polish, the SECURITY NARRATIVE can be demonstrated verbally during the Sprint 1 demo's optional "Act II":

> *"What if I, as an employee, ask the system to approve someone's leave?"*
>
> — runs the variant query as `probe.user`, audience watches the orchestrator say "I don't have permission." 
>
> *"Notice: the IdP rejected the request, not the app. Even if I rewrote the agent code to bypass the chat router, my IdP-issued token would never carry the scope. That's identity-first governance — the authority to approve is tied to who I am, not what code I run."*

This is the strongest single security talking point in the demo.
