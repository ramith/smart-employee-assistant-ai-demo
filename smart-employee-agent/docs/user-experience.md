# User Experience & Data-Flow Validation Spec

**Purpose:** plain-language description of what the user sees and what happens behind the scenes, written so the implementation can be validated against it. Every observable behavior here is something a tester or demo audience can check without reading code.

**How to use this doc:** when reviewing a PR, demo, or test report, walk through the scenarios below. If any "Expected" line is wrong, the implementation is wrong (or this doc is wrong — fix whichever is out of sync first). Acceptance-criteria lines map to the negative-test IDs in [milestone-plan.md](milestone-plan.md) §3.4-H and §4.4-G.

**Scope:** Sprint 1 (Agent-to-Agent) and Sprint 2 (Secure Session Termination). UAE Pass and WSO2 API Manager are out of scope; the doc says so explicitly where relevant.

---

## 1. Cast of characters

| Name | What it is | Where it runs |
|---|---|---|
| **You** (the employee) | The human user, in a browser. Has a role like `employee` or `hr_admin`. | Browser SPA at `client/` |
| **Asgardeo** | The identity provider. Issues credentials, runs the consent screen, can revoke sessions. | WSO2 cloud (SaaS) |
| **Orchestrator** | The chat receptionist. Talks to the user, decides which specialist to ask. Has an LLM brain (Gemini). | `orchestrator/` |
| **HR Agent** | A specialist that knows HR things (leave, time-off, employee info). | `hr-agent/` |
| **IT Asset Agent** | A specialist that knows what hardware each employee has. | `it-agent/` |
| **HR backend** | Existing record system behind HR Agent. | `hr-server/` |

When this doc says "the system," it means the whole thing collectively.

---

## 2. Scenario A — First sign-in (the consent moment)

### What you do
1. Open the app. Click **Sign in**.
2. You land on Asgardeo's familiar login page. Enter your username + password.

### What you see next
3. **A consent screen** appears. It says something like:
   > "**Orchestrator Agent** wants to act on your behalf for HR and IT operations. Approve?"
   With buttons **Approve** and **Deny**.

### What happens if you click Approve
4. You're redirected back to the chat app. You see your name in the header. The chat is empty and ready.

### What happens if you click Deny
5. You're redirected back to the chat app's sign-in page with a **clear, friendly error message** ("You did not approve the delegation. Please try again or contact admin."). No chat session is created. No silent fallthrough.

### Behind the scenes (in plain words)
- Asgardeo issued the Orchestrator a **delegated badge** that names you (the principal) and the Orchestrator (the acting party).
- The badge is scoped to *what you consented to* — nothing more.
- If you denied, no badge was issued and nothing in the system has any record that you were "almost authenticated."

### Acceptance criteria
- ✅ A consent screen is visible — not bypassed.
- ✅ Denying produces a user-friendly error, not a crash or a half-logged-in state.
- ✅ Approving creates exactly one session record on the Orchestrator side.
- Maps to negative tests: **N12** (user denial), **N14** (Asgardeo policy denial — see §6.3 below).

---

## 3. Scenario B — The demo query (leave + equipment)

This is the headline scenario.

### What you do
1. In chat, type:
   > "Approve John's leave request LR001 — but first, has he returned his equipment?"
2. Press Enter.

### What you see
3. Within a few seconds, the Orchestrator replies. Something like:
   > "John (employee 1042) has 1 outstanding asset: laptop #12345. I've approved leave LR001 conditional on its return."
4. Optionally, the chat shows what specialists were consulted (this is a UX nice-to-have, not required for validation).

### Behind the scenes (in plain words)
- The Orchestrator's brain (LLM) reads your message and decides it needs **two specialists**: HR (for leave) and IT (for equipment).
- The Orchestrator goes back to Asgardeo **twice**, asking for two narrow badges — one that works only at HR, one that works only at IT. Both badges carry your name *and* the Orchestrator's name.
- The Orchestrator hands the HR badge to HR Agent and asks about LR001. HR Agent verifies the badge (right counter, right scope, right names), looks up the leave, and replies.
- In parallel, the Orchestrator hands the IT badge to IT Asset Agent and asks about John's assets. IT Agent verifies and replies.
- The Orchestrator combines both answers into a single sentence for you.
- If HR Agent needs to look anything up in the HR backend, it gets *another* narrow badge — for the backend specifically — with three names on it now (you, Orchestrator, HR Agent).

### What you do NOT see (and shouldn't)
- The badges themselves (tokens). Never visible to you, never logged to your browser console, never in the chat transcript.
- The internal URLs of HR Agent or IT Asset Agent. The chat UI does not need to expose them.
- Stack traces or implementation errors when something goes wrong (you should see a friendly explanation; engineers see details in logs).

### Acceptance criteria
- ✅ The reply correctly combines leave info and equipment info — not just one of them.
- ✅ Two distinct badges are minted (one per specialist), verifiable in Asgardeo's audit log.
- ✅ Each badge has the correct **counter restriction** (HR badge cannot open IT's door — see Scenario D-1).
- ✅ Each badge carries both your identity and the Orchestrator's, all the way through to the HR backend.
- ✅ No bearer tokens or JWT-shaped strings appear anywhere in the chat UI, browser network tab body, console, or trace export (per N13 leak scan).
- Maps to: **N1, N2, N3, N5, N7, N13, N15**.

---

## 4. Scenario C — Sign-out (the revocation moment)

### What you do
1. Click **Sign out** in the chat header.

### What you see
2. You're redirected to Asgardeo's sign-out page, then back to the app's sign-in page. Within a second.
3. If you (or someone using the same browser) click "Sign in" again, you go through the **full** flow again — Asgardeo login + consent screen. There is no "remember me" shortcut that bypasses consent.

### Behind the scenes (in plain words)
- Two things happen in parallel:
  - Your local session in the Orchestrator (chat history, current badges) is wiped.
  - Asgardeo notifies the Orchestrator over a **back channel** (server-to-server, not via your browser) that your session is dead.
- The Orchestrator immediately tells HR Agent and IT Asset Agent: *"Forget any badges you've seen for this user."*
- Each specialist also independently checks every badge with Asgardeo on every request — so even if the back-channel notification is delayed, badges go stale within ~2 seconds of revocation.

### What should fail after sign-out
- Re-using a previously-issued badge (e.g., copied from network capture) at HR Agent or IT Asset Agent → **rejected within 5 seconds**.
- Re-using a previously-issued badge at the HR backend → **rejected within 5 seconds**.
- Any in-flight chat that was streaming a response when you signed out → **stream is cancelled cleanly**; no further specialist calls happen after the sign-out.

### Acceptance criteria
- ✅ Sign-out completes within ~1 second of click (the user-visible part).
- ✅ Replays of stale badges fail at every specialist within 5 seconds (per R1, R2).
- ✅ An admin terminating your session in Asgardeo's console has the same effect as you signing out yourself (per R3).
- ✅ A streaming chat in flight at sign-out time is cancelled before any further specialist call (per R6 enhancement).
- Maps to: **R1, R2, R3, R6, R9**.

---

## 5. Scenario D — Adversarial flows (what should NOT happen)

These are observable from a tester's perspective. Each one corresponds to a failure case the system must demonstrate it handles correctly.

### D-1 — A stolen HR badge cannot be used at IT
**Setup:** capture a badge the Orchestrator minted for HR Agent (e.g., from server logs in a test environment).
**Action:** present it to IT Asset Agent.
**Expected:** IT rejects it with a clear "wrong audience" error. Maps to **N7**.

### D-2 — Forwarding the user's session badge directly to a specialist
**Setup:** the Orchestrator is supposed to mint a fresh, narrow badge per specialist. What if it forwards the user's broad delegated badge instead?
**Expected:** the specialist rejects the broad badge — its audience is "Orchestrator," not the specialist. Maps to **N1**.

### D-3 — A rogue server pretending to be IT Agent
**Setup:** stand up a malicious agent-card server at a different URL claiming to be IT Agent.
**Expected:** the Orchestrator does not load it. Only URLs in the configured allowlist are even considered. Maps to **N8**.

### D-4 — A real card, but with a tampered "issuer"
**Setup:** an allowlisted specialist's agent card has been doctored to point JWKS to an attacker-controlled key.
**Expected:** the Orchestrator **ignores** the card's `issuer` field — it only ever uses the issuer hardcoded in its validator. Card is logged + refused. Maps to **N8b**.

### D-5 — Specialist is down mid-conversation
**Setup:** during the demo query, IT Asset Agent crashes or returns 503.
**Expected:** the user sees a friendly "I couldn't reach the IT system right now, here's the leave info I do have" — not a stack trace, not a hang. Maps to **N9b**.

### D-6 — LLM hallucinates a non-existent skill
**Setup:** the Orchestrator's LLM tries to call a skill that doesn't exist on any specialist.
**Expected:** the user does NOT see the failure as a crash. The Orchestrator returns a structured "I don't know how to do that" to its own LLM, the LLM recovers, and the user sees a graceful natural-language answer. Maps to **N15**.

### D-7 — Hijack via the back-channel logout endpoint
**Setup:** while signed in as User-B, an attacker POSTs a valid logout-token for User-A's session to the Orchestrator's `/auth/backchannel-logout`.
**Expected:** User-A's session is terminated (legitimate per spec). User-B's session is **completely unaffected** — the endpoint never inspects the caller's identity to decide whose session to kill, only the validated token's contents. Maps to **R7**.

### D-8 — Replaying a logout-token
**Setup:** capture a real, valid logout-token. Try to POST it twice.
**Expected:** the second POST is rejected (single-use enforcement). Maps to **R4**.

### D-9 — A stranger forging a logout-token
**Setup:** sign your own JWT and POST it to the back-channel logout endpoint.
**Expected:** rejected with a signature-validation error. Maps to **R5**.

---

## 6. UX-level edge cases

### 6.1 First-time consent vs. returning user
- **First time:** consent screen always shown (Asgardeo decision; we don't suppress it).
- **Returning user with active Asgardeo session:** depends on Asgardeo's `prompt=consent` policy. For the POC, consent should appear at every fresh app session, not just the first ever. Document the actual behavior in `docs/asgardeo-setup.md` once verified.

### 6.2 What happens if Asgardeo is down
- During sign-in: Asgardeo is the front door; if it's down, you can't sign in. Expected — the SPA shows an error.
- After sign-in, mid-conversation: the Orchestrator can still hold your session, but it cannot mint new specialist badges. The user sees a "service is degraded" message. Expected behavior; not a security failure.
- During sign-out: the Orchestrator falls back to local session termination only. Specialists' introspection cache TTL (2 s) means stale badges expire quickly anyway. Document as a known degradation in `docs/production-hardening.md`.

### 6.3 The user has the role but the orchestrator policy doesn't permit them
- Asgardeo's application policy controls which actors are allowed for which apps. If the policy rejects the `requested_actor` parameter (e.g., misconfigured), the user sees a "configuration error, contact admin" message at sign-in. Maps to **N14**.

### 6.4 The user lacks a needed scope
- If the user's role doesn't grant `it_assets_read_mcp` and they ask about equipment, the Orchestrator does NOT silently skip — it tells the user "I don't have permission to look up assets for you." Maps to **N5**.

---

## 7. Things the user experience should NEVER include

These are phrased as red lines for reviewers:

- ❌ A bearer token, JWT, or anything matching `eyJ[A-Za-z0-9_-]{10,}\.` visible in the browser, the chat transcript, the server-rendered HTML, or the network tab body. (Headers excepted; that's where they belong.)
- ❌ Internal service URLs (`hr-agent.local`, `it-agent.local`) leaked into the LLM's view or the chat transcript.
- ❌ Stack traces or framework error messages shown to the user instead of friendly messages.
- ❌ The orchestrator continuing to do work for a session that Asgardeo has terminated (post-sign-out tool calls reaching specialists).
- ❌ A "skip consent" mode for development that ships in the demo build.
- ❌ Tokens cached in `localStorage` or `sessionStorage` of the SPA. (Cookies for the orchestrator session are fine; bearer tokens for specialists must never reach the browser.)

---

## 8. How to validate against this doc

For each release / PR / demo prep:

1. **Walk Scenario A** in a fresh browser. Take a screenshot of the consent screen. Confirm Approve/Deny both work.
2. **Walk Scenario B** with logged user actions. Save the chat transcript. Save the LangSmith run-tree export. Run the N13 leak-scan against the export.
3. **Walk Scenario C.** Save the network log. Confirm sign-out completes; replay attempts fail per the timing budget.
4. **Walk D-1 through D-9** as a test suite (these correspond to N/R-tests automated in CI).
5. **Read §7's red lines** and confirm none are tripped.

This doc is the source of truth for what "the user experience works" means. The technical milestone plan is how we get there; this is what we get.

---

## 9. References

- Technical plan: [milestone-plan.md](milestone-plan.md)
- Asgardeo setup walkthrough: [asgardeo-setup.md](asgardeo-setup.md) (created in Sprint 0)
- Original POC vision: [Proof of Concept (POC)_ Identity-First AI Agent Governance.md](Proof%20of%20Concept%20(POC)_%20Identity-First%20AI%20Agent%20Governance.md)
- Negative tests: milestone-plan §3.4-H (Sprint 1 N1–N15) and §4.4-G (Sprint 2 R1–R11)
