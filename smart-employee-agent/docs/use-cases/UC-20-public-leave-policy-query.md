# UC-20 — Employee asks about leave policy (unauthenticated)

**Status:** written (S6). Uses the new `POST /public/chat` endpoint and the pre-login Info Bot widget.

## Goal

An employee who has not yet signed in (or a prospective hire, visitor, or contractor) wants to understand the company's leave entitlements — how many days, which types, carry-over rules — without needing to authenticate.

## Pre-conditions

- User is on the sign-in page (unauthenticated).
- Info Bot widget is visible.

## Main flow

1. User types: **"How many leave days do I get?"** and clicks Send.
2. Widget sends `POST /public/chat {"message": "How many leave days do I get?"}`. No auth header.
3. `PublicInfoHandler` builds the system prompt with the embedded leave policy knowledge base.
4. Gemini composes a plain-text summary of all leave types and their entitlements.
5. Widget renders the reply in a bot bubble.

**Example reply (LLM mode):**
> "Employees receive 20 working days of annual leave per year (accrued monthly, requires HR approval), 10 working days of sick leave (medical certificate needed for absences over 2 consecutive days), and 5 days of personal leave (no reason required, not carried over). Up to 5 unused annual leave days may be carried over to the next year; unused sick and personal leave lapses."

## Post-conditions

- Reply displayed in the widget. No session created. No personal data accessed.

## Alternate flows

### A1 — Specific leave type query
- User asks: **"What is the sick leave policy?"**
- LLM focuses on the sick leave portion: 10 days, medical certificate requirement.

### A2 — Carry-over query
- User asks: **"Can I carry over unused leave?"**
- LLM explains: up to 5 annual leave days carry over; sick and personal leave do not.

### A3 — Personal balance query (out of scope for public widget)
- User asks: **"How many leave days do I have left?"**
- LLM responds per the system prompt guardrail: *"I can only share general leave policy information here. To check your personal leave balance, please sign in to the employee portal."*
- Widget shows this as a normal bot reply. No data is fetched from `hr_server`.

### A4 — Gemini unavailable (static fallback)
- `_static_fallback` detects "leave" keyword → returns a pre-written policy summary template.

## Notes

- The reply describes population-level policy only — the LLM has no access to any individual's data.
- The system prompt explicitly prohibits inventing personal balances or speculating about named individuals (see `sprint-6.md` §2.5).
- UC-13 (applying for leave) still requires authentication and uses the authenticated `POST /api/chat` route.
