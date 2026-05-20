# UC-03 — Two-specialist serial query (the headline demo)

**Sprint:** 1
**Priority:** Critical (the demo the POC is judged on)
**Maps to N-tests:** all of UC-02's, plus N18 (mid-flow denial: HR ok, IT denied), N19 (all denied), N24 (parallel double-send), N27 (consent fatigue moderated bar)
**Maps to scenarios:** [user-experience.md](../user-experience.md) Scenario B (full)

## Actors
- Same as UC-02 plus: IT Agent + IT Server.

## Preconditions
- Same as UC-02 (both specialists configured + reachable).
- Orchestrator's LLM (or keyword-fallback) routes the query to **both** HR and IT in sequence.
- Q2 decision locks **serial** fan-out — the orchestrator does HR fully (UC-02 main flow) before starting IT.

## Trigger
User asks a query that requires both specialists, e.g.: *"Show me my leave balance and what laptops are available."*

## Main flow
1. SPA submits the query (steps 1–2 of UC-02).
2. Orchestrator's LLM produces a tool-call sequence: `[("hr_agent", "get_leave_balance"), ("it_agent", "list_available_assets")]`.
3. Orchestrator runs **HR leg as UC-02 in full** (steps 3–18). User sees the HR Consent Widget, approves, sees the leave answer.
4. Once HR's reply has been rendered in chat, orchestrator emits SSE: `{type: "routing", agent: "it_agent"}`. Chat shows: *"Now routing to IT Agent…"*
5. Orchestrator runs **IT leg as UC-02 in full** (steps 4–17 with HR substituted by IT). User sees the IT Consent Widget, approves, sees IT's contribution.
6. Orchestrator's LLM composes a **final combined reply** from the two tool outputs: *"You have 12 days of leave. Available laptops: MBP-14, MBP-16, XPS-13."*
7. The combined reply renders in chat.

## Exception flows

### EX-1 — User denies HR (step 3) — see UC-04 EX-1
### EX-2 — User denies IT (step 5) — see UC-04 EX-2
### EX-3 — User denies both — see UC-04 EX-3
### EX-4 — HR succeeds but IT crashes (step 5)
1. Orchestrator catches IT failure.
2. LLM composes a partial answer: *"You have 12 days of leave. I couldn't reach the IT system; try again in a moment."*
3. Maps to N18 implicitly.

### EX-5 — User submits a second query before the first finishes
1. SPA blocks the input or queues the message visibly (Stage 3 decides).
2. Orchestrator does NOT process the second message until the first is done.
3. Maps to N24 (no race in CIBA singleflight).

## Postconditions
- **Success:** chat shows ONE combined reply that integrates both specialists' outputs; orchestrator session map has 2 records `(hr_agent, jti_b, ...)` and `(it_agent, jti_c, ...)`.
- **Partial:** chat shows a partial reply mentioning what was missing; session map has 1 record (whichever succeeded).

## Demo storyboard (60–90s on stage)

| Time | Audience sees | Presenter narrates |
|---|---|---|
| 0–5s | Logged-in chat. Type the query. Hit Enter. | "I'm asking for both leave + assets." |
| 5–10s | "Routing to HR Agent…" then HR Consent Widget appears | "The agent isn't *allowed* to act for me until I approve. Watch." |
| 10–18s | Click Approve. Widget transitions Verifying → Working → Done. Chat shows leave balance. | "It opened a tab to my IdP. The same binding code (KX-7491) appeared there. I confirmed. The agent now has a token, scoped only to leave-read, on my behalf." |
| 18–22s | "Now routing to IT Agent…" then IT Consent Widget. | "It asks AGAIN for IT — because IT is a separate authority. This is identity-first governance." |
| 22–35s | Approve. Widget cycles. Chat shows the asset list. | (silence — let the audience watch) |
| 35–40s | Final combined reply renders. | "Two agents acted on my behalf. Two explicit consents. Two scoped tokens. Full audit." |
| 40–55s | Switch to IS audit log; show the two CIBA events. | "Here's what the IdP recorded — every action attributed to me, with the agent that acted." |
| 55–60s | Click Sign out (foreshadow Sprint 3). | "And when I sign out, those tokens die." |

## Design notes for downstream stages

### UX (Stage 3)
- **Single widget at a time** (Q2 lock). NEVER show both consent cards stacked.
- **Visible "Now routing to IT Agent" indicator** — gives the audience a beat between consents.
- **Final reply** must clearly integrate both specialists' outputs into one coherent sentence (LLM composition). Stage 4 must specify the prompt template.
- **Keyword-fallback** (per S1.4b) must produce the same two-specialist routing for the canonical demo query, so the demo doesn't depend on OpenAI being up.

### Architecture (Stage 4)
- Orchestrator's LLM tool-routing must be **deterministic for the demo query** — pin OpenAI's temperature to 0 OR use keyword fallback to guarantee the demo works on stage.
- After HR's reply is in, orchestrator MUST wait until SSE delivery confirms (or just give it a fixed pause) before starting the IT leg, so the user has time to read the HR answer and prepare for the IT consent.
- Consider a `routing_pause_ms` config (default 500ms) between specialists.

### Testing (Stages 7–8)
- **Manual stage rehearsal** is the primary validation. Time the full flow with a stopwatch — must be ≤90s for a comfortable stage demo.
- **N27** is a **moderated 5-user usability bar** in Sprint 2 — not a CI test. Measures consent-read-time on widget #2 vs #1 to detect muscle-click.
- **Smoke test:** run UC-03 in a script against the live IS each morning of demo week.
