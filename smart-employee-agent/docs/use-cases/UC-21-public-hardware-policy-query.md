# UC-21 — Employee asks about hardware allocation policy (unauthenticated)

**Status:** written (S6). Uses the new `POST /public/chat` endpoint and the pre-login Info Bot widget. Hardware allocation policy is new content authored in `it_server/service/store.py` (`_SEED_HARDWARE_POLICY`) and embedded in `orchestrator/chat/public_handler.py`.

## Goal

An employee (particularly a new hire or someone who has not yet signed in) wants to understand what equipment they will receive — laptop model, phone, peripherals, replacement cycle — without needing to authenticate.

## Pre-conditions

- User is on the sign-in page (unauthenticated).
- Info Bot widget is visible.
- Hardware allocation policy has been authored in `it_server/service/store.py` and its text is embedded in `public_handler.py`.

## Main flow

1. User types: **"What laptop will I get as a new hire?"** and clicks Send.
2. Widget sends `POST /public/chat {"message": "What laptop will I get as a new hire?"}`. No auth header.
3. `PublicInfoHandler` builds the system prompt including the hardware allocation policy knowledge.
4. Gemini answers from the embedded policy.
5. Widget renders the reply in a bot bubble.

**Example reply (LLM mode):**
> "For new hires, the standard laptop allocation depends on your role: Developer/Engineering roles receive a MacBook Pro 14" M4 Pro, while Business/Admin roles receive a Dell XPS 15. You'll also receive a mobile phone (iPhone 15 or equivalent) and a 27" 4K monitor if you're on-site. Remote-first employees receive the laptop and phone; monitors are available on request subject to stock."

## Post-conditions

- Reply displayed in widget. No session created. No asset inventory is queried from `it_server` — the answer comes solely from the embedded policy text.

## Alternate flows

### A1 — Replacement cycle query
- User asks: **"How often do laptops get replaced?"**
- LLM replies: "Laptops are replaced every 3 years, phones every 2 years, and peripherals as needed."

### A2 — Peripheral query
- User asks: **"Do I get a mouse and keyboard?"**
- LLM replies with the standard peripherals allocation.

### A3 — Personal asset query (out of scope for public widget)
- User asks: **"What IT assets have been assigned to me?"**
- LLM responds per the system prompt guardrail: *"I can only share the general hardware allocation policy here. To view your personal asset assignments, please sign in to the employee portal."*
- No `it_server` call is made.

### A4 — Gemini unavailable (static fallback)
- `_static_fallback` detects "laptop" / "hardware" / "equipment" / "device" keyword → returns the pre-written hardware policy template string.

## Notes

- The hardware allocation policy is **new content** for S6. The canonical text lives in `it_server/service/store.py` (`_SEED_HARDWARE_POLICY`). The `public_handler.py` embedding is a manual copy — a Stage-10 snapshot test asserts the two are in sync (see `sprint-6.md` §8 R6).
- UC-07 (HR Admin issues an asset) and UC-12 (employee self-service asset discovery) remain authenticated workflows using the MCP tool chain — unaffected by this UC.
