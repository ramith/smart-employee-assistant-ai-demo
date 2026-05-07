# User Experience & Data-Flow Validation Spec

**Purpose:** plain-language description of what the user sees and what happens behind the scenes, written so the implementation can be validated against it. Every observable behavior here is something a tester or demo audience can check without reading code.

**How to use this doc:** when reviewing a PR, demo, or test report, walk through the scenarios below. If any "Expected" line is wrong, the implementation is wrong (or this doc is wrong — fix whichever is out of sync first). Acceptance-criteria lines map to N-test IDs in [milestone-plan.md](milestone-plan.md) §5.

**Architecture this doc describes:** v4 — per-agent CIBA, **serial** fan-out, depth-1 OBO per specialist. See [`spikes/wso2-is-capability-memo.md`](spikes/wso2-is-capability-memo.md) for why.

**Scope:** Sprint 1 (happy path), Sprint 2 (correlation + denial polish), Sprint 3 (revocation). UAE Pass federation and signed envelopes are roadmap, out of scope for the demo.

---

## 1. Cast of characters

| Name | What it is | Where it runs |
|---|---|---|
| **You** (the employee) | The human user, in a browser. Has a role like `employee` or `hr_admin`. | Browser SPA at `client/` |
| **WSO2 IS** | The identity provider. Issues tokens, runs the per-agent consent screen, can revoke sessions. **Is the only place consent decisions are made.** | On-prem WSO2 Identity Server 7.2.0 |
| **Orchestrator** | The chat receptionist. Talks to the user, decides which specialist to ask. Has an LLM brain (Gemini, with a deterministic keyword fallback for demos). | `orchestrator/` |
| **HR Agent** | A specialist that knows HR things (leave, time-off). Initiates its own CIBA flow when invoked. | `hr_agent/` |
| **IT Asset Agent** | A specialist that knows what hardware each employee has. Same shape as HR Agent. | `it_agent/` |
| **HR Server / IT Server** | The MCP backends each specialist talks to. | `hr_server/`, `it_server/` |

When this doc says "the system," it means the whole thing collectively.

---

## 2. Scenario A — First sign-in (the orchestrator-consent moment)

### What you do
1. Open the app at `http://localhost:3001`. Click **Sign in**.
2. You land on WSO2 IS's login page. Enter your username + password.

### What you see next
3. **A consent screen** appears (rendered by WSO2 IS, not the app):
   > "**Orchestrator Agent** wants to act on your behalf. Approve?"
   With buttons **Approve** and **Deny**.

### What happens if you click Approve
4. You're redirected back to the chat app. You see your name in the header. The chat is empty and ready.

### What happens if you click Deny
5. You're redirected back to the chat app's sign-in page with a **clear, friendly error message** ("You did not approve the delegation. Please try again or contact admin."). No chat session is created.

### Behind the scenes (in plain words)
- WSO2 IS issued the orchestrator a **delegated token (token-A)** that names you (the principal) and the orchestrator (the acting party).
- token-A is bound to the orchestrator only — it cannot be used at HR Agent, IT Agent, or any MCP server. Those will require **separate** consents later.
- If you denied, no token was issued.

### Acceptance criteria
- ✅ A consent screen is visible — not bypassed.
- ✅ Denying produces a user-friendly error, not a crash or a half-logged-in state.
- ✅ Approving creates exactly one session record on the orchestrator.
- Maps to: **N1** (Pattern C produces token-A with depth-1 act), **N12** (denial path).

---

## 3. Scenario B — The demo query (leave + equipment, **serial**)

This is the headline scenario. The architecture uses **serial fan-out** — the orchestrator routes through ONE specialist at a time, not in parallel. You will see two consent prompts, one after the other, with answer narration in between.

### What you do
1. In chat, type:
   > "Show me my leave balance and what laptops are available."
2. Press Enter.

### What you see (60–90 seconds total, one specialist at a time)

| Time | What appears |
|---|---|
| 0–3s | Orchestrator: "Routing your request to HR Agent first…" |
| 3–8s | **Consent Widget #1** appears in the assistant panel: "**HR Agent** wants to view your leave balance" + Approve/Deny + a small binding code (e.g. `7f3a-c12d`) |
| 8–12s | You click **Approve** → browser opens a tab to WSO2 IS consent screen → you see the same binding code → confirm. The widget transitions to "Verifying with your identity provider…" → "HR Agent is checking your leave balance…" |
| 12–18s | Widget collapses to a transcript line: "✓ HR Agent — completed". Chat shows: "You have 12 days of leave." |
| 18–22s | Orchestrator: "Now routing to IT Agent for asset info…" |
| 22–35s | **Consent Widget #2** appears: "**IT Agent** wants to look up available laptops". Same dance — Approve, IS consent, polling, MCP call. |
| 35–40s | Final answer composed: "Available laptops: MBP-14, MBP-16, XPS-13." |

### Behind the scenes (in plain words)
- The orchestrator's LLM reads your message, decides it needs HR first.
- It forwards your request to HR Agent over A2A, carrying token-A as a Bearer header. HR Agent extracts your user identity (`sub`) from token-A.
- HR Agent calls WSO2 IS's `/oauth2/ciba` endpoint, asking for a token to act on your behalf for `hr.read`. IS replies with an `auth_url`.
- The orchestrator pushes the `auth_url` to your browser via Server-Sent Events. The Consent Widget renders.
- You click Approve → browser opens the auth_url tab → IS shows the consent screen → you confirm.
- Meanwhile HR Agent has been polling. As soon as IS records your consent, HR Agent receives **token-B** — `sub=you, act.sub=hr_agent`. It uses token-B to call HR Server (the MCP backend).
- HR Agent returns the answer to the orchestrator.
- Orchestrator now routes the second half of your query to IT Agent. **The same flow repeats** — separate CIBA, separate consent widget, separate token (token-C with `act.sub=it_agent`).
- Final answer is composed by the LLM and rendered.

### Why every agent asks separately (the "feature, not bug" framing)
This is identity-first agent governance. Each agent's authority is **explicitly user-consented at the moment it acts**, not assumed from a one-time login. If an attacker compromised the orchestrator, they could not silently invoke specialists — every action requires your real-time approval, with the agent's name and the action shown to you on the consent screen.

### What you do NOT see
- The tokens themselves. Never visible to you, never logged to the chat, never in the browser console.
- Internal URLs (`hr_agent:8001`, `it_agent:8002`).
- Stack traces or framework errors. Only friendly messages.

### Acceptance criteria
- ✅ Two consent widgets appear, one at a time (NOT both up front).
- ✅ Each widget shows the agent's display name (not its OAuth client ID), a plain-language action, and a binding code.
- ✅ Each token issued has the correct shape: `sub=you, aut=APPLICATION_USER, act.sub=<this-agent>`.
- ✅ token-B and token-C have **distinct** `aud` values; presenting one to the other agent's MCP server is rejected (cross-audience defense).
- ✅ The total wait time is dominated by your own click time, not network or polling.
- ✅ No bearer tokens or JWT-shaped strings appear anywhere in the chat UI, browser network tab body, console, or trace export.
- Maps to: **N1, N7, N16, N21, N25, N27** (consent fatigue moderated bar in Sprint 2).

---

## 4. Scenario B-1 — Mid-flow denial (you change your mind)

### What you do
1. Same as Scenario B — submit "leave + laptops" query.
2. Approve the **HR** consent widget (you get leave info).
3. When the **IT** consent widget appears, click **Deny**.

### What you see
4. The IT widget collapses to "⊘ Declined — IT Agent will not run for this request."
5. The chat shows a graceful partial answer: "You have 12 days of leave. I couldn't access IT asset info — let me know if you'd like to retry."

### Acceptance criteria
- ✅ Orchestrator does NOT error out; it returns the partial result with explanation.
- ✅ HR Agent's token (already issued and used) remains valid until natural expiry (1hr); not affected by IT denial.
- Maps to: **N18, N19**.

---

## 5. Scenario C — Sign-out (revocation)

### What you do
1. Click **Sign out** in the chat header.

### What you see
2. You're redirected to the IS sign-out page, then back to the app's sign-in page. Within a second.
3. If you (or someone using the same browser) click "Sign in" again, you go through the **full** flow again — IS login + Pattern C consent screen.

### Behind the scenes (Sprint 3 work)
- The orchestrator revokes token-A (`POST /oauth2/revoke` to IS).
- Using its **session map** (built up during the conversation: `session_id → [(agent_id, jti, exp)]`), the orchestrator fans out **cache-bust signals** to every specialist that holds an outstanding token for your session.
- Each specialist marks the listed `jti`s as denylisted; on the next MCP call it would refuse.
- MCP servers also independently introspect every token (with a 2-second positive cache), so even without the cache-bust, a revoked token is rejected within ~2 seconds.

### What should fail after sign-out
- Replaying a previously-issued token-B at HR Server → **rejected within 5 seconds**.
- Replaying token-A or token-C → same.
- An in-flight chat that was streaming a response when you signed out → **stream is cancelled cleanly**; no further specialist calls happen after sign-out.

### What about CIBA prompts in flight?
- If you click Sign out while a Consent Widget is on screen waiting for your approval: the orchestrator cancels the polling loop and signals IS to invalidate the `auth_req_id`. If you then click Approve in a stale browser tab, the resulting token (if any) will be rejected by the specialist on first introspect.

### Acceptance criteria
- ✅ Sign-out completes within ~1 second of click (the user-visible part).
- ✅ Replays of stale tokens fail at every specialist within 5 seconds.
- ✅ Admin terminating your session in the IS Console has the same effect as signing out yourself.
- Maps to: **R1, R2, R3, R14** (pending-CIBA-at-logout), **R15** (half-fan-out logout), **R16** (audit-chain integrity).

---

## 6. Scenario D — Adversarial flows (what should NOT happen)

### D-1 — A stolen HR-Agent token cannot be used at IT Server
**Setup:** capture token-B in a test environment.
**Action:** present it to IT Server.
**Expected:** IT Server rejects it — `aud` mismatch (token-B's `aud` is hr_agent's OAuth Client ID, not it_agent's). Maps to **N21**.

### D-2 — Forwarding token-A directly to a specialist's MCP server
**Setup:** the orchestrator is supposed to make the specialist run its own CIBA. What if it forwards token-A to HR Server directly?
**Expected:** HR Server rejects — token-A's `act.sub` is `orchestrator-agent`, not `hr_agent`. The MCP server validates `act.sub == paired-agent-id`. Maps to **N25**.

### D-3 — A rogue server pretending to be IT Agent
**Setup:** stand up a malicious agent-card server claiming to be IT Agent.
**Expected:** the orchestrator's agent-card discovery uses an allowlist of URLs; only those are loaded.

### D-4 — Stolen `actor_token` used with a different `login_hint`
**Setup:** an attacker captures HR Agent's I4 token (the one HR uses to authenticate `/oauth2/ciba`). Tries to initiate CIBA for a different user.
**Expected:** WSO2 IS validates the actor_token's binding to the agent identity and rejects mismatches. Maps to **N22**.

### D-5 — Specialist is down mid-conversation
**Setup:** during the demo query, IT Agent crashes or returns 503.
**Expected:** the user sees a friendly "I couldn't reach the IT system right now, here's the leave info I do have" — not a stack trace, not a hang.

### D-6 — LLM hallucinates a non-existent skill
**Setup:** the orchestrator's LLM tries to call a skill that doesn't exist on any specialist.
**Expected:** the user does NOT see the failure as a crash. The orchestrator returns a structured "I don't know how to do that" to its own LLM, which recovers and produces a graceful natural-language answer.

### D-7 — Browser closed during CIBA polling
**Setup:** widget appears, you close the tab without clicking either button.
**Expected:** orchestrator detects the dead SSE connection within seconds, cancels the polling loop, no zombie polls in logs. Maps to **N23**.

### D-8 — Two requests racing CIBA for the same user
**Setup:** rapid-fire two chat messages that both need HR Agent.
**Expected:** orchestrator deduplicates via singleflight per `(user_sub, agent_id, scopes)` — only one CIBA flow + one consent widget; both requests get answered from the same token. Maps to **N24**.

### D-9 — Missing `X-Request-ID` header on A2A
**Setup:** call HR Agent's A2A endpoint manually without `X-Request-ID`.
**Expected:** specialist refuses (or auto-generates with a warning logged — Sprint 2 decides which). Maps to **N26**.

### D-10 — client_id collision (T9)
**Setup:** misconfigure HR Server's `EXPECTED_AGENT_OAUTH_CLIENT_ID` env to it_agent's value.
**Expected:** all calls return 401 with a clear log line "configured client_id does not match incoming token aud". Maps to **N28**.

### D-11 — Hijack via the back-channel logout endpoint (Sprint 3)
**Setup:** while signed in as User-B, an attacker POSTs a valid logout-token for User-A's session to the orchestrator's `/auth/backchannel-logout`.
**Expected:** User-A's session is terminated (legitimate per spec). User-B's session is **completely unaffected** — the endpoint never inspects the caller's identity to decide whose session to kill, only the validated token's contents.

### D-12 — Replaying a logout-token (Sprint 3)
**Setup:** capture a valid logout-token. POST it twice.
**Expected:** the second POST is rejected (single-use enforcement).

---

## 7. UX-level edge cases

### 7.1 Returning user with active IS session
- When you return after a previous session, IS may or may not re-prompt for username/password depending on its session-cookie state.
- The Pattern C consent (Scenario A) **should still appear** — `prompt=consent` is the demo expectation.
- Per-agent CIBA consents (Scenario B widgets) **always appear** for each specialist invocation — they don't piggy-back on the IS SSO session.

### 7.2 What happens if WSO2 IS is down
- During sign-in: IS is the front door; if it's down, you can't sign in. SPA shows an error.
- After sign-in, mid-conversation: orchestrator can still hold your session, but no new CIBA flows are possible. Consent widgets fail to appear; chat shows "service is degraded — please retry in a moment."

### 7.3 Token expiry mid-conversation
- Default token-B and token-C TTL = 1 hour. No refresh-token extension (per F7 + Q3 decision).
- If your session is still active when a token expires, the next request to that specialist triggers a **re-CIBA**. The Consent Widget reappears, but with a distinct **Session Refresh** treatment ("HR Agent's previous access has expired — Re-approve?") rather than looking like a new request. See [`consent-widget-spec.md`](consent-widget-spec.md) §4.

### 7.4 You lack a needed scope
- If your role doesn't grant `hr.read` and you ask about leave, the orchestrator does NOT silently skip — it tells you "I don't have permission to look up HR info for you."

---

## 8. Things the user experience should NEVER include

These are phrased as red lines for reviewers:

- ❌ A bearer token, JWT, or anything matching `eyJ[A-Za-z0-9_-]{10,}\.` visible in the browser, the chat transcript, the server-rendered HTML, or the network tab body.
- ❌ Internal service URLs (`hr_agent:8001`, `it_agent:8002`) leaked into the chat or LLM context.
- ❌ Stack traces or framework error messages shown to the user instead of friendly messages.
- ❌ The orchestrator continuing to do work for a session that IS has terminated (post-sign-out tool calls reaching specialists).
- ❌ A "skip consent" mode for development that ships in the demo build.
- ❌ The SPA silently dropping a CIBA consent denial without informing the user.
- ❌ Tokens cached in `localStorage` or `sessionStorage` of the SPA. (Cookies for the orchestrator session are fine; bearer tokens for specialists must never reach the browser.)
- ❌ Two consent widgets stacked at once (the architecture is **serial** — Q2 decision).

---

## 9. How to validate against this doc

For each release / PR / demo prep:

1. **Walk Scenario A** in a fresh browser. Take a screenshot of the IS consent screen. Confirm Approve/Deny both work.
2. **Walk Scenario B** with logged user actions. Save the chat transcript and the per-hop logs. Use `grep <X-Request-ID>` to reconstruct the chain user → orchestrator → hr_agent → hr_server → it_agent → it_server.
3. **Walk Scenario B-1** — approve HR, deny IT, confirm partial answer.
4. **Walk Scenario C.** Save the network log. Confirm sign-out completes; replay attempts fail per the timing budget.
5. **Walk D-1 through D-12** as a test suite (these correspond to N/R-tests automated in CI).
6. **Read §8's red lines** and confirm none are tripped.

This doc is the source of truth for what "the user experience works" means. The technical milestone plan is how we get there; this is what we get.

---

## 10. References

- Technical plan: [milestone-plan.md](milestone-plan.md)
- Capability spike memo (canonical findings): [spikes/wso2-is-capability-memo.md](spikes/wso2-is-capability-memo.md)
- WSO2 IS setup walkthrough: [wso2-is-setup.md](wso2-is-setup.md)
- Consent widget design: [consent-widget-spec.md](consent-widget-spec.md)
- Council decisions: `memory/project_council_decisions_2026_05_07.md` (in user's auto-memory)
- N-tests catalog: milestone-plan.md §5
