# Consent Widget Spec — Sprint 1 / S1.8

**Date:** 2026-05-07
**Owner:** S1.8 implementation
**Anchor:** legacy `_archive/agent.before-v3/main.py` `/api/obo/url` + `obo_flow.py` `callback_html()` patterns; replace PKCE redirect with SSE-pushed `auth_url`.

This is the design contract for the in-SPA Consent Widget that renders when a specialist agent (HR, IT) initiates CIBA. With **serial fan-out** (Q2 decision), only one widget is visible at a time; if a query needs both HR and IT, the second widget appears AFTER the first one resolves.

---

## 1. When the widget appears

Trigger: orchestrator pushes an SSE event of shape:
```json
{ "type": "ciba_url",
  "agent_id": "hr_agent",
  "agent_label": "HR Agent",
  "auth_url": "https://13.60.190.47:9443/oauth2/ciba_authorize?authCodeKey=...",
  "binding_message": "HR Agent wants to view your leave balance for request 7f3a-c12d",
  "scope": "openid hr.read",
  "expires_in": 300,
  "request_id": "7f3a-c12d-..." }
```

SPA renders the widget in the assistant panel above the chat input. **Does not modal-block** the page (matches legacy demo's tone — assistant-on-the-side, not modal-hijack).

---

## 2. Widget anatomy (acceptance criteria for S1.8)

```
┌─ Action requires your approval ──────────────────────────┐
│                                                           │
│  [HR icon] HR Agent                                       │
│  Wants to: View your leave balance                        │
│                                                           │
│                     binding code: 7f3a-c12d  (small,     │
│                                                muted)    │
│                                                           │
│  [   Approve   ]   [ Deny ]              ⏱ expires 4:53  │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

**Required fields and rendering rules:**

| Field | Source | Render rule |
|---|---|---|
| Agent label | `agent_label` from event | Human-friendly: "HR Agent" / "IT Agent". **Never** raw client_id. With colored icon — HR=teal, IT=purple — to anchor audience identity visually. |
| Plain-language action | derived from `scope` via local map | Map `hr.read` → "View your leave balance"; `hr.approve` → "Approve a leave request on your behalf"; `it.read` → "Look up available laptops". **Map lives in the SPA**, not the agent. Required to avoid "phishy" raw-scope display. |
| Binding code | `request_id` (short form, 8 chars) | Mono font, secondary text color, smaller than the main action text. Lets users verify the IS consent screen will show the same code. |
| Approve button | — | Primary visual weight (filled, prominent). Visible without scroll. |
| Deny button | — | Secondary visual weight (outline) — but **same size** as Approve, NOT a tiny secondary link. |
| Expiry countdown | `expires_in` from event | Counts down live. At T-60s, change color to amber. At T-0, widget self-converts to the "expired" state. |

**Anti-pattern to avoid:** rendering `binding_message` verbatim as the main UI text. The full `binding_message` is for the IS consent screen (server-side). The SPA widget renders the *parsed* fields (agent_label, action, request_id) cleanly.

---

## 3. State machine

```
   AWAITING_APPROVAL ──Approve clicked──▶ VERIFYING
   AWAITING_APPROVAL ──Deny clicked────▶ DENIED ──▶ (collapse to a transcript line)
   AWAITING_APPROVAL ──countdown=0─────▶ EXPIRED ──▶ (show retry CTA)

   VERIFYING ──token issued by orch (SSE event)─▶ WORKING
   VERIFYING ──CIBA error (SSE event)──────────▶ ERROR ──▶ (show retry CTA)

   WORKING   ──MCP call complete───────────────▶ DONE   ──▶ (collapse to transcript line ✓)
   WORKING   ──MCP error───────────────────────▶ ERROR
```

**Visual treatment per state:**

| State | Card visual | Copy |
|---|---|---|
| AWAITING_APPROVAL | Outline card; pulse animation on Approve button | Heading + action + binding code + buttons |
| VERIFYING | Card collapses to a slim row; animated 3-dot indicator in agent's color | "Verifying with your identity provider…" (NOT "Securing access" — too jargon-y) |
| WORKING | Slim row, solid color | "HR Agent is checking your leave balance…" (use the action text) |
| DONE | Slim row fades into the chat transcript | "✓ HR Agent — completed" |
| DENIED | Slim row, dimmed | "⊘ Declined — HR Agent will not run for this request" |
| EXPIRED | Card persists, amber outline | "Approval window expired" + [Retry] button |
| ERROR | Card persists, red outline | "Something went wrong (code: <auth_req_id short>)" + [Retry] / [Cancel] |

A **Cancel** button is visible during VERIFYING and WORKING. Clicking it sends `POST /api/ciba/cancel?auth_req_id=...` to the orchestrator → orchestrator stops polling → widget transitions to DENIED state.

---

## 4. Re-CIBA on token expiry (Sprint 2 D2.5)

When token-B has expired and user issues a follow-up query that needs the same specialist, the widget **does NOT** silently re-prompt as if it were a new request. It renders distinctly:

```
┌─ Session refresh ────────────────────────────────────────┐
│  ↻  HR Agent's previous access has expired                │
│     You approved this 1h 12m ago.                         │
│                                                           │
│  Wants to: View your leave balance                        │
│                                                           │
│  [   Re-approve   ]   [ Skip ]                            │
└───────────────────────────────────────────────────────────┘
```

Color: amber banner accent (not teal/purple) to mark it as a session-extension UX, not a fresh authorization. Without this distinct treatment, ~30% of test users will reflexively click Deny thinking "I already approved this."

---

## 5. Stage demo timing budget (60–90s for two-specialist serial)

| Phase | Budget |
|---|---|
| User submits query | 1s |
| LLM routing (or keyword fallback) | 1–3s |
| Orchestrator → HR-agent A2A | 1s |
| HR-agent CIBA initiation | 1–2s |
| Widget render | <500ms |
| User approve click + IS consent screen + confirm | 4–6s |
| HR-agent polls + receives token | 2–4s |
| HR-agent → HR-MCP call | 1s |
| Repeat for IT (same budget) | 12–22s |
| Orchestrator composes user-facing answer | 2–3s |
| **Total** | **~30–45s for two specialists** |

If demo runs >90s on stage, fall back to single-specialist query for the live demo and pre-record the dual-specialist demo for video.

---

## 6. Out of scope for Sprint 1

- Multi-card stack (parallel fan-out) — Q2 decision is serial; defer indefinitely.
- Push notifications via email/SMS — Sprint 1 uses External notification channel only (auth_url over SSE).
- Dark mode / theme switching.
- Accessibility audit (WCAG) — Sprint 2 polish item if time permits.
- i18n / localization — English only.

---

## 7. Acceptance tests for S1.8

A1. Render the widget for HR-agent — agent label says "HR Agent" (not the client_id), icon is teal, action says "View your leave balance" (not "hr.read").
A2. Render the widget for IT-agent — purple icon, action "Look up available laptops".
A3. Click Approve → widget transitions through VERIFYING → WORKING → DONE within 30s with a real CIBA flow.
A4. Click Deny → widget transitions to DENIED, orchestrator returns a graceful "I couldn't access HR" message in chat.
A5. Wait 5min without clicking → widget transitions to EXPIRED with Retry button.
A6. During VERIFYING, click Cancel → polling stops, widget collapses, no zombie polls in orchestrator logs.
A7. Token expires (set IS app token TTL to 60s for this test); next query triggers Session refresh treatment per §4.
A8. Maps to N-tests: N12 (Deny), N18/N19 (mid-flow denial), N20 (timeout), N23 (browser closed), N27 (consent fatigue moderated test in Sprint 2).

---

## 8. Reference: legacy demo widget anchor

- `_archive/agent.before-v3/main.py` lines 511–569: `/api/obo/url`, `/api/obo/callback`, `/api/obo/status` endpoints. **Pattern reused; mechanism replaced.**
  - OLD: PKCE state → user clicks button → popup opens → consent → callback HTML → window.opener.postMessage.
  - NEW: SSE event delivers `auth_url` → widget renders → user clicks Approve → browser navigates to auth_url → IS consent → callback closes window → orchestrator's poll loop picks up token.
- `_archive/agent.before-v3/obo_flow.py` `callback_html()`: keep verbatim — it's the post-consent close-the-popup HTML; works identically under CIBA.
