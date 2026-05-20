# Sprint 4 — Stage 4: UX Design

**Stage:** 4 (UX design — locked after document review)
**Date:** 2026-05-10
**Branch (entry):** `sprint-3-build` @ `b497616`
**Read order:** [`sprint-4.md`](sprint-4.md) (binding) → this doc → Stage 5 (API design)
**Supersedes:** nothing. First UX doc for Sprint 4.

---

## §1. Goal and scope summary

Sprint 4 requires four new UI surfaces on top of the existing single-page application (vanilla JS, no build step):

1. **Reports page** — a dedicated top-level page for HR Admin, containing three tabs: Pending Leaves, Cubicles, Devices. Option B (separate page) is **confirmed and locked** per `sprint-4.md` §8 and the PM brief §8. Option A (inline in chat) is closed.
2. **My Leaves panel** — a persistent panel on the home page, visible to all logged-in users, showing the authenticated user's own leave requests. Fetches from `GET /api/me/leaves` on first paint and re-fetches when a `chat_message` SSE event settles.
3. **Multi-turn cubicle chat** — all four turns of the UC-11 cubicle-assignment workflow are plain chat. No new widget between turns. The consent widget appears only at turn 3 when `hr_assets_write_rest` fires.
4. **Consent-widget action-text variants** — Sprint 4 adds five new `(scope, tool)` action texts to the existing `SCOPE_ACTION_MAP` in `client/app.js:122` and extends the SSE `ciba_url` event payload with an `action_text` field so the SPA does not have to reconstruct parameterised strings (cubicle IDs, usernames) from the raw `binding_message`.

The home page is redefined as: chat surface (existing) + My Leaves panel (new, below chat) + "View Reports" link for HR Admin only. The Reports page is a separate route (`/reports`). All changes are additive — no existing chat or widget functionality is altered.

---

## §2. Information architecture

### Navigation map

```
/ (home)
  |-- chat surface (existing)
  |-- My Leaves panel (NEW — all logged-in users)
  `-- [View Reports ->] link   (HR Admin only — conditionally rendered)

/reports                       (HR Admin only — hidden for Employee role)
  |-- Pending Leaves tab
  |-- Cubicles tab
  `-- Devices tab
```

**Route-level access rules:**

| Route | Employee | HR Admin | Notes |
|---|---|---|---|
| `/` | Visible | Visible | Home page for both roles. |
| `/reports` | Hidden (no nav entry) | Visible | If Employee URL-fuzzes to `/reports`, each API call returns 403 from orchestrator pre-flight. SPA renders 403 panel (see §4). |

**Navigation surface decision:** Sprint 4 does NOT add a persistent top-level `<nav>` bar to avoid restructuring `index.html` beyond the minimum. Instead:
- The header gains a **"Reports" button** (`class="btn-ghost btn-sm"`, same pattern as existing "Trace" and "Sign out" buttons) rendered only when the user's scope set includes `hr_read_rest`. This is determined from the `user_scopes` field added to the `GET /auth/exchange` response in Stage 6.
- The My Leaves panel contains a "View Reports" text link for HR Admin (same narrow inline approach; no separate breadcrumb or sidebar).

**Rationale for no persistent nav bar:** the SPA is intentionally minimal (no build step, no router library). A two-item nav bar adds considerable structural overhead for one additional route. The header button and panel link together are sufficient for the demo scope.

---

## §3. Home page wireframe

```
+----------------------------------------------------------------------+
| Smart Employee Assistant          [Trace N] [hr_admin_user] [Sign out]|
+----------------------------------------------------------------------+
| [service banner — hidden unless degraded]                            |
+----------------------------------------------------------------------+
|                                                                      |
|  CHAT TRANSCRIPT                                             (scroll)|
|  ___________________________________________________________________  |
|  |                                                                  | |
|  |  What can I help you with?                                       | |
|  |  I can check your leave balance, look up available               | |
|  |  equipment, and answer routine HR and IT questions.              | |
|  |  Each request will ask you to approve the agent                  | |
|  |  that handles it.                                                | |
|  |  [What is my leave balance?] [What laptops are available?]       | |
|  |_________________________________________________________________ | |
|                                                                      |
|  CONSENT WIDGET (slides in above composer when active)               |
|                                                                      |
|  __________________________________________________________________ |
|  CHIPS: [What is my leave balance?] [What laptops are available?]..  |
|  +----------------------------------------------------------------+ |
|  | Ask about your leave, equipment, or team...          [->SEND]  | |
|  +----------------------------------------------------------------+ |
|  Enter to send . Shift+Enter for new line                            |
|                                                                      |
+----------------------------------------------------------------------+
|                                                                      |
|  MY LEAVES                                    [View Reports ->]      |
|  (HR Admin only: View Reports link appears here.                     |
|   Employee: View Reports link absent.)                               |
|                                                                      |
|  +-------+----------+-----------+--------+-----------+----------+   |
|  | Leave | Type     | Start     | End    | Days      | Status   |   |
|  | ID    |          | Date      | Date   |           |          |   |
|  +-------+----------+-----------+--------+-----------+----------+   |
|  | LR-01 | Annual   | 2026-06-10| Jun 14 | 5         | [Pending]|   |
|  | LR-02 | Sick     | 2026-05-02| May 03 | 2         |[Approved]|   |
|  +-------+----------+-----------+--------+-----------+----------+   |
|                                                                      |
|  Empty state (when no leaves):                                       |
|  "You have no leave requests yet. Ask the HR agent to apply          |
|   for one."                                                          |
|                                                                      |
+----------------------------------------------------------------------+
```

**Panel placement decision:** My Leaves panel sits below the chat composer, not beside it. Reasoning: the existing layout is a single vertical column; inserting a panel beside the chat would require a two-column grid, touching `#chat-main` layout and the composer `max-width` constraint. Below-the-fold placement requires no layout restructuring. The panel is always rendered in the DOM; its content loads asynchronously on first paint.

**Panel columns:** Request ID | Type | Start Date | End Date | Days | Status.
Default sort: `start_date` descending (most recent first), per UC-13 design note. Column headers are `<th scope="col">` with `role="button"` click-to-sort toggle. Active sort column gets an arrow indicator (`+` / `-` appended as text, no icon font dependency).

**Status pill colour mapping:**

| Status value | Background | Text colour | CSS class |
|---|---|---|---|
| `Pending` | `var(--warning-soft)` (#fffbeb) | `#92400e` | `.pill--pending` |
| `Approved` | `var(--success-soft)` (#f0fdf4) | `var(--success)` (#16a34a) | `.pill--approved` |
| `Rejected` | `var(--danger-soft)` (#fef2f2) | `var(--danger)` (#dc2626) | `.pill--rejected` |

Status pills always render the text label (not colour-only). Non-colour signalling is the text itself; the colour is supplementary. CSS recipe for the pill:

```css
.status-pill {
  display: inline-block;
  padding: 0.2rem 0.5rem;
  border-radius: 20px;
  font-size: 0.78rem;
  font-weight: 500;
  white-space: nowrap;
}
.pill--pending  { background: var(--warning-soft); color: #92400e; }
.pill--approved { background: var(--success-soft); color: var(--success); }
.pill--rejected { background: var(--danger-soft);  color: var(--danger); }
```

**"View Reports" link:** rendered only when the session includes `hr_read_rest` scope. Plain `<a href="/reports">` anchor styled as `var(--primary)` text link (matching existing `.btn-link` pattern). Placed in the panel header row, right-aligned. Not a button — navigation, not an action.

**Loading state:** on first paint the panel shows two skeleton rows (grey animated bars, same approach as the existing chat empty state fade-in) while `GET /api/me/leaves` is in-flight. If the request fails, the panel shows: "Could not load your leave requests. Try refreshing the page."

**Re-fetch trigger:** the SPA re-fetches `/api/me/leaves` when a `chat_message` SSE event settles and `requestInFlight` transitions from `true` to `false` (i.e., in `onChatMessageEvent` at `client/app.js:795`). This avoids polling; the panel updates passively. No heuristic about whether the message "looks leave-related" — always re-fetch on chat settle. Cost is one cheap GET per chat turn; acceptable for a demo.

---

## §4. Reports page wireframe

### Route and rendering

The SPA intercepts navigation via `window.location.pathname`. At `init()` time, if `pathname === "/reports"`, the SPA hides the home page and renders the Reports page. The Reports page is a new `<div id="reports-page">` sibling to `<main id="chat-main">`, not a separate HTML file. The tab system is vanilla horizontal tabs with `role="tablist"` / `role="tab"` / `role="tabpanel"` ARIA.

```
+----------------------------------------------------------------------+
| Smart Employee Assistant          [Trace N] [hr_admin_user] [Sign out]|
+----------------------------------------------------------------------+
| [service banner — hidden unless degraded]                            |
+----------------------------------------------------------------------+
|                                                                      |
|  <- Back to home                            Reports                  |
|                                                                      |
|  [Pending Leaves (3)] [Cubicles (10)] [Devices (7)]                  |
|   ~~~~~~~~~~~~~~~~~~~~                                               |
|   (active tab underlined with var(--primary), 2px solid)            |
|                                                                      |
|  ------------------------------------------------------------------- |
|  TAB CONTENT AREA                                                    |
|  ------------------------------------------------------------------- |
|                                                                      |
+----------------------------------------------------------------------+
```

"Back to home" is a plain `<a href="/">` text link, styled as muted text with a left-arrow character (`<`). No router — `href="/"` causes a full page reload which is acceptable given the SPA's vanilla-JS pattern.

Tab count badges show the current row count from the last successful fetch. Badge is a plain `<span class="tab-count">` (same muted border-radius pill as `trace-toggle-count` in the header — `client/styles.css` pattern).

---

### §4.1 Pending Leaves tab

```
+----------------------------------------------------------------------+
|  Pending Leaves (3)                                                  |
|                                                                      |
|  +----------+-----------------------+----------+------+------+------+|
|  | Username | Email                 | Type     | Days |Start |Action||
|  +----------+-----------------------+----------+------+------+------+|
|  | jane.doe | jane.doe@example.com  | Annual   |  5   |Jun 10|[A][R]||
|  | bob.smith| bob.smith@example.com | Sick     |  2   |Jun 01|[A][R]||
|  | ali.x    | ali.x@example.com     | Personal |  1   |May 30|[A][R]||
|  +----------+-----------------------+----------+------+------+------+|
|                                                                      |
|  Empty state: "No pending leave requests."                           |
|  Error state: "The HR system is unavailable. Try again in a moment." |
|  Loading:      two skeleton rows (animated grey bars)                |
+----------------------------------------------------------------------+
```

**Full column list:** Username | Email | Leave Type | Days | Start Date | Actions.

Action buttons in the Actions column: `[A]` = Approve (green outline, `class="btn-outline btn-approve"`) and `[R]` = Reject (red outline, `class="btn-outline btn-reject"`). Button text is "Approve" and "Reject" (full words, not abbreviations — the wireframe uses abbreviations for space only). Both buttons are `<button type="button">` with `aria-label="Approve leave for {username}"` and `aria-label="Reject leave for {username}"` respectively.

**PUSHBACK on UC-15 §10 framing — approve via chat plumbing:** UC-15 §10 says Approve/Reject click should emit a "chat-style command" routed through the existing chat plumbing. This design document recommends against surfacing that command as a visible chat message in the transcript. Reasons:

1. The user is on the Reports page, not the chat page. A chat message appearing in a tab the user is not looking at is invisible and confusing.
2. It pollutes the chat transcript with internal commands ("Approve leave request LR-042 for jane.doe") that read as machine output, not user input.
3. The existing consent widget is agnostic to page context — it can appear on the Reports page just as it does on the chat page.

**Recommended design:** Approve/Reject buttons POST directly to a new orchestrator endpoint `POST /api/reports/leave-requests/{request_id}/approve` (or `/reject`), which the orchestrator handles by invoking HR Agent → CIBA on `hr_approve_rest` → same agent flow, but without injecting a chat message. The orchestrator SSE stream already connects to the Reports page (same session, same EventSource). The consent widget appears over the Reports page (same `#consent-widget` DOM element, floated above the page). On widget DONE: the Pending Leaves tab re-fetches and the row disappears. This is the clean path. Stage 5 locks the endpoint shape.

If Stage 5 decides the chat plumbing approach is mandatory for architecture reasons (to avoid adding new orchestrator handler code), the fallback is to emit the command as a **hidden message** (not appended to the transcript) and route it through the existing `POST /api/chat` endpoint with a flag `{internal: true, skip_transcript: true}`. The UX result is identical from the user's perspective. Stage 5 makes this call.

**Post-approval behaviour:** on consent widget DONE, SPA re-fetches the Pending Leaves tab silently. The approved row disappears. No explicit "row updated to Approved" state — the row is gone from the pending filter. If the admin denies the consent: toast message "Approval cancelled." appears (3 s auto-dismiss). Row stays.

---

### §4.2 Cubicles tab

```
+----------------------------------------------------------------------+
|  Cubicles (10)                                                       |
|                                                                      |
|  +----------+-----------------------+----------+-------+------------+|
|  | Username | Email                 | Cubicle  | Floor | Assigned At||
|  +----------+-----------------------+----------+-------+------------+|
|  | jane.doe | jane.doe@example.com  | C-027    |   2   | 2026-05-10 ||
|  | ...      | ...                   | ...      |  ...  | ...        ||
|  +----------+-----------------------+----------+-------+------------+|
|                                                                      |
|  Empty state: "No cubicles have been assigned yet."                  |
|  Error state: "The HR system is unavailable. Try again in a moment." |
|  Loading:     two skeleton rows                                      |
+----------------------------------------------------------------------+
```

Columns: Username | Email | Cubicle ID | Floor | Assigned At. Default sort: Floor ascending, then Cubicle ID alphabetically. Click-to-sort on all `<th>` headers. No drilldown on Cubicles rows (each employee has at most one cubicle; the row already contains the full data).

"Assigned At" renders as a short date: `YYYY-MM-DD` parsed from ISO 8601. No time component shown.

---

### §4.3 Devices tab

```
+----------------------------------------------------------------------+
|  Devices (7)                                                         |
|                                                                      |
|  Type: [All types v]  (client-side filter dropdown — no server call) |
|                                                                      |
|  +----------+-------------------+----------+--------+-------+-------+|
|  | Username | Email             | Asset ID | Type   | Model | Status||
|  +----------+-------------------+----------+--------+-------+-------+|
|  | jane.doe | jane.doe@example  | AST-12345| laptop | MBP 14| [Out] ||
|  > [drilldown row — all assets for jane.doe]                         |
|  |   Asset ID   | Type   | Model         | Status                   ||
|  |   AST-12345  | laptop | MBP 14 M3     | outstanding              ||
|  |   AST-99901  | headset| Sony WH-1000  | outstanding              ||
|  < [end drilldown]                                                   |
|  | bob.smith| bob.smith@example | AST-55621| phone  | iPhone | [Out] ||
|  +----------+-------------------+----------+--------+-------+-------+|
|                                                                      |
|  Empty state: "No IT assets have been assigned yet."                 |
|  Error state: "The IT system is unavailable. Try again in a moment."|
|  Loading:     two skeleton rows                                      |
+----------------------------------------------------------------------+
```

**Columns:** Username | Email | Asset ID | Type | Model | Status.

**Type filter:** a `<select>` dropdown with options: All types | laptop | phone | monitor | headset. Client-side filter only — `filter()` over the already-loaded data array. No server round-trip. The dropdown is placed above the table, left-aligned. No label text, but has `aria-label="Filter by asset type"`.

**Row drilldown:** clicking any row expands an inline sub-row immediately below the clicked row showing all assets for that employee. The sub-row is a nested `<table>` inside a `<td colspan="6">`. The affordance is a `>` chevron in the leftmost cell (prepended as a text character, no icon font). Expanded state flips it to `v`. Only one row can be expanded at a time (clicking a second row closes the first). This is client-side only — data is filtered from the already-loaded array by `employee_username`. No new API call.

The expand/collapse is implemented with a `<details>`-style toggle pattern using a `<button aria-expanded="false/true">` in the row. The sub-row is a `<tr class="device-drilldown-row" hidden>`.

**Status pill in Devices tab:** reuses the same `.status-pill` recipe from §3. Values are `outstanding` (map to Approved green) and `returned` (map to text-muted grey).

| Device status | Pill class | Label shown |
|---|---|---|
| `outstanding` | `.pill--approved` | Outstanding |
| `returned` | `.pill--muted` | Returned |

`.pill--muted` recipe: `{ background: var(--surface-alt); color: var(--text-muted); }`.

---

### §4.4 Reports page error states

**403 — non-admin access:**

```
+----------------------------------------------+
|                                              |
|   You do not have access to this page.       |
|                                              |
|   Reports are available to HR Admins only.  |
|   Sign in with an HR Admin account to        |
|   view employee leave and assignment data.   |
|                                              |
|            [Go to home page]                 |
|                                              |
+----------------------------------------------+
```

This panel renders as `<div role="alert">` (per WCAG, for 403 error messages that announce themselves to screen readers). Button links back to `/`. The panel appears inside `#reports-page` in place of the tab content area when any reporting endpoint returns 403.

**Loading state:** skeleton rows use a `@keyframes` animation (same approach as the existing `approve-pulse` keyframe in `client/styles.css:665`) — a horizontal shimmer across grey placeholder bars. Two skeleton rows per tab.

**Tab-level error (5xx / network):** the tab content area shows the per-system error string. Other tabs are unaffected.

---

## §5. Multi-turn cubicle chat — locked copy deck

All four turns use the existing chat surface. The conversation is not scripted at the SPA level; the copy below is what the orchestrator's HR Agent returns. These strings are the target output for the LLM prompt and the keyword-fallback route labels (`cubicle_summary`, `cubicle_floor_pick`, `cubicle_vacant_list`, `cubicle_assign`).

### Turn 1 — vacant summary

**Admin prompt (example):**
```
Show me vacant cubicles.
```

**HR Agent reply:**
```
Vacant cubicles by floor: Floor 1 — 23 of 25, Floor 2 — 25 of 25,
Floor 3 — 18 of 25, Floor 4 — 25 of 25.

Which floor would you like to pick from?
```

Note: the numbers above reflect UC-11 §"Turn 1" step 6 sample data. The actual numbers come from `hr_service.get_cubicle_summary()` at runtime. The phrase "Which floor would you like to pick from?" is the required prompt to drive turn 2.

**Empty state — all cubicles full (all floors at 0 vacant):**
```
All 100 cubicles are currently assigned. Cubicle reassign is
not available in this release. Please contact HR administration
to free up a cubicle before assigning a new one.
```

---

### Turn 2 — admin selects floor

**Admin prompt (example):**
```
Show me floor 2.
```

**HR Agent reply:**
```
Floor 2 has 25 vacant cubicles: C-026, C-027, C-028, C-029,
C-030, C-031, C-032, C-033, C-034, C-035, C-036, C-037,
C-038, C-039, C-040, C-041, C-042, C-043, C-044, C-045,
C-046, C-047, C-048, C-049, C-050.

Which cubicle would you like to assign, and to whom?
```

The phrase "Which cubicle would you like to assign, and to whom?" is required to prompt Turn 3. If the floor has zero vacant cubicles:
```
Floor 2 has no vacant cubicles. All 25 are currently assigned.
Try a different floor: Floor 1 has 23 vacant, Floor 3 has 18 vacant,
Floor 4 has 25 vacant.
```

---

### Turn 3 — admin picks cubicle and names employee

**Admin prompt (canonical form):**
```
Assign C-027 to jane.doe.
```

**Admin prompt (alternate with email):**
```
Assign C-027 to jane.doe@example.com.
```

At this turn the orchestrator routes to HR Agent `assign_cubicle` → CIBA fires on `hr_assets_write_rest`. The consent widget appears with action text (see §6 below). No assistant text message is sent between the admin's prompt and the widget appearing (the routing SSE line "Routing to HR Agent..." is shown as usual).

**Ambiguous Turn 3 — admin names a cubicle but omits the employee:**
```
Assign C-027.
```
HR Agent reply before CIBA:
```
Which employee should I assign C-027 to? Please provide their
username (e.g. jane.doe) or email address.
```

**Ambiguous Turn 3 — employee not found (UC-11 EX-3):**
```
I couldn't find an employee with username 'jane.doe'.
Please check the username or provide their email address.
```

**Denied consent (UC-11 EX-2):**
```
Cubicle assignment was not authorised. No change was made.
You can try again when ready.
```

**CIBA window expired (UC-11 EX-5):**
```
The authorisation window expired. Please ask again to retry
the cubicle assignment.
```

---

### Turn 4 — post-approval confirmation

**HR Agent reply (success):**
```
Cubicle C-027 on floor 2 has been assigned to jane.doe
(jane.doe@example.com).
```

No automatic navigation to the Reports page after assignment. The admin can manually navigate to Reports > Cubicles to confirm the row appears. The chat message is the only confirmation surface.

**TOCTOU race (UC-11 EX-1 — cubicle taken between turns 2 and 3):**
```
Cubicle C-027 was just assigned to bob.smith before your request
completed. Please pick a different cubicle from floor 2.
```

---

## §6. Consent-widget binding-message variants

### Design decision: extend the SSE `ciba_url` payload with `action_text`

The existing `ciba_url` SSE event (`client/app.js:694`) carries `scope` and `binding_message`. The SPA currently derives action text by calling `scopeToAction(scope)` which looks up a scope-keyed string from `SCOPE_ACTION_MAP` (`client/app.js:122`). This is adequate for generic strings ("View your leave balance and requests") but insufficient for parameterised admin-action strings like "Assign cubicle C-027 to jane.doe" where the cubicle ID and username are dynamic.

**Option A (not recommended):** parse `binding_message` in the SPA to extract parameters. Fragile — `binding_message` is an IS-contract field (RFC 9126 `binding_message`), not a structured data field. The SPA should not parse it.

**Option B (recommended and locked):** extend the SSE `ciba_url` event payload with an optional `action_text` field. When present, the SPA uses it directly instead of the scope lookup. When absent, the SPA falls back to `scopeToAction(scope)` as today. No breaking change.

**Implementation in `onCibaUrlEvent` (`client/app.js:694`):**

```javascript
// Current (line ~721):
const actionText = scopeToAction(scope);

// Sprint 4 change:
const actionText = event.action_text || scopeToAction(scope);
```

The orchestrator-side HR Agent sets `action_text` in the A2A consent-required response, and the orchestrator includes it verbatim in the SSE `ciba_url` event. The agent can construct `action_text` from the resolved tool arguments before CIBA is initiated.

### Action-text strings locked by (scope, tool)

These strings are the canonical values. Engineers copy them verbatim into the orchestrator's HR Agent and IT Agent action-text construction logic.

**Read-scope actions (neutral tint — no visual change from today):**

```
(hr_self_rest, get_my_cubicle)
  action_text: "View your cubicle assignment"
  gerund:      "looking up your cubicle"

(hr_self_rest, get_my_leave_requests)
  action_text: "View your leave requests"
  gerund:      "checking your leave requests"

(hr_apply_rest, apply_leave)
  action_text: "Apply for leave on your behalf"
  gerund:      "submitting your leave request"

(it_assets_self_rest, get_my_assets)
  action_text: "View your assigned IT equipment"
  gerund:      "looking up your assigned equipment"
```

Note: `hr_apply_rest` and `hr_self_rest` both map to a write (apply_leave is a write), but the CIBA is initiated on behalf of the employee requesting their own leave — this is self-service, not admin delegation. The widget uses neutral tint. This is consistent with the existing Sprint 1 convention.

**Write-scope actions requiring amber tint (admin delegation):**

```
(hr_assets_write_rest, assign_cubicle)
  action_text: "Assign cubicle {cubicle_id} to {employee_username}"
  example:     "Assign cubicle C-027 to jane.doe"
  gerund:      "assigning the cubicle"
  tint:        amber

(hr_approve_rest, approve_leave_request)
  action_text: "Approve {employee_username}'s leave from {start_date}"
  example:     "Approve jane.doe's leave from 2026-06-10"
  gerund:      "approving the leave request"
  tint:        amber

(hr_approve_rest, reject_leave_request)
  action_text: "Reject {employee_username}'s leave from {start_date}"
  example:     "Reject jane.doe's leave from 2026-06-10"
  gerund:      "rejecting the leave request"
  tint:        amber

(it_assets_write_rest, issue_asset)
  action_text: "Issue IT assets to employees"        [EXISTING — keep]
  gerund:      "issuing the IT asset"               [EXISTING — keep]
  tint:        amber
```

### Amber tint implementation

The amber tint convention from UC-07 is already in the codebase via `.cw-binding-message-row` (`client/styles.css:625`) — a warm left-border callout. For Sprint 4, the amber tint on the action line itself (not just the binding message row) is implemented by adding a modifier class to the consent widget when the `action_text` field is present on the SSE event AND the scope is a write scope.

Add a `data-write-action` attribute to the consent widget `<div>` when the widget renders with a write-scope action text. CSS:

```css
/* Write-action amber accent on the action line */
.consent-widget[data-write-action] .cw-action-text {
  color: var(--warning);   /* #d97706 */
  font-weight: 600;
}

.consent-widget[data-write-action] .cw-card {
  border-color: var(--amber-border);
}
```

The write-scope set is: `hr_assets_write_rest`, `hr_approve_rest`, `it_assets_write_rest`. Logic in `renderWidget()`:

```javascript
const isWriteScope = ["hr_assets_write_rest","hr_approve_rest","it_assets_write_rest"]
  .some(s => (scope || "").includes(s));
$("consent-widget").toggleAttribute("data-write-action", isWriteScope);
```

This does not alter the existing `consent-widget--awaiting` class or the existing amber-border on EXPIRED state. The `data-write-action` attribute only affects the action-text colour and the card border. No visual change for read scopes.

### Updated `SCOPE_ACTION_MAP` entries for Sprint 4

Add to `client/app.js:122`:

```javascript
"hr_apply_rest":        "Apply for leave on your behalf",
"hr_assets_write_rest": "Assign cubicle to employee",    // generic fallback
"it_assets_self_rest":  "View your assigned IT equipment",
```

The generic fallback strings are used only when `action_text` is absent from the SSE event (degraded path). The parameterised strings from the `action_text` field take precedence.

Gerund additions for `SCOPE_GERUND_MAP` at `client/app.js:133`:

```javascript
"hr_apply_rest":        "submitting your leave request",
"hr_assets_write_rest": "assigning the cubicle",
"it_assets_self_rest":  "looking up your assigned equipment",
```

---

## §7. Approve/Reject from the Pending Leaves table

Per UC-15 §10 and the design decision in §4.1 above.

### Click-to-consent flow

```
HR Admin on Reports / Pending Leaves tab
   |
   v
Clicks "Approve" on row for jane.doe (LR-042)
   |
   v
SPA: POST /api/reports/leave-requests/LR-042/approve
     Cookie: orchestrator_session=<sid>
   |
   v
Orchestrator:
  - Validates session
  - Invokes HR Agent internally (no transcript message)
  - HR Agent initiates CIBA: scope=hr_approve_rest
    binding_message="HR Agent wants to approve jane.doe's leave
                     from 2026-06-10 (corr-id: <rid>)"
    action_text="Approve jane.doe's leave from 2026-06-10"
   |
   v
SSE ciba_url event reaches SPA
  -> Consent widget appears (amber tint, write scope)
     Action text: "Approve jane.doe's leave from 2026-06-10"
   |
   +-- Admin clicks Approve -->
   |      IS consent screen -> admin confirms
   |      Token-C issued: scope=hr_approve_rest
   |      HR Server approve_leave_request runs
   |      SSE ciba_state_change: DONE
   |      SPA: re-fetch Pending Leaves tab (GET /api/reports/leave-requests?status=pending)
   |      Row for LR-042 disappears from table (moved out of pending)
   |
   +-- Admin clicks Deny -->
          IS returns access_denied
          HR Agent: ERR-CIBA-005
          SSE ciba_state_change: DENIED
          Consent widget collapses to DENIED transcript line
          Toast: "Approval cancelled." (3 s auto-dismiss)
          Row stays in the table
```

**Reject flow is identical** with `reject_leave_request` substituted.

**Button affordances:**

```css
.btn-approve {
  border: 1px solid var(--success);
  color: var(--success);
  background: transparent;
  padding: 0.25rem 0.6rem;
  border-radius: var(--radius-sm);
  font-size: 0.8rem;
}
.btn-approve:hover {
  background: var(--success-soft);
}

.btn-reject {
  border: 1px solid var(--danger);
  color: var(--danger);
  background: transparent;
  padding: 0.25rem 0.6rem;
  border-radius: var(--radius-sm);
  font-size: 0.8rem;
}
.btn-reject:hover {
  background: var(--danger-soft);
}
```

Buttons are `disabled` while a consent widget is active (set via `requestInFlight` equivalent on the Reports page). This prevents double-clicks from firing two CIBA flows simultaneously.

**Toast copy for denied consent:**
```
approvalCancelled: "Approval cancelled.",
```
Add to `COPY` object in `client/app.js`.

---

## §8. Visual language continuity

### Colour and typography

No new colours or fonts are introduced. All Sprint 4 surfaces use the existing tokens from `client/styles.css:13`. Mapping:

| UI element | Token |
|---|---|
| Reports page background | `var(--bg)` (#f7f8fb) |
| Tab active underline | `var(--primary)` (#2563eb) |
| Tab inactive text | `var(--text-muted)` (#6b7280) |
| Table border | `var(--border)` (#e5e7eb) |
| Table header background | `var(--surface-alt)` (#f1f3f9) |
| Approve button outline | `var(--success)` (#16a34a) |
| Reject button outline | `var(--danger)` (#dc2626) |
| Skeleton row colour | `var(--surface-alt)` with shimmer overlay |
| "View Reports" link | `var(--primary)` (#2563eb) |

### Tables

Simple bordered rows. No zebra striping (adds CSS noise for negligible demo benefit; skipped per §5 out-of-scope). Table structure:

```html
<table class="data-table">
  <thead>
    <tr>
      <th scope="col" class="sortable" data-col="username">
        Username <span class="sort-icon" aria-hidden="true"></span>
      </th>
      ...
    </tr>
  </thead>
  <tbody>...</tbody>
</table>
```

```css
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.875rem;
}
.data-table th,
.data-table td {
  padding: 0.55rem 0.75rem;
  text-align: left;
  border-bottom: 1px solid var(--border);
}
.data-table thead th {
  background: var(--surface-alt);
  font-weight: 600;
  color: var(--text-muted);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.data-table th.sortable { cursor: pointer; user-select: none; }
.data-table th.sortable:hover { color: var(--primary); }
.data-table tbody tr:hover { background: var(--surface-alt); }
```

### Loading states

Skeleton rows use the same animation keyframe name pattern as the existing `consent-widget--visible` transition. Two skeleton rows per table. Each skeleton row has three `<td>` elements with `<div class="skeleton-bar">` inside.

```css
.skeleton-bar {
  height: 0.75rem;
  border-radius: 4px;
  background: var(--border);
  animation: skeleton-pulse 1.2s ease-in-out infinite;
}
@keyframes skeleton-pulse {
  0%, 100% { opacity: 1; }
  50%       { opacity: 0.4; }
}
```

This matches the visual rhythm of the existing `pulse-dot` animation (`client/styles.css:263`).

---

## §9. Accessibility notes

All new surfaces must satisfy WCAG 2.1 AA. Items specific to Sprint 4:

**Keyboard navigation:**
- Reports page tab bar: ARIA tabs pattern (`role="tablist"`, `role="tab"`, `role="tabpanel"`). Left/right arrow keys cycle tabs. `tabindex="-1"` on inactive tabs; `tabindex="0"` on active. Focus enters the tab panel when a tab is selected.
- My Leaves panel sort controls: each `<th class="sortable">` is keyboard-reachable via natural tab order. `role="button"` is NOT added — `<th>` in a `<thead>` is already interactive when it has a click handler. Add `tabindex="0"` and `onkeydown` to handle `Enter`/`Space`. Announce sort change via the `a11y-polite` live region (`client/index.html:17`): "Sorted by Start Date, ascending."
- Approve/Reject buttons: tab-reachable in DOM order (left-to-right per row). `aria-label` disambiguates ("Approve leave for jane.doe").
- "Back to home" link and "View Reports" link: standard `<a>` elements, keyboard-reachable by default.
- Device row drilldown toggle: `<button aria-expanded="false">` / `aria-expanded="true"`. The expanded sub-row is `aria-label="Details for {username}"`.

**Non-colour status signalling:**
- Status pills always render the text label. The pill text is the primary signal; background colour is secondary. Never a colour-only badge.
- Sort direction: indicated by a text arrow appended to `<th>` (`^` ascending, `v` descending). No icon font dependency.
- Write-scope amber action text: `aria-label` on the action text element announces the full action text to screen readers regardless of colour.

**Table structure:**
- All `<th>` elements have `scope="col"`.
- Empty-state messages are inside the `<tbody>` as a `<tr><td colspan="N">` element (visible to screen readers scanning the table).

**403 panel:**
- Rendered as `<div role="alert">` so screen readers announce it immediately on render.
- Focus is moved to the 403 panel heading on render (`element.focus()` with `tabindex="-1"`).

**Consent widget on Reports page:**
- The existing `announce()` call in `onCibaUrlEvent` (`client/app.js:746`) already announces to the `a11y-assertive` live region. This works on the Reports page because the live regions are in `index.html:17` and always present in the DOM regardless of which page is shown.

**My Leaves panel:**
- Loading state skeleton rows have `aria-busy="true"` on the `<tbody>` while fetch is in-flight.
- Error state uses `role="status"` on the error message element.

---

## §10. Open questions for Stages 5 and 6

1. **API envelope for `GET /api/me/leaves`** — is it `{data: [...], count: N}` (matching the reports pattern from `sprint-4.md` §8) or `[...]` bare array? Stage 5 must lock this. The panel's sort function and empty-state check both depend on the envelope shape. Recommend `{data: [], count: 0}` for consistency.

2. **SSE `ciba_url` payload extension (`action_text` field)** — Stage 5 must confirm the orchestrator-side change: the orchestrator takes `action_text` from the HR/IT Agent's A2A `consent_required` response and includes it in the `ciba_url` SSE event JSON. This is a new field on an existing event type (non-breaking). The A2A spec for `consent_required` also needs to specify `action_text` as an optional field. If absent, the SPA falls back to `scopeToAction(scope)`.

3. **Approve/Reject orchestrator endpoint shape** — Stage 5 decides: `POST /api/reports/leave-requests/{request_id}/approve` vs `POST /api/approve-leave` with body `{request_id}` vs routing through `POST /api/chat` with `{internal: true}`. The UX is identical either way; the architecture question is whether to add a new handler or reuse the chat plumbing. The most likely bite: if the chat plumbing route is used and `skip_transcript` is not implemented cleanly, an internal command message appears in the transcript on the home page (visible when the user navigates back), leaking "Approve leave request LR-042 for jane.doe" as a user message. Stage 5 must prevent this.

4. **My Leaves panel re-fetch scope** — the design in §3 says re-fetch on every `chat_message` SSE settle. This is safe but sends an extra GET per chat turn even for unrelated queries (e.g. "what laptops are available?" triggers a panel refresh). Stage 6 may want to add a simple heuristic: re-fetch only when `event.source_agent === "hr_agent"`. This is a Stage 6 optimisation, not a Stage 5 API issue. Flagging here to avoid a runtime surprise during demo.

5. **Reports navigation entry timing** — does the "Reports" header button appear immediately on page load (from the cached session scope set) or only after the SPA calls a `/api/me/scopes` endpoint added in Sprint 4? Today, `completeLogin` (`client/app.js:378`) only receives `session_id` and `user_display_name` from `/auth/exchange`. Sprint 4 needs to also return the user's scope set (or at minimum a boolean `is_hr_admin`) so the SPA can conditionally render the Reports button. Stage 6 must extend the `/auth/exchange` response. If this is missed, the Reports button will not appear on page load, which breaks the demo flow for Act III.

6. **Device drilldown with Type filter interaction** — when the Type filter is active (e.g. "laptop" selected) and a row is expanded showing assets for a user, if the user then changes the filter to "phone", the expanded row should close and the table re-renders filtered. Stage 6 must ensure the drilldown state resets when the filter changes. Low risk (in-memory client-side), but a demo bug if overlooked.

---

## §11. Deliverable summary table

| Surface | Build status | Files touched |
|---|---|---|
| My Leaves panel | NEW | `client/app.js`, `client/index.html`, `client/styles.css` |
| Reports page — 3 tabs, sortable tables | NEW | Same as above + new template fragment (inline in `index.html` or JS-rendered) |
| Reports page — 403 state | NEW | Same |
| Reports page — nav entry (header button) | NEW | `client/app.js`, `client/index.html`, `client/styles.css` |
| Multi-turn cubicle chat copy | NEW | Orchestrator-side LLM prompt + keyword-fallback router (`orchestrator_mcp_client/`) |
| Consent-widget `action_text` field | NEW field on existing SSE event | `client/app.js` (`onCibaUrlEvent`, `renderWidget`) + orchestrator SSE emitter |
| Write-scope amber action-text tint | NEW CSS modifier | `client/styles.css` + `client/app.js` (`renderWidget`) |
| `SCOPE_ACTION_MAP` / `SCOPE_GERUND_MAP` additions | NEW entries | `client/app.js:122`, `client/app.js:133` |
| Approve/Reject buttons (Reports > Pending Leaves) | NEW | `client/app.js`, `client/styles.css` |
| Toast copy `approvalCancelled` | NEW entry in `COPY` | `client/app.js:16` |
| Status pill CSS (`.status-pill`, `.pill--*`) | NEW | `client/styles.css` |
| Table CSS (`.data-table`, `.skeleton-bar`) | NEW | `client/styles.css` |

---

## §12. References

- `docs/architecture/sprint-4.md` — binding sprint plan (§7 identity model, §8 reporting data flow, §2 objectives)
- `docs/architecture/sprint-4-stage-1-product-review.md` — PM brief (narrative, §8 Option B recommendation)
- `docs/use-cases/UC-11-hr-admin-assigns-cubicle.md` — multi-turn cubicle flow, 4 turns, exception copy
- `docs/use-cases/UC-12-employee-self-service-asset-discovery.md` — serial fan-out, dual-consent, action text per scope
- `docs/use-cases/UC-13-employee-applies-for-leave.md` — My Leaves panel data flow, re-fetch trigger
- `docs/use-cases/UC-14-employee-checks-own-leave-status.md` — panel / chat consistency requirement
- `docs/use-cases/UC-15-hr-admin-pending-leaves-table.md` — CIBA-driven Approve/Reject, 403 non-admin
- `docs/use-cases/UC-16-hr-admin-assignment-reporting-tables.md` — Cubicles + Devices tabs, drilldown pattern
- `docs/use-cases/UC-07-hr-admin-issues-asset.md` — amber tint convention (write scope, admin delegation)
- `docs/ux/copy-deck.md` — existing scope-to-action map (§5.A), gerund map (§5.C)
- `docs/consent-widget-spec.md` — widget state machine
- `client/app.js` — `SCOPE_ACTION_MAP` at line 122, `renderWidget()` at line 830, `onCibaUrlEvent` at line 694
- `client/styles.css` — design tokens (line 13), consent widget styles (line 488), amber variables (line 47)
