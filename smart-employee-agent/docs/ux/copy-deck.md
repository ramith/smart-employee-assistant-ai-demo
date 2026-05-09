# Copy Deck — Sprint 1 Stage 3 Deliverable

**Date:** 2026-05-07
**Owner:** Stage 3 (UI Design)
**Scope:** All user-facing strings rendered by the SPA, plus orchestrator-composed natural-language replies that travel through the chat surface.
**Source UCs:** UC-01 through UC-06 in `../use-cases/`.
**Source UX spec:** `../user-experience.md` (Scenarios A, B, B-1, C, D-* and §8 red lines).
**Source widget spec:** `../consent-widget-spec.md`.

This deck is the canonical source for every literal string the user can read. If a string in code or in a screenshot does not match this deck, fix one of them before merging.

---

## Voice and tone preface

The product speaks in a single confident voice. It is calm, clear, and serious about identity. It does not chatter, joke, or apologize at length. Authority is shown by being precise about what happened, not by adding modifiers.

Principles:

- **Address the user as "you".** Never "the user". Never "we" except in the first-person voice of the orchestrator answering a question.
- **Name agents properly.** "HR Agent", "IT Agent", "Orchestrator Agent". Never raw client_ids, never internal hostnames, never "the bot".
- **Action verbs over noun phrases.** "View your leave balance" beats "Leave balance access".
- **No security jargon in the chat surface.** Avoid "OAuth", "CIBA", "JWT", "scope", "audience", "token". Replace with plain-language: "approval", "access", "permission", "session".
- **No exclamation marks.** No emojis except the small set fixed in the widget spec (`✓`, `⊘`, `↻`, `⏱`).
- **Errors name the cause briefly, then offer a next step.** Two short sentences max.
- **Latency states are progressive, not chatty.** Routing → Verifying → Working → Done. Each state replaces the previous one; do not stack messages.
- **Never expose tokens, request IDs longer than 8 chars, internal URLs, or stack traces.** See user-experience.md §8.

When in doubt, prefer fewer words.

---

## Conventions used in this deck

- `{placeholder}` denotes a runtime value. Valid placeholders are listed per row.
- "Surface" is the UI region rendering the string. "State" is the state-machine value or trigger condition.
- "Max length" is a soft cap; copy must read naturally below it. Hard truncation should not occur.
- Strings marked **dynamic template** are composed by the orchestrator's LLM with the listed inputs; the row shows the canonical phrasing the prompt template targets.

---

## 1. Login surface (UC-01)

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 1.1 | Sign-in page — page title | UC-01 main | `Smart Employee Assistant` | Browser tab title and page heading. Max 40 chars. |
| 1.2 | Sign-in page — subtitle | UC-01 main | `Sign in to ask about your leave, equipment, and team.` | One-line value proposition. Static. Max 80 chars. |
| 1.3 | Sign-in page — primary CTA | UC-01 step 1 | `Sign in` | Button label. Routes to `<IS>/oauth2/authorize`. Max 12 chars. |
| 1.4 | Sign-in page — helper text | UC-01 design notes | `You will be redirected to your identity provider.` | Small text below the button. Static. |
| 1.5 | Sign-in page — cert warning hint | UC-01 EX (dev only) | `First time? Your browser may show a certificate warning for the development identity server. Choose "Advanced" then "Proceed".` | Shown only when `ENV=dev`. Collapsible. |
| 1.6 | Session expired notice (banner above sign-in) | UC-06 EX-2 | `Your session has expired. Sign in again to continue.` | Shown when SPA arrives at `/login?reason=session_expired`. Dismissible. |
| 1.7 | Session signed-out notice | UC-01 / Scenario C return | `You have been signed out.` | Shown when SPA arrives at `/login?reason=signed_out`. Auto-dismiss after 5s. |
| 1.8 | Consent denied at IS (UC-01 EX-1) | UC-01 EX-1 | `You did not approve the delegation. Sign in and approve to continue, or contact your administrator.` | Friendly error page after `error=access_denied` callback. Includes a `Try again` button (1.10). |
| 1.9 | State mismatch error (UC-01 EX-2) | UC-01 EX-2 | `The sign-in flow could not be completed. Please try again.` | Shown when `state` validation fails on `/auth/exchange`. Includes `Try again` button (1.10). |
| 1.10 | Retry sign-in button | UC-01 EX-1 / EX-2 | `Try again` | Returns user to step 1.3. Max 12 chars. |
| 1.11 | Configuration error (UC-01 EX-3) | UC-01 EX-3 | `Sign-in is temporarily unavailable. Please contact your administrator.` | Shown for `unauthorized_client` and other 5xx classes. No retry button — escalation only. |
| 1.12 | Identity server unreachable | user-experience.md §7.2 | `The identity server is not responding. Please try again in a moment.` | Shown when `<IS>/oauth2/authorize` cannot be reached. Includes `Try again` button. |
| 1.13 | Partial sign-out banner (UC-09 EX-5 — user cancelled at IS consent) | UC-09 EX-5 | `You have been signed out of this application. Note: your sign-in at the identity provider may still be active. To fully sign out everywhere, visit your organization's sign-out page or close your browser.` | Shown when SPA arrives at `/login?reason=signed_out_partial`. Distinct from 1.7. Informational styling (not error). Stage 4 BLOCK-E rewrite. |

---

## 2. App chrome — header, navigation, identity (UC-01 postcondition)

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 2.1 | Header — product name | UC-01 step 10 | `Smart Employee Assistant` | Left side of header. |
| 2.2 | Header — signed-in identity | UC-01 step 10 | `{user_display_name}` | Pulled from `id_token.name` or falls back to `id_token.preferred_username`. Truncate at 24 chars with ellipsis. |
| 2.3 | Header — sign-out button | Scenario C | `Sign out` | Triggers UC sign-out flow. Max 12 chars. |
| 2.4 | Header — connection healthy indicator (tooltip) | — | `Connected` | Tooltip on a small green dot when SSE channel is alive. |
| 2.5 | Header — connection degraded indicator (tooltip) | user-experience.md §7.2 | `Reconnecting…` | Tooltip on amber dot when SSE retry in progress. |
| 2.6 | Header — connection lost indicator (tooltip) | UC-05 EX-1 | `Disconnected. Trying to reconnect.` | Tooltip on red dot. |

---

## 3. Chat surface — composer and empty state (UC-02, UC-03)

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 3.1 | Composer placeholder | UC-02 trigger | `Ask about your leave, equipment, or team…` | Single line. Max 60 chars. Disappears on focus + first keystroke. |
| 3.2 | Composer placeholder — disabled (request in flight) | UC-03 EX-5 | `Waiting for the current request to finish…` | Shown while a CIBA flow is active. Composer is disabled. |
| 3.3 | Composer send button | UC-02 step 1 | `Send` | Visually a paper-plane icon with `Send` as accessible label. Max 8 chars. |
| 3.4 | Composer hint — keyboard | — | `Enter to send · Shift+Enter for a new line` | Small text below the composer. |
| 3.5 | Empty chat state — heading | UC-01 step 10 | `What can I help you with?` | First-load empty state. |
| 3.6 | Empty chat state — body | UC-01 step 10 | `I can check your leave balance, look up available equipment, and answer routine HR and IT questions. Each request will ask you to approve the agent that handles it.` | Sets expectation about per-agent consent up front. Max 240 chars. |
| 3.7 | Empty chat state — example chip 1 | — | `What is my leave balance?` | Click inserts into composer. |
| 3.8 | Empty chat state — example chip 2 | — | `What laptops are available?` | Click inserts into composer. |
| 3.9 | Empty chat state — example chip 3 | UC-03 trigger | `Show me my leave balance and what laptops are available.` | The headline-demo prompt. |
| 3.10 | No-specialists-configured state | EX (ops) | `No specialist agents are connected. Ask your administrator to configure HR Agent or IT Agent before continuing.` | Shown when orchestrator's agent registry is empty. Replaces 3.5/3.6. |
| 3.11 | Composer character counter (over limit) | — | `{n}/2000` | Shown when message > 1800 chars. Red at 2000. |
| 3.12 | Composer over-limit toast | — | `Messages are limited to 2000 characters.` | Toast on submit attempt over limit. |

---

## 4. Routing notifications (UC-02 step 3, UC-03 step 4)

These render as a transient, dimmed line above the assistant response area. They are replaced as the flow advances.

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 4.1 | Routing — single specialist | UC-02 step 3 | `Routing to {agent_label}…` | `agent_label` ∈ {`HR Agent`, `IT Agent`}. Shown for ~1–3s before the consent widget appears. |
| 4.2 | Routing — first of two | UC-03 step 3 | `Routing to {agent_label} first…` | Used when the orchestrator has planned a two-specialist serial flow. |
| 4.3 | Routing — second of two | UC-03 step 4 | `Now routing to {agent_label}…` | Shown after the first specialist's reply has rendered, before the second consent widget appears. |
| 4.4 | Routing — composing final reply | UC-03 step 6 | `Composing your answer…` | Shown briefly while the orchestrator's LLM merges multi-specialist outputs. |
| 4.5 | Routing — fallback (no specialist matched) | UC-02 EX-1 | `I'm thinking…` | Used by the deterministic keyword router while it inspects the message. |

---

## 5. Consent Widget — fresh authorization states (UC-02, UC-03, UC-04, UC-05)

The widget appears in the assistant panel above the composer. It is non-modal. See `consent-widget-spec.md` §2 for layout. Action text is mapped from scope by the SPA.

### 5.A Scope-to-action map (SPA-local, per consent-widget-spec.md §2)

| Scope | Action text |
|---|---|
| `hr_basic_rest` | `View company leave policy and holidays` |
| `hr_self_rest` | `View your leave balance and requests` |
| `hr_read_rest` | `View all employee leave requests` |
| `hr_approve_rest` | `Approve or reject a leave request on your behalf` |
| `it_assets_read_rest` | `Look up available IT equipment` |
| `it_assets_write_rest` | `Issue IT assets to employees` |

If a scope arrives that is not in the map, render: `Perform an action on your behalf` and log a warning (do not show the raw scope to the user).

### 5.B State-by-state copy

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 5.1 | AWAITING_APPROVAL — card title | UC-02 step 11 | `Action requires your approval` | Card heading. Static. Max 40 chars. |
| 5.2 | AWAITING_APPROVAL — agent line | UC-02 step 11 | `{agent_label}` | Rendered with the agent's color icon (HR=teal, IT=purple). Never the client_id. |
| 5.3 | AWAITING_APPROVAL — action line label | UC-02 step 11 | `Wants to:` | Static prefix. |
| 5.4 | AWAITING_APPROVAL — action line text | UC-02 step 11 | `{action_text}` | From 5.A map. Sentence case, no trailing period. |
| 5.5 | AWAITING_APPROVAL — binding code label | consent-widget-spec.md §2 | `binding code: {short_request_id}` | `short_request_id` = first 8 chars of `request_id`. Mono font. Muted color. |
| 5.6 | AWAITING_APPROVAL — Approve button | UC-02 step 12 | `Approve` | Primary. Max 12 chars. |
| 5.7 | AWAITING_APPROVAL — Deny button | UC-04 trigger | `Deny` | Secondary, same size as Approve. Max 12 chars. |
| 5.8 | AWAITING_APPROVAL — countdown | consent-widget-spec.md §2 | `⏱ expires {mm:ss}` | Live countdown. At T-60s, color shifts to amber. |
| 5.9 | AWAITING_APPROVAL — countdown amber suffix | — | `⏱ expires {mm:ss} — almost out of time` | Replaces 5.8 when remaining ≤ 60s. |
| 5.10 | AWAITING_APPROVAL — explainer footer | UC-03 narrative ("feature, not bug") | `Each agent asks for its own approval. Your identity provider records every consent.` | Small footer text inside the card. Builds trust during demo. Max 100 chars. |
| 5.11 | VERIFYING — slim row text | consent-widget-spec.md §3 | `Verifying with your identity provider…` | Replaces the card on Approve click. |
| 5.12 | VERIFYING — Cancel link | consent-widget-spec.md §3 | `Cancel` | Inline secondary link on the slim row. Triggers `POST /api/ciba/cancel`. |
| 5.13 | WORKING — slim row text | consent-widget-spec.md §3 | `{agent_label} is {action_gerund}…` | `action_gerund` mapped from the same scope dictionary. See 5.C below. |
| 5.14 | WORKING — Cancel link | consent-widget-spec.md §3 | `Cancel` | Same as 5.12. |
| 5.15 | DONE — transcript line | consent-widget-spec.md §3 | `✓ {agent_label} — completed` | The card fades into the chat transcript. Persistent. |
| 5.16 | DENIED — transcript line | UC-04 design notes | `⊘ Declined — {agent_label} will not run for this request` | The card collapses to this dimmed transcript line. Persistent. |
| 5.17 | EXPIRED — card title | UC-02 EX-6 | `Approval window expired` | Card persists with amber outline. |
| 5.18 | EXPIRED — body | UC-02 EX-6 | `You did not approve {agent_label} in time. You can ask again.` | Two-sentence body. |
| 5.19 | EXPIRED — Retry button | UC-02 EX-6 | `Ask again` | Re-submits the original user message. Max 12 chars. |
| 5.20 | EXPIRED — Dismiss button | UC-02 EX-6 | `Dismiss` | Collapses the card without retrying. Max 12 chars. |
| 5.21 | ERROR — card title | UC-02 EX-3 / EX-7 / EX-8 | `Something went wrong` | Card persists with red outline. |
| 5.22 | ERROR — body (generic) | UC-02 EX-3 | `{agent_label} could not complete this request. Reference: {short_auth_req_id}.` | `short_auth_req_id` = first 8 chars. Reference shown so support can correlate. |
| 5.23 | ERROR — body (backend down) | UC-02 EX-8 | `The system {agent_label} relies on is not responding. Please try again in a moment.` | Used when specialist returns `backend_unavailable`. |
| 5.24 | ERROR — body (misconfigured agent) | UC-02 EX-3 | `{agent_label} is not configured correctly. Please contact your administrator.` | Used for `unauthorized_client`, `invalid_request`. |
| 5.25 | ERROR — body (cross-aud / config collision) | UC-02 EX-7 | `{agent_label} could not authorize this action. Please contact your administrator.` | Used for `aud` mismatch and `act.sub` mismatch (D-1, D-2, D-10). |
| 5.26 | ERROR — Retry button | UC-02 EX-3 | `Try again` | Re-submits the original user message. |
| 5.27 | ERROR — Cancel button | UC-02 EX-3 | `Cancel` | Collapses the card without retrying. |

### 5.C Scope-to-gerund map (used in WORKING state, 5.13)

| Scope | Gerund phrase |
|---|---|
| `hr_basic_rest` | `looking up leave policy` |
| `hr_self_rest` | `checking your leave balance` |
| `hr_read_rest` | `reviewing employee leave requests` |
| `hr_approve_rest` | `approving the leave request` |
| `it_assets_read_rest` | `looking up available equipment` |
| `it_assets_write_rest` | `issuing the IT asset` |

Fallback: `working on it`.

---

## 6. Consent Widget — Session Refresh variant (UC-06)

Distinct visual treatment per consent-widget-spec.md §4. Amber banner accent. Triggered when an OBO token has expired and a new query needs the same specialist.

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 6.1 | SESSION_REFRESH — card title | UC-06 step 10 | `Session refresh` | Heading. Replaces "Action requires your approval". |
| 6.2 | SESSION_REFRESH — banner line | UC-06 step 10 | `↻ {agent_label}'s previous access has expired` | Amber banner inside the card. |
| 6.3 | SESSION_REFRESH — prior consent timestamp | UC-06 step 10 | `You approved this {humanized_duration} ago.` | `humanized_duration` ∈ {`a moment`, `{n} minutes`, `{h} hours`, `{h} hours {m} minutes`, `over a day`}. Singular/plural handled. |
| 6.4 | SESSION_REFRESH — action line label | UC-06 step 10 | `Wants to:` | Same as 5.3. |
| 6.5 | SESSION_REFRESH — action line text | UC-06 step 10 | `{action_text}` | Same scope-to-action map as 5.A. |
| 6.6 | SESSION_REFRESH — primary button | UC-06 step 11 | `Re-approve` | Replaces "Approve". Max 14 chars. |
| 6.7 | SESSION_REFRESH — secondary button | UC-06 EX-1 | `Skip` | Replaces "Deny". Max 8 chars. |
| 6.8 | SESSION_REFRESH — countdown | — | `⏱ expires {mm:ss}` | Same as 5.8. |
| 6.9 | SESSION_REFRESH — explainer footer | UC-06 design notes | `Approving this gives {agent_label} access for another hour.` | Sets expectation about TTL. Max 80 chars. |

### 6.A Mid-flight resume variant (UC-05 EX-2 — user closed tab and reopened)

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 6.10 | RESUMING — banner line | UC-05 EX-2 | `Resuming previous request — {mm:ss} left to approve.` | Amber banner. Replaces 5.8/5.9 on the resumed widget. |

---

## 7. Final reply patterns — orchestrator-composed (UC-02, UC-03, UC-04)

These are **dynamic templates** the orchestrator's LLM is prompted to produce. The deck specifies the canonical phrasing and the rules; the prompt template lives in `orchestrator/prompts/` (Stage 4).

### 7.A Single-specialist reply (UC-02)

| # | Pattern | UC ref | String / template | Notes |
|---|---|---|---|---|
| 7.1 | HR — leave balance | UC-02 step 17 | `You have {n} days of leave remaining.` | `n` is integer ≥ 0. If n == 0 → `You have no leave remaining.` If n == 1 → `You have 1 day of leave remaining.` |
| 7.2 | HR — no leave records | UC-02 result variant | `I could not find any leave records for you. If that seems wrong, contact HR.` | Used when `hr_server` returns an empty record. |
| 7.3 | IT — laptop list | UC-02 step 17 | `Available laptops: {comma_separated_list}.` | List rendered as readable comma list, oxford comma. e.g., `MBP-14, MBP-16, and XPS-13.` |
| 7.4 | IT — no laptops available | UC-02 result variant | `No laptops are currently available. I can check again later if you ask.` | Used for empty inventory. |
| 7.5 | Generic — single tool result | UC-02 step 17 | `{natural_language_summary_of_tool_output}.` | LLM-composed. Template constraint: one sentence, no preamble like "Sure, here is…". |

### 7.B Two-specialist combined reply (UC-03)

| # | Pattern | UC ref | String / template | Notes |
|---|---|---|---|---|
| 7.6 | HR + IT — both succeeded | UC-03 step 6 | `You have {n} days of leave remaining. Available laptops: {laptops}.` | LLM may rephrase as: `You have {n} days of leave remaining, and the available laptops are {laptops}.` |
| 7.7 | HR + IT — both succeeded, generic | UC-03 step 6 | `{hr_sentence} {it_sentence}` | Both sentences stand alone; LLM joins them with a single space. |

### 7.C Partial-result reply on denial (UC-04)

| # | Pattern | UC ref | String / template | Notes |
|---|---|---|---|---|
| 7.8 | Single-specialist deny | UC-04 EX-1 | `I could not access {domain} information for you because you declined the authorization. Ask again if you would like to retry.` | `domain` ∈ {`HR`, `IT`, `directory`}. |
| 7.9 | Two-specialist, first denied (HR denied) | UC-04 EX-2 | `Request cancelled — you declined HR access. Ask again if you would like to retry.` | Per UC-04 EX-2 default: abort whole request on first denial. |
| 7.10 | Two-specialist, second denied (IT denied after HR ok) | UC-04 EX-3 | `You have {n} days of leave remaining. I could not pull asset information because you declined IT access.` | The HR result is preserved verbatim from 7.1. |
| 7.11 | All denied | UC-04 EX-2 (variant) | `Request cancelled — you declined the requested access.` | Used when user denies every consent in a multi-specialist plan. |

### 7.D Partial-result reply on error / timeout (UC-02 EX-6, EX-8; UC-03 EX-4)

| # | Pattern | UC ref | String / template | Notes |
|---|---|---|---|---|
| 7.12 | Approval timeout | UC-02 EX-6 | `You did not approve {agent_label} in time. Ask again when you are ready.` | Used after EXPIRED state. |
| 7.13 | Specialist backend down — single | UC-02 EX-8 | `The {domain} system is unavailable right now. Please try again in a moment.` | `domain` ∈ {`HR`, `IT`, `directory`}. |
| 7.14 | Specialist backend down — combined (HR ok, IT down) | UC-03 EX-4 | `You have {n} days of leave remaining. I could not reach the IT system; try again in a moment.` | |
| 7.15 | Authorization failed at specialist | UC-02 EX-2 | `Authorization failed at {agent_label}. Please contact your administrator.` | Used for peer-trust / A2A validation failures (N4, N7). |
| 7.16 | LLM hallucinated tool / no capability | UC-02 EX-1, D-6 | `I do not have a way to do that yet.` | Recovery from the LLM's structured tool-not-found error. Single sentence. |
| 7.17 | Permission missing for user | user-experience.md §7.4 | `I do not have permission to look up {domain} information for you. If that seems wrong, contact your administrator.` | Used when the user's role does not grant the needed scope. Note: this is detected at the agent / IS layer, not by the orchestrator inspecting the user's role directly. |
| 7.18 | Identity server degraded | user-experience.md §7.2 | `The identity service is degraded right now. Please try again in a moment.` | Used when CIBA initiation itself fails (not user-driven). |
| 7.19 | Skip on Session Refresh | UC-06 EX-1 | `Request cancelled — your access expired and you chose not to refresh. Ask again when you are ready.` | |
| 7.20 | Generic unrecoverable error | catch-all | `Something went wrong handling your request. Reference: {short_request_id}.` | Used when no more specific copy applies. Always include the short request ID. |

---

## 8. Sign-out flow (Scenario C, Sprint 3 prep)

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 8.1 | Sign-out confirmation dialog title | Scenario C | `Sign out?` | Modal title. |
| 8.2 | Sign-out confirmation dialog body | Scenario C | `You will be signed out of the assistant and any agents that acted on your behalf will lose their access.` | Sets the right mental model for revocation. Max 160 chars. |
| 8.3 | Sign-out confirmation — primary | Scenario C | `Sign out` | Primary destructive button. Max 12 chars. |
| 8.4 | Sign-out confirmation — secondary | Scenario C | `Stay signed in` | Cancels the sign-out. Max 16 chars. |
| 8.5 | Sign-out in-progress notice (phase 1, during cascade) | UC-09 step 1 | `Revoking access for all agents…` | Action-grounded; mirrors the multi-step nature. Stage 4 FIX-14. Replaces earlier "Signing you out…". Spinner duration ≤2 s. |
| 8.6 | Sign-out completed banner (on sign-in page) | UC-09 step 14 | `You have been signed out.` | Same as 1.7. |
| 8.7 | Sign-out — pending CIBA warning | Scenario C "in flight" | `An approval is in progress. Sign out anyway?` | Replaces 8.2 when there is a live consent widget. |
| 8.8 | Sign-out — pending CIBA primary | Scenario C "in flight" | `Sign out and cancel approval` | Max 32 chars. |
| 8.9 | Sign-out in-progress notice (phase 2, before IS redirect) | UC-09 step 10 | `Redirecting to complete sign-out at your identity provider…` | Sets context for the IS consent screen transition (Q3 spec-pure path). Mirrors the established 1.4 redirect pattern. Stage 4 BLOCK-E + FIX-14. Spinner duration ~200 ms. |
| 8.10 | Sign-out error banner | UC-09 EX-6 | `Sign-out could not be completed right now. Close your browser to end your session, or try again.` | Shown when `POST /auth/logout` times out (10 s) or returns 5xx. Amber/warning styling. Stage 4 FIX-16. |
| 8.11 | Admin-terminate banner (online user) | UC-10 step 6 | `Your session has ended. Sign in again to continue.` | Shown when SPA arrives at `/login?reason=admin_terminated`. Amber styling but neutral copy (no attribution of intent — cause lives in admin audit log). Stage 4 FIX-15. |

---

## 9. Toast and banner copy — connection and infrastructure

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 9.1 | Toast — connection lost | UC-05 EX-1 | `Connection lost. Trying to reconnect…` | Auto-shown when SSE drops. Persists until reconnected. |
| 9.2 | Toast — reconnected | UC-05 EX-2 | `Reconnected.` | Auto-shown for 3s after SSE comes back. |
| 9.3 | Toast — reconnect failing | UC-05 EX-1 | `Could not reconnect. Refresh the page to try again.` | Shown after 3 failed reconnect attempts. |
| 9.4 | Banner — service degraded | user-experience.md §7.2 | `The identity service is responding slowly. Some actions may take longer than usual.` | Persistent banner near top of chat. Dismissible. |
| 9.5 | Banner — service down | user-experience.md §7.2 | `The identity service is unavailable. New requests cannot be processed right now.` | Persistent banner. Disables the composer (use 3.2 placeholder when this banner is up). |
| 9.6 | Toast — request submitted while disconnected | UC-05 EX-1 | `You appear to be offline. Your message will be sent when the connection is restored.` | Used when the user hits Send with SSE down. |
| 9.7 | Toast — message rejected (rate-limited) | future | `Too many requests. Please wait a moment before trying again.` | Reserved for Sprint 2+. |
| 9.8 | Toast — copied to clipboard | utility | `Copied.` | Used when the user copies a request reference (e.g., from 5.22). |
| 9.9 | Banner — admin maintenance | future | `Scheduled maintenance: the assistant will be briefly unavailable on {date} at {time}.` | Reserved for Sprint 2+. Static announcement. |

---

## 10. Accessibility labels and screen-reader-only strings

These are not visible but are spoken or announced by assistive tech. They follow the same voice rules.

| # | Surface / state | UC ref | String | Notes |
|---|---|---|---|---|
| 10.1 | Live region — routing announcement | UC-02 step 3 | `Routing your request to {agent_label}.` | `aria-live=polite`. |
| 10.2 | Live region — consent widget appeared | UC-02 step 11 | `An approval is required. {agent_label} wants to {action_text}. Approve or deny.` | `aria-live=assertive`. |
| 10.3 | Live region — verifying | consent-widget-spec.md §3 | `Verifying with your identity provider.` | `aria-live=polite`. |
| 10.4 | Live region — working | consent-widget-spec.md §3 | `{agent_label} is {action_gerund}.` | `aria-live=polite`. |
| 10.5 | Live region — done | consent-widget-spec.md §3 | `{agent_label} completed.` | `aria-live=polite`. |
| 10.6 | Live region — denied | UC-04 | `{agent_label} declined. The request will continue without it.` | `aria-live=polite`. |
| 10.7 | Live region — expired | UC-02 EX-6 | `The approval window expired.` | `aria-live=assertive`. |
| 10.8 | Live region — error | UC-02 EX-3 | `An error occurred. Reference {short_auth_req_id}.` | `aria-live=assertive`. |
| 10.9 | Connection indicator — `aria-label` healthy | — | `Connected to the assistant.` | |
| 10.10 | Connection indicator — `aria-label` reconnecting | — | `Reconnecting to the assistant.` | |
| 10.11 | Connection indicator — `aria-label` lost | — | `Disconnected from the assistant.` | |
| 10.12 | Composer — `aria-label` | — | `Message the assistant.` | |
| 10.13 | Send button — `aria-label` | — | `Send message.` | |
| 10.14 | Live region — sign-out in progress | UC-09 step 1 | `Signing out. Please wait.` | `aria-live=polite`. Stage 4 NIT-9. |
| 10.15 | Live region — sign-out / admin-terminate completed (login banner on arrival) | UC-09 step 14 / UC-10 step 6 | `You have been signed out.` (signed_out) / `You have been signed out of this application.` (signed_out_partial) / `Your session has ended.` (admin_terminated) | `aria-live=assertive`. Banner replaces page heading on `?reason=…` arrival; `assertive` is less disruptive than programmatic focus-stealing. Stage 4 NIT-9. |

---

## 11. Specialist agent display names and colors (canonical)

These are the only display names allowed in the SPA. Listed here so consumers do not invent variants.

| `agent_id` | `agent_label` (display) | Color token | Icon |
|---|---|---|---|
| `orchestrator-agent` | `Orchestrator Agent` | `--color-orchestrator` (slate) | speech-bubble |
| `hr_agent` | `HR Agent` | `--color-hr` (teal) | clipboard |
| `it_agent` | `IT Agent` | `--color-it` (purple) | laptop |

Anti-pattern (NEVER render): the OAuth client_id, the internal hostname, "the HR bot", "HR API".

---

## 12. Domain labels (used in 7.8, 7.13, 7.17)

| `agent_id` | `domain` (lowercase, in-sentence) |
|---|---|
| `hr_agent` | `HR` |
| `it_agent` | `IT` |
| `directory-agent` | `directory` |

Used in copy like `I could not access {domain} information…`.

---

## 13. Humanized duration formatter (used in 6.3)

Source: a pure SPA-side helper. Inputs: `now - prior_iat` in seconds.

| Range | Output |
|---|---|
| `< 60s` | `a moment` |
| `60s ≤ x < 120s` | `1 minute` |
| `120s ≤ x < 3600s` | `{n} minutes` (n = floor(x/60)) |
| `3600s ≤ x < 7200s` | `1 hour` |
| `7200s ≤ x < 86400s` | `{h} hours` (or `{h} hours {m} minutes` if `m ≥ 1`) |
| `≥ 86400s` | `over a day` |

Examples: `45s` → `a moment`; `72m` → `1 hour 12 minutes`; `4h 5m` → `4 hours 5 minutes`.

---

## 14. Red-line strings — never appear in the UI

Per user-experience.md §8, the following must never be rendered to the user. Reviewers should grep for these patterns in PRs.

- Any string matching `eyJ[A-Za-z0-9_-]{10,}\.` (JWT-shaped).
- Any of the literal client_ids registered in WSO2 IS.
- Hostnames from the internal service mesh: `hr_agent:`, `it_agent:`, `hr_server:`, `it_server:`, `:8001`, `:8002`, `:8003`, `:8004`.
- The strings `OAuth`, `CIBA`, `JWT`, `JWKS`, `act.sub`, `aud`, `iss`, `scope` — in user-facing chat or widget copy. (They are fine in developer-facing logs and in the README.)
- Stack-trace fragments: `Traceback`, `at <anonymous>`, `Error: `, `TypeError`, `ValueError`.
- The literal scope strings (`hr_basic_rest`, `hr_self_rest`, `hr_read_rest`, `hr_approve_rest`, `it_assets_read_rest`, `it_assets_write_rest`) — always render via the action map (5.A) or gerund map (5.C). Never expose the underscore-delimited scope string to the user.
- `localhost:`, `127.0.0.1`, `13.60.190.47` (or any IP literal).
- The word `token` in chat copy. Use `access` or `permission`. Exception: the sign-out dialog (8.2) may say "lose their access".

---

## 15. Change log

| Date | Version | Author | Notes |
|---|---|---|---|
| 2026-05-07 | 0.1 | Stage 3 | Initial deck covering Sprint 1 UCs 01–06, plus Sprint 3 sign-out prep. |

---

## 16. Open questions for Stage 4 / Sprint 2

These are not blockers for Sprint 1 implementation but should be resolved before Sprint 2 polish lands.

- 16.1 — When the SPA shows the resume variant (6.10), does the original countdown text disappear entirely, or sit beneath the resume banner? Current spec: resume banner replaces the countdown. Confirm with stage rehearsal.
- 16.2 — For the "permission missing" copy (7.17), does the orchestrator know enough to compose this without a redirect through the specialist? Architecture decision pending.
- 16.3 — UC-04 EX-2 default is "abort whole request on first denial". If stakeholder feedback in Sprint 2 says continue, copy 7.9 needs a sibling for "HR denied, continued with IT".
- 16.4 — The "consent fatigue moderated bar" (N27) may surface a need for one extra sentence in 5.10. Defer until Sprint 2 user testing.
