# Sprint 5 — Stage 4: UX Design

**Date:** 2026-05-11
**Scope:** S5 is a backend/orchestration change ([`sprint-5.md`](sprint-5.md) §3). The only user-visible UX delta is a transient "thinking" affordance to cover the extra LLM round-trip latency, plus copy-deck notes for the LLM-composed reply.

---

## 1. What changes for the user — almost nothing

The chat panel, the consent widget, the sidebar cards, the Reports page — all unchanged. Two small things:

### 1a. "Thinking…" affordance (router latency)

**Problem:** in keyword mode, `POST /api/chat` returns and the first SSE event (`routing` → `consent_required`) arrives almost instantly. In LLM mode there's now a Gemini round-trip *before* the first event (the router call, ≈0.5–2 s). Without feedback the chat looks frozen.

**Solution:** reuse the existing progress element pattern (the same `progressEl` toggled around the sign-in exchange in `client/app.js`). On `POST /api/chat` submit:
- Append a lightweight assistant-side placeholder bubble: a muted "Thinking…" line with the existing animated-dots affordance (no new spinner asset — reuse the dots used elsewhere, or three CSS-animated `·`).
- Replace it the moment the **first** SSE event for this request arrives (`routing`, `consent_required`, or `chat_message`).
- If the request errors before any SSE event, replace it with the error bubble.

**States:**
| State | Bubble |
|---|---|
| just submitted, no SSE yet | `Thinking…` (muted, animated dots) |
| `routing` received | `Thinking…` → keep, or update to `Working on it…` (optional; either is fine) |
| `consent_required` received | replace with the consent widget (existing) |
| `chat_message` received | replace with the assistant reply bubble (existing) |
| request failed (no SSE) | replace with the existing error bubble |

Keep it boring. No skeleton screens, no typing animation on the reply, no progress bar. GitHub-Primer-muted text + the existing dots.

### 1b. LLM-composed reply — copy-deck guardrails

The reply text is now whatever Gemini writes (with the keyword `_render_result` concatenation as fallback). The composer **prompt** (Stage 6) enforces:
- **Plain text only.** No markdown headings, no HTML, no code fences. Rendered with `textContent` regardless — but the prompt should ask for plain prose so it reads right.
- **Cover every tool that ran.** If two tools ran, the reply mentions both outcomes. If one was declined (`ERR-CIBA-005`), the reply says so plainly ("I couldn't do X — you declined that one").
- **Missing-arg → ask, don't apologise vaguely.** If a tool failed with `ERR-AGENT-002` (missing required arg), the reply asks for the specific missing info ("To apply for annual leave I need the start and end dates"). Don't say "something went wrong."
- **Don't invent.** The reply may only state facts present in the tool outputs. No fabricated request IDs, balances, cubicle numbers.
- **Concise.** 1–4 sentences for simple cases; bullet list only when ≥3 items (e.g. listing assigned assets or leave types). The composer prompt sets `max_output_tokens` (512 default) — long rambles are clipped, which is acceptable.
- **First person, employee-facing voice.** "You have 20 days of annual leave" — same register as today's `_render_result` strings.

### 1c. Consent widget — unchanged, and that's deliberate

The consent widget's action text still comes from the server-side `_TOOL_REGISTRY` ("Apply for leave on your behalf", "View your leave balance", "Assign cubicle C-027 to jane.doe"). The LLM never writes consent copy. Visually and behaviourally identical to Sprint 4. (This is also a security requirement — see `sprint-5.md` §2.3.)

## 2. Empty / error states (no change)

- Chat empty: existing placeholder.
- Sign-in error, session expiry, network drop: existing flows.
- New error surface: if Gemini fails *and* the keyword fallback also produces nothing routable, the assistant bubble reads (composed by `_render_result` fallback or a static string): *"I'm not sure what you'd like me to do — try asking about your leave, your cubicle, or your IT assets."* Same styling as a normal reply bubble.

## 3. Accessibility

- The "Thinking…" placeholder gets `aria-live="polite"` (or sits inside the existing `aria-live` chat region) so screen readers announce it, then announce the replacement reply. Don't double-announce — replace the node, don't append.
- No new colour-only signalling. The muted "Thinking…" uses the existing `--text-muted` token + the dots, which are also text.

## 4. Out of scope (UX)

- Token streaming / progressive reply rendering.
- A visible "the AI chose tool X" trace in the UI (it's in the server logs / SSE `routing` event payload, which the SPA may already log to console — fine for debugging, not a UI feature).
- Any redesign of the chat panel, consent widget, or sidebar.

## 5. Handoff to Stage 5 / Stage 6

- Stage 5: confirm no `/api/chat` contract change; the "Thinking…" affordance is purely client-side (it keys off the existing SSE stream — no new event needed, just "first event for this request → clear placeholder").
- Stage 6: the composer prompt must encode §1b's guardrails verbatim-ish.
- Implementation: `client/index.html` (placeholder bubble template), `client/app.js` (show on submit, clear on first SSE event), `client/styles.css` (`.chat-thinking` muted style + dots — reuse existing dot animation if present).
