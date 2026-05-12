# UC-19 — Employee asks about public holidays (unauthenticated)

**Status:** written (S6). Uses the new `POST /public/chat` endpoint and the pre-login Info Bot widget.

## Goal

An employee (or visitor) who has not signed in wants to know which days are company public holidays in 2026 — without needing an account or going through the sign-in flow.

## Pre-conditions

- User is on the sign-in page (unauthenticated).
- The Info Bot widget is visible (desktop: panel open; mobile: FAB visible).
- `orchestrator` is reachable at `http://localhost:8080`.

## Main flow

1. User types in the Info Bot input: **"What are the public holidays this year?"** and clicks Send.
2. `client/app.js` sends `POST http://localhost:8080/public/chat` with body `{"message": "What are the public holidays this year?"}`. No `Authorization` header.
3. Orchestrator validates the request (Pydantic, length ≤ 500 chars).
4. `PublicInfoHandler.answer(message)` builds the system prompt with the embedded 2026 UAE holiday knowledge, calls `GeminiLLMClient.compose_public(system_prompt, message)`.
5. Gemini responds with a plain-text list of the 14 public holidays with dates.
6. Widget renders the reply via `textContent` in a bot bubble.
7. User reads the holiday list — no sign-in required.

**Example reply (LLM mode):**
> "Here are the company's 2026 public holidays: New Year's Day (Jan 1), Eid Al Fitr — 3 days (Mar 30–Apr 1), Arafat Day (Jun 7), Eid Al Adha — 3 days (Jun 8–10), Islamic New Year (Jun 27), Prophet's Birthday (Sep 5), Commemoration Day (Nov 30), and National Day — 3 days (Dec 1–3). That's 14 public holidays in total."

## Post-conditions

- Reply is visible in the widget bubble area.
- No session is created. No user data is accessed or logged beyond the orchestrator's standard request log (no PII — the message contains only a generic question).
- The sign-in form remains accessible and unchanged.

## Alternate flows

### A1 — Specific date query
- User asks: **"Is June 8 a holiday?"**
- LLM checks the embedded calendar → replies: "Yes — June 8, 2026 is Eid Al Adha (Day 1), a company public holiday."

### A2 — Month query
- User asks: **"Any holidays in December?"**
- LLM replies with the National Day entries (Dec 1–3).

### A3 — Gemini unavailable (static fallback)
- `GeminiLLMClient.compose_public` raises an exception (timeout, key absent).
- `_static_fallback` detects "holiday" keyword → returns pre-written template listing all 14 holidays.
- User sees the same information; widget shows no error indicator.

## Notes

- The reply is rendered with `textContent` — no HTML injection risk even if the LLM produces markup.
- The orchestrator logs the request at INFO level with a correlation ID. No message content is stored persistently.
- This UC is fully offline-capable from the HR/IT servers' perspective — the handler never calls `hr_server`.
