# Sprint 4 — Stage 5: API Design

**Stage:** 5 (API design — locked after document review)
**Date:** 2026-05-10
**Branch (entry):** `sprint-3-build` @ `b497616`
**Read order:** [`sprint-4.md`](sprint-4.md) (binding) → [`sprint-4-stage-4-ux-design.md`](sprint-4-stage-4-ux-design.md) → this doc → Stage 6 (tech arch)
**Supersedes:** nothing. First API design doc for Sprint 4.

> **Amendment 2026-05-10 (post-Stage 6, user challenge):** Decision B is simplified — **drop the `roles` field and `groups` claim dependency.** The `/auth/exchange` response returns ONLY `scopes: list[str]`. SPA derives "is HR Admin" client-side via `scopes.includes("hr_approve_rest")` (canonical probe — HR-Admin-exclusive per `docs/scope-policy.md` §2). Justification: role-to-scope binding at IS is already the authoritative gate; a separate `groups` claim adds nothing to the security boundary and would require an extra IS configuration step. Server-side scope checks remain authoritative on every request. References to `roles` / `groups` below are obsolete; treat them as struck-through.
>
> **Stage 6.5 amendment 2026-05-10:** **§B2 endpoint calls `hr_service.get_all_leave_requests(status=...)` NOT `get_leaves_for_dashboard(status=...)`.** The latter's projection drops `request_id`, which is required by A6/A7 (Approve/Reject buttons). `get_all_leave_requests` already returns the right shape including `request_id`. See [`sprint-4-stage-6.5-reconciliation.md`](sprint-4-stage-6.5-reconciliation.md) D5.

---

## §1. Goal and scope summary

Sprint 4 adds business-domain richness on top of the existing IAM plumbing. To support the
Reports page, My Leaves panel, cubicle-assignment workflow, and employee self-service, the
following new API surfaces must be defined before Stage 6 can begin implementation.

| Surface | New endpoints / extensions | Key scopes |
|---|---|---|
| **A — Orchestrator REST** | 5 new endpoints + 1 response extension (`/auth/exchange`) | cookie session; proxies token-A |
| **B — HR Server REST** | 5 new endpoints (3 read, 2 write) | `hr_self_rest`, `hr_read_rest`, `hr_approve_rest` |
| **C — IT Server REST** | 1 new endpoint | `it_assets_read_rest` |
| **D — HR Server MCP tools** | 5 new tools | `hr_read_rest`, `hr_self_rest`, `hr_assets_write_rest` |
| **E — IT Server MCP tools** | 1 new tool | `it_assets_self_rest` |
| **F — SSE payload extension** | `ciba_url` event gains `action_text` field | — |
| **G — A2A extension** | `ConsentRequiredPayload` gains `action_text` field | — |

Total: **11 new REST endpoints**, **6 new MCP tool definitions**, **1 SSE event field extension**,
**1 A2A payload field extension**, and **1 `/auth/exchange` response field extension**.

The scope lock from `docs/architecture/sprint-4.md` §6 is binding. No new scope family is
introduced beyond `hr_assets_write_rest` and `it_assets_self_rest` (both already locked).

---

## §2. Decisions on UX-flagged items

### Decision A — Approve/Reject plumbing

**Item flagged by:** `sprint-4-stage-4-ux-design.md` §4.1 and §7.

**Options considered:**

- **(a) Chat-routed internal command.** SPA posts to `POST /api/chat` with a hidden internal
  flag (`skip_transcript: true`). The orchestrator routes to HR Agent as usual and CIBA fires.
  The transcript is not updated.
- **(b) Dedicated REST handler** — `POST /api/reports/leave-requests/{request_id}/approve` and
  the corresponding `/reject` variant. The orchestrator handles it independently, invokes HR
  Agent directly without inserting any chat message, and SSE state changes are delivered on the
  existing session stream.

**Decision: Option (b) — dedicated REST handlers.**

Justification:

1. **Transcript hygiene.** Option (a) requires that the orchestrator skip appending the command
   to the chat transcript. If that guard is ever missing (e.g. a code path that doesn't check
   the flag), an internal command like `"Approve leave request LR-042 for jane.doe"` leaks into
   the visible transcript. Option (b) is immune because it never touches the chat message store.
2. **Cleaner audit trail.** The new endpoints emit their own `X-Request-ID`-tagged log lines
   without mixing with chat-path log lines. Searching audit logs for approve/reject events is
   unambiguous.
3. **No new orchestrator capability.** The orchestrator already has the ability to invoke HR
   Agent and push CIBA state over SSE from non-chat entry points (it does so in the background
   for BCL cascade in Sprint 3). Adding a reports-route handler is the same pattern.
4. **Same CIBA path under the hood.** The CIBA flow (agent invocation, IS consent, token-C, MCP
   call) is identical to the chat path; only the entry point differs. No security surface change.

The endpoint shapes are specified in §3 Group A (A6, A7).

---

### Decision B — `/auth/exchange` scope and role surface

**Item flagged by:** `sprint-4-stage-4-ux-design.md` §10 Q5.

**Options considered:**

- **(i)** Add `is_hr_admin: bool` field only.
- **(ii)** Add `roles: list[str]` only.
- **(iii)** Add `roles: list[str]` AND `scopes: list[str]`.
- **(iv)** Add a separate `GET /api/me/scopes` endpoint the SPA can call after login.

**Decision: Option (iii) — both `roles` and `scopes` in the `/auth/exchange` response.**
Option (iv) is explicitly rejected (§3 Group A A2 below explains why).

Justification:

1. **SPA navigation gating.** The SPA must conditionally render the "Reports" header button and
   the "View Reports" panel link based on role. `roles.includes("HR Admin")` is the cleanest
   expression of this intent; a boolean flag is brittle when a third role is added. A list is
   the correct abstraction.
2. **Scope display in consent widget fallback.** `scopes` is already surfaced in the `ciba_url`
   SSE event per-request; having the full list at login time lets the SPA build `SCOPE_ACTION_MAP`
   coverage checks without calling a separate endpoint.
3. **Server-side authority is not weakened.** Both HR Server and IT Server still validate scope
   on every request. The `roles` and `scopes` values returned at `/auth/exchange` are *display
   hints* for the SPA — they do not substitute for server-side enforcement.
4. **Single round-trip.** The `roles` claim comes from the `groups` claim in the IS-issued ID
   token (or access token — operator action A3 must confirm this is mapped). The `scopes` value
   is `token_a.scope.split()`. Both are available immediately after the code exchange. No extra
   IS call needed.

**Derivation rule** (locked for Stage 6 implementation):

```
ExchangeResponse.roles  = id_token["groups"] ?? access_token["groups"] ?? []
ExchangeResponse.scopes = token_a.scope.split(" ")
```

The `groups` claim name is the WSO2 IS default for role membership. Stage 6 must verify this
against the live IS at `13.60.190.47:9443` (risk R1 from `sprint-4.md` §10) and add a fallback
probe in `scripts/probe-claims.sh`.

**Rejection of separate `GET /api/me/scopes` (A2).** A separate endpoint adds a required round-
trip before the SPA can render the header button, producing a flash of missing navigation on
first paint. Embedding the data in `/auth/exchange` is the zero-round-trip solution. There is no
benefit to a separate endpoint for Sprint 4.

---

## §3. Endpoint specifications

Conventions used throughout:

- **Auth (cookie session):** `Cookie: orch_sid=<sid>`. Validated by
  `orchestrator/auth/session_store.py`. 401 if absent or invalid; 403 if scope check fails.
- **Auth (Bearer token-A):** `Authorization: Bearer <token-A>` forwarded by orchestrator. Scope
  is validated server-side by the receiving service.
- **Auth (Bearer token-C):** OBO token issued by IS after CIBA. Carries scope appropriate to the
  tool being invoked.
- **Pydantic v2 style.** All new models use `model_config = ConfigDict(extra="forbid")`.
- **Envelope for list endpoints.** `{data: [...], count: N}`. Detail endpoints return the record
  directly without an envelope. See §5 for the locked envelope.
- **Timestamps.** ISO 8601 strings (`YYYY-MM-DDTHH:MM:SSZ` for datetimes; `YYYY-MM-DD` for
  dates). Never Unix epoch on the wire.
- **Error envelope.** `ErrorEnvelope` from `api-contracts.md` §1:
  `{"error_id": "ERR-...", "message": "...", "request_id": "..."}`.

---

### Group A — Orchestrator REST (cookie-auth, browser-facing)

All A-group endpoints live in a new FastAPI router at `orchestrator/reports/routes.py`
(recommended file; not yet created). They share the session-validation middleware already used
by `POST /api/chat`.

---

#### A1. `GET /api/me/leaves`

**Purpose:** My Leaves panel — returns the authenticated user's own leave requests.

**Auth:** Cookie session (`orch_sid`). No scope pre-flight needed at orchestrator level (scope
`hr_self_rest` is validated by HR Server). Orchestrator reads `session.token_a` and proxies.

**Request:** No body. No query parameters for Sprint 4.

**Response model:**

```python
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict


class LeaveRequestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str          # e.g. "LR-001"
    type: str                # e.g. "Annual", "Sick", "Personal"
    start_date: str          # YYYY-MM-DD
    end_date: str            # YYYY-MM-DD
    days_requested: int
    status: Literal["Pending", "Approved", "Rejected"]
    reason: str | None = None


class MyLeavesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[LeaveRequestItem]
    count: int
```

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | No session cookie or session expired |
| 502 | `ERR-API-PROXY-001` | HR Server returned 5xx or is unreachable |

**Behaviour notes:**

- Orchestrator proxies `GET /api/me/leaves` to `hr_server` with `Authorization: Bearer <token-A>`.
- `sub` is extracted from `session.token_a` and forwarded as a query parameter
  (`?user_sub=<sub>`) OR HR Server derives it from the token — Stage 6 decides which. The
  token-derived path is preferred (avoids sub leakage in URL); see §8 open question OQ-1.
- Idempotent read. No side effects.

**Cross-references:** UC-13, UC-14, `sprint-4.md` §2 item 10/11, `sprint-4.md` §8 "My Leaves
panel".

---

#### A2. `GET /api/me/scopes` — ELIMINATED

Per Decision B in §2, this endpoint is not introduced. The `/auth/exchange` response carries
`roles` and `scopes` at login time. No separate endpoint is needed.

---

#### A3. `GET /api/reports/leave-requests`

**Purpose:** Reports page, Pending Leaves tab — all leave requests (admin view).

**Auth:** Cookie session + orchestrator pre-flight requires `hr_read_rest` in `session.token_a.scope`.
If missing, 403 returned before contacting HR Server.

**Query parameters:**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `status` | `str \| None` | `None` | Filter: `"pending"`, `"approved"`, `"rejected"`. Case-insensitive match at HR Server. |

**Response model:**

```python
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict


class PendingLeaveItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    employee_username: str    # derived from store, NOT sub — see sprint-4.md §7
    employee_email: str
    type: str
    start_date: str           # YYYY-MM-DD
    days_requested: int
    status: Literal["Pending", "Approved", "Rejected"]


class LeaveRequestsReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[PendingLeaveItem]
    count: int
```

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | No session |
| 403 | `ERR-AUTH-007` | `hr_read_rest` missing from token-A claims |
| 502 | `ERR-API-PROXY-001` | HR Server 5xx or unreachable |

**Behaviour notes:**

- Orchestrator proxies to HR Server `GET /api/reports/leave-requests?status=<status>` verbatim.
- No pagination for Sprint 4 (demo scale, ≤ 50 records).
- `employee_username` and `employee_email` come from `store.leave_requests[req_id]["user_name"]`
  (existing field) and a new `user_email` field that Stage 6 must add to the leave record at
  `apply_leave` time. See §8 OQ-2.

**Cross-references:** UC-15, `sprint-4.md` §2 item 4, §8 "Per-tab summary".

---

#### A4. `GET /api/reports/cubicle-assignments`

**Purpose:** Reports page, Cubicles tab — all assigned cubicles.

**Auth:** Cookie session + orchestrator pre-flight requires `hr_read_rest` in `session.token_a.scope`.

**Request:** No body. No query parameters for Sprint 4.

**Response model:**

```python
from __future__ import annotations
from pydantic import BaseModel, ConfigDict


class CubicleAssignmentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_username: str
    employee_email: str
    cubicle_id: str           # e.g. "C-027"
    floor: int                # 1, 2, 3, or 4
    assigned_at: str          # ISO 8601 date, YYYY-MM-DD


class CubicleAssignmentsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[CubicleAssignmentItem]
    count: int
```

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | No session |
| 403 | `ERR-AUTH-007` | `hr_read_rest` missing |
| 502 | `ERR-API-PROXY-001` | HR Server 5xx |

**Behaviour notes:** Read-only; idempotent. No filtering in Sprint 4 (client-side sort only).
`assigned_to_sub` is never included in the response (internal join key per `sprint-4.md` §7).

**Cross-references:** UC-16 Part A, `sprint-4.md` §2 item 4, §8.

---

#### A5. `GET /api/reports/device-assignments`

**Purpose:** Reports page, Devices tab — all IT asset assignments.

**Auth:** Cookie session + orchestrator pre-flight requires `it_assets_read_rest` in
`session.token_a.scope`.

**Request:** No body. No query parameters.

**Response model:**

```python
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict


class DeviceAssignmentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_username: str
    employee_email: str
    asset_id: str             # e.g. "AST-12345"
    type: str                 # "laptop" | "phone" | "monitor" | "headset"
    model: str                # e.g. "MBP 14 M3"
    status: Literal["outstanding", "returned"]


class DeviceAssignmentsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[DeviceAssignmentItem]
    count: int
```

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | No session |
| 403 | `ERR-API-PROXY-001` | `it_assets_read_rest` missing — see note |
| 502 | `ERR-API-PROXY-001` | IT Server 5xx |

**Note on 403 for Devices tab:** UC-16 EX-B2 notes that `it_assets_read_rest` is granted to
both roles (per `docs/scope-policy.md` §2). Employees can therefore pass this pre-flight check
if they URL-fuzz `/reports`. The UC-16 design note accepts this for Sprint 4 (read-only, data
they had access to via `list_available_assets` anyway). The SPA still hides the Reports page nav
entry for non-admins. Stage 6 is flagged to consider adding a `groups`-claim check if this
becomes a concern (`sprint-4.md` §9 open item).

**Cross-references:** UC-16 Part B, `sprint-4.md` §2 item 4, §8.

---

#### A6. `POST /api/reports/leave-requests/{request_id}/approve`

**Purpose:** Approve a pending leave request from the Reports page table. Triggers HR Agent CIBA
on `hr_approve_rest` without creating a chat message.

**Path parameter:** `request_id` — string, e.g. `"LR-042"`. Must match a record in HR Server.

**Auth:** Cookie session. Orchestrator pre-flight requires `hr_approve_rest` in
`session.token_a.scope`. If missing, 403 immediately.

**Request model:**

```python
from pydantic import BaseModel, ConfigDict


class ApproveLeaveRequest(BaseModel):
    """No required body fields for Sprint 4. Future: rejection_reason."""
    model_config = ConfigDict(extra="forbid")
```

The endpoint accepts an empty JSON body `{}` or no body. This is intentional — all information
needed to construct the CIBA binding message is derived from `request_id` (fetched from HR
Server at orchestrator lookup time) and the caller's session identity.

**Response model:**

```python
from pydantic import BaseModel, ConfigDict


class ApproveRejectAck(BaseModel):
    """Acknowledgement that the CIBA flow was initiated."""
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    request_id: str       # leave request ID echoed
    ciba_initiated: bool  # always True on 200; False paths return non-200
```

The full CIBA lifecycle (consent widget, DONE/DENIED/EXPIRED) is communicated via the existing
SSE stream on `/events/{session_id}`. The SPA does not poll this endpoint; it relies on SSE
state changes as with the chat path.

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | No session |
| 403 | `ERR-AUTH-007` | `hr_approve_rest` missing from token-A claims |
| 404 | `ERR-API-PROXY-001` | Leave request not found on HR Server |
| 409 | `ERR-API-PROXY-001` | Leave request already Approved or Rejected (not Pending) |
| 502 | `ERR-API-PROXY-001` | HR Server or HR Agent unreachable |

**Behaviour notes:**

- The orchestrator invokes HR Agent via A2A (not via `POST /api/chat`). No chat message is
  created; `session.pending_ciba` is updated as with any A2A call.
- Before invoking the agent, the orchestrator fetches the leave request metadata from HR Server
  (`GET /api/reports/leave-requests?request_id=<id>` — a detail lookup Stage 6 adds to HR
  Server, or it reads from the existing `get_leave_request_details` service function) in order
  to construct the `action_text`:
  `"Approve {employee_username}'s leave from {start_date}"`.
- **Idempotency:** two concurrent clicks on the same row are prevented by the SPA disabling the
  button while `requestInFlight` is true (per `sprint-4-stage-4-ux-design.md` §7). The
  orchestrator additionally checks `session.pending_ciba` — if an approve/reject CIBA is already
  in-flight for the same session, it returns 429 (`ERR-AGENT-006`, same as the chat path).
- The `action_text` value is forwarded through A2A `ConsentRequiredPayload.action_text` to the
  SSE `ciba_url` event so the SPA renders the amber-tinted widget copy correctly.

**Cross-references:** UC-15 step 10, `sprint-4-stage-4-ux-design.md` §7, Decision A above.

---

#### A7. `POST /api/reports/leave-requests/{request_id}/reject`

**Purpose:** Reject a pending leave request from the Reports page table.

**Auth:** Same as A6: cookie session + `hr_approve_rest` pre-flight.

**Request model:**

```python
from pydantic import BaseModel, ConfigDict, Field


class RejectLeaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        default="Rejected by HR Admin",
        min_length=1,
        max_length=500,
        description="Rejection reason; forwarded to HR Agent for the binding message.",
    )
```

`reason` is optional in the sense that a default value is supplied. If the SPA does not send a
body, the orchestrator uses the default. The field is included in the CIBA `binding_message` and
in `action_text` to distinguish "Reject jane.doe's leave from 2026-06-10" from the approve flow.

**Response model:** same `ApproveRejectAck` as A6.

**Error responses:** identical to A6 table.

**Behaviour notes:** structurally identical to A6 except:

- HR Agent calls `hr_service.reject_leave_request(request_id, reason, reviewer_sub, reviewer_name)`.
- `action_text` = `"Reject {employee_username}'s leave from {start_date}"`.

**Cross-references:** UC-15 step 10, `sprint-4-stage-4-ux-design.md` §7.

---

#### A8. Extension to `/auth/exchange` response

Per Decision B, `ExchangeResponse` in `orchestrator/auth/routes.py` gains two new fields. The
existing fields (`ok`, `user_label`, `session_id`, `user_display_name`) are unchanged.

```python
from __future__ import annotations
from pydantic import BaseModel, ConfigDict


class ExchangeResponse(BaseModel):
    """Extended ExchangeResponse — Sprint 4 adds roles and scopes."""
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    user_label: str
    session_id: str
    user_display_name: str
    # Sprint 4 additions:
    roles: list[str]   # e.g. ["HR Admin"] or ["Employee"]
    scopes: list[str]  # e.g. ["openid", "hr_self_rest", "hr_read_rest", ...]
```

**Derivation (Stage 6 implementation rule):**

```python
# In orchestrator/auth/routes.py, inside the exchange handler, after token_a is built:
import pyjwt

id_payload = pyjwt.decode(result.token_a.id_token, options={"verify_signature": False})
roles: list[str] = id_payload.get("groups", [])
scopes: list[str] = result.token_a.raw.scope.split()
```

If `groups` is absent from the ID token, an access-token claim decode must be attempted
(same unverified decode; token was already validated). If neither carries `groups`, `roles`
defaults to `[]` — the SPA then hides all admin surfaces, which is the safe-fail direction.

**Breaking change assessment:** non-breaking. The two new fields are additive; clients that
ignore unknown fields are unaffected. The existing `ExchangeResponse` consumers (the relay
HTML page's fetch in `_make_exchange_relay_html`) only read `session_id` and
`user_display_name`; the new fields are ignored there.

**Cross-references:** `sprint-4-stage-4-ux-design.md` §10 Q5, `sprint-4.md` §9 "Stage 5 must
answer", `sprint-4.md` risk R1.

---

### Group B — HR Server REST (Bearer token-A, server-to-server)

All B-group endpoints are added to `hr_server`. They validate token-A via the existing
`validate_token` (F-04 six-step + Sprint 3 Step 7 denylist). The token-A audience is the
orchestrator's OAuth Client ID (not the agent's); HR Server's validator must be aware of
this for reporting endpoints (as distinct from MCP tool endpoints where the audience is the
HR Agent's Client ID). Stage 6 must confirm the HR Server validator handles both audience
values. See §8 OQ-3.

---

#### B1. `GET /api/me/leaves` (HR Server side)

**Purpose:** Returns the authenticated user's own leave requests. Called by orchestrator when
proxying A1.

**Auth:** Bearer token-A. Required scope: `hr_self_rest`.

**Query parameters:** none (user identity derived from `token.sub`).

**Implementation:** calls `hr_service.get_my_leave_requests(sub=claims.sub, ...)`. The existing
function signature at `hr_server/service/hr_service.py:54` takes `sub`, `first_name`, `last_name`.
Sprint 4 must resolve `first_name`/`last_name` from the `username` claim or the user's stored
record, not from agent-injected arguments. See §4 identity claim plumbing.

**Response model:** same `MyLeavesResponse` as A1 (HR Server owns the type definition; the
orchestrator passes through verbatim).

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | Token missing, expired, or bad signature |
| 403 | `ERR-MCP-003` | Scope `hr_self_rest` absent |

**Cross-references:** A1 above, `hr_server/service/hr_service.py:54`.

---

#### B2. `GET /api/reports/leave-requests` (HR Server side)

**Purpose:** All leave requests with optional status filter. Called by orchestrator when proxying A3.

**Auth:** Bearer token-A. Required scope: `hr_read_rest`.

**Query parameters:** `status` (optional string, case-insensitive).

**Implementation:** calls `hr_service.get_leaves_for_dashboard(status=status)`. Existing
function at `hr_server/service/hr_service.py:274`. The response must include
`employee_username` and `employee_email` fields. The existing store record has `user_name`
(a display name) but lacks separate `username` and `email` fields. Stage 6 must add `user_email`
to `store.leave_requests` records at creation time (in `apply_leave`) so this endpoint can
return them. See §8 OQ-2.

**Response model:** same `LeaveRequestsReportResponse` as A3.

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | Token validation failure |
| 403 | `ERR-MCP-003` | `hr_read_rest` absent |

**Cross-references:** A3 above, `hr_server/service/hr_service.py:274`.

---

#### B3. `GET /api/reports/cubicle-assignments` (HR Server side)

**Purpose:** All assigned cubicles. Called by orchestrator when proxying A4.

**Auth:** Bearer token-A. Required scope: `hr_read_rest`.

**Implementation:** calls new `hr_service.get_all_cubicle_assignments()`. New service function
(Sprint 4 build): reads `store.cubicles` filtered by `occupied=True`. Projects only
`{employee_username, employee_email, cubicle_id, floor, assigned_at}`. Never returns
`assigned_to_sub` (per `sprint-4.md` §7 identity model).

**Response model:** same `CubicleAssignmentsResponse` as A4.

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | Token validation failure |
| 403 | `ERR-MCP-003` | `hr_read_rest` absent |

**Cross-references:** A4 above, UC-16 Part A, `sprint-4.md` §2 item 1.

---

#### B4. `POST /api/leave-requests/{request_id}/approve` (HR Server side)

**Purpose:** Approve a single pending leave request. Called by HR Agent after CIBA on
`hr_approve_rest`. Note the path does **not** include `/reports/` — this is an agent-invoked
write, not a reporting read.

**Auth:** Bearer token-C. Required scope: `hr_approve_rest`.

**Path parameter:** `request_id` — string.

**Request model:**

```python
from pydantic import BaseModel, ConfigDict


class HRApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_name: str   # HR Agent resolves from token claims (username)
```

The `reviewer_sub` is derived from `claims.sub` (never sent by the client). `reviewer_name` is
the admin's username from the `username` claim; the HR Agent constructs this from the resolved
claim set before calling this endpoint.

**Response model:**

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict


class LeaveActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    request_id: str
    new_status: Literal["Approved", "Rejected"]
    employee_username: str
    notification: str   # human-readable, e.g. "Leave request LR-042 for jane.doe approved."
```

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | Token validation failure |
| 403 | `ERR-MCP-003` | `hr_approve_rest` absent |
| 404 | `ERR-API-PROXY-001` | `request_id` not found |
| 409 | `ERR-API-PROXY-001` | Request already Approved or Rejected |

**Implementation:** calls `hr_service.approve_leave_request(request_id, reviewer_sub=claims.sub,
reviewer_name=body.reviewer_name)`. Existing function at `hr_server/service/hr_service.py:196`.

**Idempotency:** calling approve on an already-Approved request returns 409
(`invalid_status` error from service layer). This is intentional — the button is disabled
after the first click, so 409 is a guard against race conditions, not a normal path.

**Cross-references:** UC-15 step 10, A6 above, `hr_server/service/hr_service.py:196`.

---

#### B5. `POST /api/leave-requests/{request_id}/reject` (HR Server side)

**Purpose:** Reject a single pending leave request.

**Auth:** Bearer token-C. Required scope: `hr_approve_rest`.

**Request model:**

```python
from pydantic import BaseModel, ConfigDict, Field


class HRRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_name: str
    reason: str = Field(default="Rejected by HR Admin", min_length=1, max_length=500)
```

**Response model:** same `LeaveActionResult` as B4 (with `new_status="Rejected"`).

**Error responses:** identical to B4 table.

**Implementation:** calls `hr_service.reject_leave_request(request_id, reason=body.reason,
reviewer_sub=claims.sub, reviewer_name=body.reviewer_name)`. Existing function at
`hr_server/service/hr_service.py:243`.

**Cross-references:** UC-15 step 10, A7 above, `hr_server/service/hr_service.py:243`.

---

### Group C — IT Server REST (Bearer token-A, server-to-server)

---

#### C1. `GET /api/reports/device-assignments` (IT Server side)

**Purpose:** All IT asset assignments. Called by orchestrator when proxying A5.

**Auth:** Bearer token-A. Required scope: `it_assets_read_rest`.

**Implementation:** calls new `it_service.get_all_asset_assignments()`. New service function:
reads `store.assets` (Sprint 4 rekeyed by `username`), joins `store.users` for `email` lookup.
Returns all assets with `employee_username`, `employee_email` derived from the username key.

Current `it_server/service/store.py` seeds by `employee_id` (numeric strings like `"1042"`).
Stage 6 performs the one-shot data migration to `username` keys (`sprint-4.md` §7 item 15).
After migration, `get_assets_for_employee(employee_id)` is replaced by
`get_assets_for_username(username)`. The reporting function reads the full `store.assets` list.

**Response model:** same `DeviceAssignmentsResponse` as A5.

**Error responses:**

| HTTP | `error_id` | Condition |
|---|---|---|
| 401 | `ERR-AUTH-006` | Token validation failure |
| 403 | `ERR-MCP-003` | `it_assets_read_rest` absent |

**Cross-references:** A5 above, UC-16 Part B, `sprint-4.md` §2 item 4 and §7.

---

### Group D — HR Server MCP Tools (Bearer token-C)

All D-group tools follow the existing `tools.py` shape in `hr_server/mcp/tools.py`. Token
validation is the six-step F-04 check plus denylist (Step 7) on every call. The `act.sub`
check (step 5 in `api-contracts.md` §4) uses the HR Agent's agent UUID. Scope per tool is
checked at step 6.

---

#### D1. `get_cubicle_summary()`

**Scope required:** `hr_read_rest`.

**Args:** none.

**Return model:**

```python
from pydantic import BaseModel, ConfigDict


class FloorSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    vacant: int
    occupied: int


class CubicleFloorSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor_1: FloorSummary
    floor_2: FloorSummary
    floor_3: FloorSummary
    floor_4: FloorSummary
```

**Behaviour:** reads `store.cubicles`; groups by `floor`; counts `occupied=True` vs total.
Returns a dict-shaped response; the agent formats it into prose for the chat reply.

**Error responses:** 403 (`ERR-MCP-003`) if scope absent; 401 (`ERR-AUTH-006`) if token invalid.

**Cross-references:** UC-11 Turn 1, `sprint-4.md` §2 item 2.

---

#### D2. `get_vacant_cubicles_on_floor(floor: int)`

**Scope required:** `hr_read_rest`.

**Args model:**

```python
from pydantic import BaseModel, ConfigDict, Field


class GetVacantCubiclesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor: int = Field(ge=1, le=4, description="Floor number 1–4.")
```

**Return model:**

```python
from pydantic import BaseModel, ConfigDict


class VacantCubiclesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    floor: int
    vacant_cubicle_ids: list[str]   # e.g. ["C-026", "C-027", ..., "C-050"]
    vacant_count: int
```

**Behaviour:** filters `store.cubicles` by `floor=floor AND occupied=False`. Returns the list of
IDs. If `vacant_count == 0`, agent is expected to surface the "no vacant cubicles on this floor"
message with a redirect to the summary (per `sprint-4-stage-4-ux-design.md` §5 Turn 2).

**Cross-references:** UC-11 Turn 2.

---

#### D3. `assign_cubicle(cubicle_id: str, employee_username: str, employee_email: str)`

**Scope required:** `hr_assets_write_rest`.

**Args model:**

```python
from pydantic import BaseModel, ConfigDict, Field


class AssignCubicleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cubicle_id: str = Field(description="Cubicle identifier, e.g. 'C-027'.")
    employee_username: str = Field(
        description="Target employee's IS username, e.g. 'jane.doe'."
    )
    employee_email: str = Field(
        description="Target employee's email. Resolved by lookup_employee before this call."
    )
```

**Return model:**

```python
from pydantic import BaseModel, ConfigDict


class AssignCubicleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    cubicle_id: str
    floor: int
    assigned_to_username: str
    assigned_to_email: str
    assigned_at: str   # ISO 8601 datetime string


class CubicleAlreadyOccupiedError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str = "cubicle_already_occupied"
    current_holder_username: str
    current_holder_email: str
```

**Idempotency rule (locked by `sprint-4.md` §9 "Stage 5 must answer"):**

- Same `(cubicle_id, employee_username)` called a second time: return the existing
  `AssignCubicleResult` with `success=True`. No state change. The `assigned_at` reflects the
  original assignment time.
- Same `cubicle_id` with a *different* `employee_username`: return a 200 response body with
  `error="cubicle_already_occupied"` and `current_holder_*` fields. HTTP status remains 200
  (the MCP call succeeded; the business outcome is an error). The agent surfaces
  `ERR-CUBICLE-001` to the orchestrator.

**Why 200 not 409 for occupied?** MCP tools in this codebase return 200 with an error-shaped
body for business-logic rejections (consistent with how `apply_leave` returns
`{"error": "invalid_leave_type", ...}` as a 200 at
`hr_server/service/hr_service.py:84–91`). The agent is the party that interprets the error
field and surfaces the appropriate copy to the orchestrator.

**Error responses at HTTP level:**

| HTTP | Condition |
|---|---|
| 401 | Token invalid |
| 403 | `hr_assets_write_rest` absent (`ERR-MCP-003`) |

**Cross-references:** UC-11 Turn 3/4, UC-11 EX-1, `sprint-4.md` §2 item 2.

---

#### D4. `get_my_cubicle()`

**Scope required:** `hr_self_rest`.

**Args:** none (caller identity derived from `claims.username`).

**Return model:**

```python
from __future__ import annotations
from pydantic import BaseModel, ConfigDict


class MyCubicleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assigned: bool
    cubicle_id: str | None = None   # None when assigned=False
    floor: int | None = None
    assigned_at: str | None = None
```

**Behaviour:** reads `store.cubicles` filtered by `assigned_to_username == claims.username`.
If no match, returns `{"assigned": false}`. The `username` claim must be present in the token
(operator action A3, `sprint-4.md` §3); if absent, fails with 401.

**Cross-references:** UC-12 HR leg, `sprint-4.md` §2 item 2.

---

#### D5. `lookup_employee(username_or_email: str)`

**Scope required:** `hr_read_rest`.

**Args model:**

```python
from pydantic import BaseModel, ConfigDict


class LookupEmployeeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username_or_email: str
```

**Return model:**

```python
from pydantic import BaseModel, ConfigDict


class EmployeeLookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    username: str | None = None
    email: str | None = None
    sub: str | None = None     # IS UUID; used internally by HR Agent for CIBA login_hint
```

**Behaviour:** searches a pre-seeded lookup table in `hr_server/service/store.py`. Sprint 4
seeds: `employee_user`, `hr_admin_user`, and 1–2 "new hire" stub records (e.g. `jane.doe`,
`bob.smith`) with their corresponding IS `sub` values. Matching logic:

1. If `username_or_email` contains `@`, match against `email` field.
2. Otherwise, match against `username` field.
3. Case-insensitive.

If no match: `{"found": false}`. The HR Agent (not the MCP server) raises `ERR-EMPLOYEE-001`
and surfaces the "employee not found" copy before initiating CIBA.

**Note:** `sub` is returned here because the HR Agent needs it as `login_hint` for CIBA. The
`sub` is never surfaced in SPA responses or reporting rows — it lives only in the agent's in-
memory reasoning step. This is consistent with `sprint-4.md` §7 ("assigned_to_sub for joining
(never displayed)").

**Cross-references:** UC-11 Turn 3 step 16, `sprint-4.md` §7.

---

### Group E — IT Server MCP Tools (Bearer token-C)

---

#### E1. `get_my_assets()`

**Scope required:** `it_assets_self_rest`.

**Args:** none (caller identity derived from `claims.username`).

**Return model:**

```python
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict


class MyAssetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    type: str
    model: str
    status: Literal["outstanding", "returned"]


class MyAssetsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assets: list[MyAssetItem]
    total: int
```

**Behaviour:** reads `store.assets` filtered by `username == claims.username`. The `username`
claim is mandatory (operator A3). If absent from the token, return 401 rather than falling back
to `sub`-based lookup (fail-closed, per §4). If no assets found for the username, return
`{"assets": [], "total": 0}` — this is not an error (UC-12 EX-2).

**Breaking change from existing `get_my_assets`.** The existing `GetMyAssetsArgs` in
`it_server/mcp/tools.py:130` has an `employee_id: str | None` parameter (inherited from the
`api-contracts.md` Sprint 1 scaffold). In Sprint 4 this is removed — the tool takes no args.
The scope changes from the legacy `it.read` stub to `it_assets_self_rest`. This is a planned
breaking change per `sprint-4.md` §7 and §2 objective 3.

**Cross-references:** UC-12 IT leg, `sprint-4.md` §2 item 3.

---

### Group F — SSE event extension

---

#### F1. `ciba_url` event payload — `action_text` field

The existing `CibaUrlEvent` TypeScript interface (from `api-contracts.md` §2) gains one
optional field. The Pydantic server-side model lives in `orchestrator/sse/events.py`
(to be verified by Stage 6; the file is not in the codebase snapshot but the TypeScript
interfaces in `api-contracts.md` §2 describe the canonical shapes).

**Updated model:**

```python
from __future__ import annotations
from pydantic import BaseModel, ConfigDict


class CibaUrlEventPayload(BaseModel):
    """SSE event payload for type='ciba_url'. Sprint 4: adds action_text."""
    model_config = ConfigDict(extra="forbid")

    type: str = "ciba_url"
    request_id: str
    agent_id: str
    agent_label: str
    action: str             # existing field — plain binding_message summary
    auth_url: str
    binding_code: str
    expires_in: int
    scope: str
    is_refresh: bool
    prior_consent_at: str | None
    # Sprint 4 addition:
    action_text: str | None = None
    # When present: SPA uses this verbatim for the consent widget action line.
    # When absent: SPA falls back to scopeToAction(scope) as today.
    # Parameterised examples:
    #   "Assign cubicle C-027 to jane.doe"
    #   "Approve jane.doe's leave from 2026-06-10"
    #   "Reject jane.doe's leave from 2026-06-10"
```

**Non-breaking.** The field is `str | None = None`. Existing SPA code that does not read
`action_text` is unaffected.

**Propagation path:** HR Agent (or IT Agent) constructs `action_text` from the resolved tool
arguments *before* calling `IS /oauth2/ciba`. It includes `action_text` in the A2A
`ConsentRequiredPayload` (see Group G below). The orchestrator reads it from the A2A response
and includes it in the SSE event.

**Cross-references:** `sprint-4-stage-4-ux-design.md` §6, A6/A7 above.

---

### Group G — A2A payload extension

The `ConsentRequiredPayload` in `common/a2a/models.py` (currently inlined in
`api-contracts.md` §3 as a Pydantic model) gains one optional field to carry the
parameterised action text from agent to orchestrator.

```python
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict


class ConsentRequiredPayload(BaseModel):
    """Sprint 4: adds optional action_text."""
    model_config = ConfigDict(extra="forbid")

    type: Literal["consent_required"]
    auth_req_id: str
    auth_url: str
    binding_code: str
    agent_label: str
    action: str          # existing — generic action string
    expires_in: int
    scope: str
    # Sprint 4 addition:
    action_text: str | None = None
    # Parameterised action string, e.g. "Assign cubicle C-027 to jane.doe".
    # When absent, orchestrator derives action_text from action for the SSE event.
```

This is a non-breaking field addition. Existing agents that do not set `action_text` continue
to work; the orchestrator falls back to the `action` string.

---

## §4. Identity claim plumbing

This section is binding for Stage 6 implementation. It answers the "username/email claim
surfacing" item from `sprint-4.md` §7 and operator ask A3.

### 4.1 Token-A and token-C claim requirements

Both token-A and token-C **must** carry `username` and `email` claims for Sprint 4 tools to
function correctly. The operator must verify this at Stage 6 Day 1 using the probe script
`scripts/probe-claims.sh` against the live IS at `13.60.190.47:9443`.

| Claim | Token | Required by | Notes |
|---|---|---|---|
| `sub` | A, C | All existing tools + session | IS UUID; always present |
| `username` | A, C | D3, D4, D5, E1, B1 | IS user attribute; must be mapped to access token claim set |
| `email` | A, C | D3, D5, A8 (roles derivation) | OIDC `email` scope; must flow into access token |
| `groups` | ID token (A exchange) | A8 (`ExchangeResponse.roles`) | IS groups claim; needed at login exchange only |

If `username` is absent from a token and a tool requires it, the MCP server must return 401
with `ERR-AUTH-007` (fail closed). It must **not** fall back to `sub` — the tool's business
logic (cubicle lookup by username, asset lookup by username) relies on the human-readable
identifier, not the UUID.

### 4.2 `JWTClaims` model additions

`common/auth/models.py:JWTClaims` currently has fields: `sub`, `iss`, `aud`, `exp`, `iat`,
`jti`, `act`, `scope`, `aut`. Sprint 4 adds:

```python
# In common/auth/models.py, inside the JWTClaims dataclass:

username: str | None = None   # IS user attribute claim; "username" is the IS default claim name
email: str | None = None      # OIDC email claim
groups: list[str] = field(default_factory=list)   # IS role group membership claim
```

The token validation code that populates `JWTClaims` must extract these from the decoded
payload if present. Absence is not a validation failure for `username` and `email` at the
`validate_token` level (some calls — e.g. healthcheck tokens — won't have them). Only
the specific tools listed in §4.1 enforce presence.

### 4.3 How MCP servers extract the claims

In the existing tool handler pattern (as shown in `hr_server/mcp/tools.py:206–219`), the
Bearer token is extracted and passed to the validator. After validation, the validated
`JWTClaims` object is available. Sprint 4 tools that need `username` do:

```python
# At the top of a tool handler (e.g. get_my_cubicle):
username = claims.username
if not username:
    raise HTTPException(
        status_code=401,
        detail={"error_id": "ERR-AUTH-007", "message": "username claim required but absent."}
    )
# Proceed with lookup using username
```

This pattern is consistent with how `claims.sub` is currently used in the existing tools.

---

## §5. Response envelopes — locked

**All list/collection endpoints** in Sprint 4 return the following envelope:

```python
{"data": [...], "count": N}
```

No `filters` echo-back (UC-15 / UC-16 are static tables; no server-side filter for Sprint 4).
No `next_cursor` or `total_pages` (no pagination in Sprint 4 — demo scale).

**Single-record / detail endpoints** return the record directly without an envelope. This
applies to: `MyCubicleResult`, `EmployeeLookupResult`, `AssignCubicleResult`, `LeaveActionResult`.

**MCP tools** that return a single object (D4 `get_my_cubicle`, D5 `lookup_employee`,
D3 `assign_cubicle`) return the record directly — this is consistent with the existing
`ApproveLeaveResult` pattern in `api-contracts.md` §4.

**The SPA** reads `response.data` for tables and `response.count` for tab count badges. The
empty-state check is `response.count === 0` (not `response.data.length`, in case the server
returns a non-zero count with truncated data on a partial error — though this should not
occur in practice).

---

## §6. Error codes

All new error codes reuse existing ERR- families. No new family prefix is introduced.

### New codes locked for Sprint 4

| Code | Family | Meaning | Raised by | HTTP status |
|---|---|---|---|---|
| `ERR-CUBICLE-001` | Custom business | `cubicle_already_occupied` — target cubicle is assigned to a different employee | HR Server MCP D3 | 200 (MCP business error body) |
| `ERR-CUBICLE-002` | Custom business | `cubicle_not_found` — no cubicle with the given ID | HR Server MCP D3 | 200 (MCP business error body) |
| `ERR-EMPLOYEE-001` | Custom business | `employee_lookup_failed` — `lookup_employee` returned `found=false` | HR Agent (not HR Server) | N/A (A2A `ErrorPayload.reason`) |
| `ERR-API-PROXY-001` | ERR-API | `upstream_unavailable` — orchestrator-proxied call received 5xx or connection error from backend service | Orchestrator (reports routes) | 502 |

**Reuse of existing codes:**

| Code | Reused for |
|---|---|
| `ERR-MCP-003` | Scope mismatch on all new MCP tools (D1–D5, E1) and new REST endpoints (B1–B5, C1) |
| `ERR-AUTH-006` | Token absent or expired on all new endpoints |
| `ERR-AUTH-007` | Missing required claim (`hr_read_rest` pre-flight failure at orchestrator; `username` claim absent in MCP tools) |
| `ERR-CIBA-005` | Admin denies consent on A6/A7 approve/reject flows |
| `ERR-AGENT-006` | Double-click guard on A6/A7 (same as `POST /api/chat` 429) |

**Note on `ERR-CUBICLE-001` / `ERR-CUBICLE-002`.** These are new business-logic codes but
they do not introduce a new `ERR-CUBICLE-` family at the HTTP/SSE level — they are surfaced
inside the `data` field of a 200 MCP response body or inside an A2A `ErrorPayload.reason`
string. The orchestrator translates them to inline chat copy via the copy deck. They are not
new HTTP error codes.

---

## §7. Out-of-scope endpoints (do not design for Sprint 4)

The following surfaces are explicitly deferred. They are recorded here to prevent scope creep
during Stage 6 and Stage 9 implementation.

| Endpoint | Why deferred |
|---|---|
| `POST /api/reports/leave-requests/bulk-approve` | Bulk operations frozen per `sprint-4.md` §5 item 10 |
| `GET /api/reports/leave-requests/{request_id}` (detail) | Not needed — table row carries all display fields |
| `GET /api/me/leave-history/{employee_username}` | Admin-view per-employee history; Sprint 4 only needs aggregate table |
| `DELETE /api/cubicle-assignments/{cubicle_id}` | Unassign/reclaim frozen per `sprint-4.md` §5 items 2–3 |
| `GET /api/assets/{asset_id}` | Asset-by-ID detail endpoint; drilldown is client-side filter of already-loaded data |
| `GET /api/reports/leave-requests?search=` | Full-text / employee-name search on the table; table is static for Sprint 4 |
| `GET /api/me/cubicle` (REST variant) | Self-service cubicle is MCP-only (`get_my_cubicle`); no REST needed |
| `POST /api/reports/cubicle-assignments` | Direct REST assignment path; cubicle assignment is MCP/CIBA only per UC-11 design |

---

## §8. Open questions for Stage 6 (technical architecture)

The following items must be resolved in the Stage 6 document. They are not blockers for this
API design, but Stage 6 must lock them before implementation begins.

**OQ-1 — Sub forwarding on `GET /api/me/leaves`.**
When the orchestrator proxies A1 to HR Server B1, how does HR Server know which user's leaves
to return? Two options: (a) orchestrator forwards `user_sub` as a query param, which leaks
the IS UUID in the URL; or (b) HR Server derives `sub` from the token-A Bearer claim. Option
(b) is strongly preferred (consistent with how MCP tools use `claims.sub`). HR Server B1
must be implemented to ignore any `user_sub` query parameter and derive identity from the token
only. Stage 6 must confirm and remove the query-param option from the service call chain.

**OQ-2 — `employee_email` in `store.leave_requests`.**
The existing `apply_leave` in `hr_server/service/hr_service.py:72–143` stores `user_sub` and
`user_name` but not `user_email`. The leave-requests reporting response (`B2`, `A3`) must
return `employee_email`. Stage 6 must decide: (a) add `user_email` to the leave request store
record at `apply_leave` time (preferred — no retrospective fix needed for future records);
or (b) join against `store.users` at query time. Option (a) is preferred for data locality and
because the `email` claim is present at CIBA time. The seed data must also be updated to include
email on pre-seeded leave records.

**OQ-3 — HR Server token-A audience for reporting endpoints.**
HR Server's existing `validate_token` enforces `aud == hr_agent's OAuth Client ID`. For the
new REST reporting endpoints (B1–B3), the inbound token is token-A whose audience is the
orchestrator's MCP Client ID — not the HR Agent's Client ID. Stage 6 must either: (a) relax
the audience check for the REST path only (risky — weakens the per-path guard); or (b) use a
separate IS Application (or API resource audience) for the HR Server REST path. The locked
architecture in `sprint-4.md` §8 says token-A is forwarded to HR Server directly — this implies
HR Server must accept the orchestrator's audience for REST endpoints. Stage 6 must resolve this
against the live IS token claims to determine which `aud` value token-A carries when forwarded
to `hr_server`. This is the question most likely to bite (see §9 final summary).

**OQ-4 — `get_my_cubicle()` username resolution for `hr_admin_user`.**
When `hr_admin_user` calls `get_my_cubicle`, the lookup is by `claims.username`. If the seed
data does not include a cubicle assignment for `hr_admin_user`, the tool correctly returns
`{assigned: false}`. Stage 6 must ensure the demo seed data includes a cubicle for the
`employee_user` account (otherwise UC-12 EX-1 is the only demo path for self-service). This
is a data seed concern, not an API design concern, but Stage 6 must include it in the seed
script checklist.

**OQ-5 — A2A `ConsentRequiredPayload` strict-mode compatibility.**
The existing `ConsentRequiredPayload` uses `model_config = ConfigDict(strict=True)` (as
observed in `common/a2a/models.py:70`). Adding `action_text: str | None = None` to a
strict-mode model is valid in Pydantic v2 — `None` is accepted. Stage 6 must run the existing
`common/a2a/` tests after applying the field addition to confirm no regression.

**OQ-6 — Reports handler file location.**
The prompt recommends `orchestrator/reports/routes.py` as the new file for A1, A3–A7. Stage 6
must confirm this is consistent with the existing router registration pattern in the orchestrator
`main.py` or `app.py` (not examined in Stage 5 input). The router must be included in the app's
`include_router` call with a tag to distinguish report routes in logs.

---

## §9. Testing hooks (Stage 10 preview)

Stage 10 will define the full test matrix. The following contract details are the minimum that
must be N-test-checked to protect the API contract:

1. **Envelope shape.** Every list endpoint (`A1`, `A3`, `A4`, `A5`, `B1`, `B2`, `B3`, `C1`)
   must return `{"data": [...], "count": N}`. Tests must assert `"data" in response` and
   `"count" in response` and must NOT assert on a bare array. The `total` field used by the
   existing `it_service.get_employee_assets` result is a different contract; the new endpoints
   do not use it.

2. **403 before backend contact.** For `A3` (`hr_read_rest` check) and `A4` (`hr_read_rest`
   check), the test must assert that when `employee_user` calls the endpoint, the response is
   403 AND the backend service received zero requests. This validates the orchestrator pre-flight
   is not bypassed. Use `httpx` mock or a request-count probe on the hr_server in the test
   fixture.

3. **Idempotent assign — two outcomes.** `D3 assign_cubicle` must be tested with:
   - Same `(cubicle_id, employee_username)` called twice: second call returns HTTP 200 with
     `success=True` and the same `assigned_at` as the first call.
   - Same `cubicle_id` with a different `employee_username`: returns HTTP 200 with
     `error="cubicle_already_occupied"` and `current_holder_username` set.

4. **`action_text` propagation.** When `assign_cubicle` is triggered via A6 approve flow, the
   SSE `ciba_url` event must carry `action_text` matching the pattern
   `"Approve {username}'s leave from {date}"`. The test checks the SSE event payload, not just
   the HTTP ack.

5. **`username` claim absent — fail closed.** A synthetic token without the `username` claim
   presented to D4 `get_my_cubicle` or E1 `get_my_assets` must return 401, not 500 or a
   fallback to `sub`.

6. **`roles` and `scopes` in `/auth/exchange`.** After login, the `ExchangeResponse` must
   contain non-empty `scopes` matching the token-A scope string and `roles` derived from the
   `groups` claim. If `groups` is absent, `roles` must be `[]` (not a 500).

---

## §10. References

- `docs/architecture/sprint-4.md` — binding sprint plan (§2 objectives, §6 scope lock,
  §7 identity model, §8 reporting data flow)
- `docs/architecture/sprint-4-stage-4-ux-design.md` — UX design (§4.1 approve pushback,
  §6 action_text, §7 click-to-consent flow, §10 open questions Q4/Q5)
- `docs/architecture/api-contracts.md` — existing contract shapes (§1 orchestrator models,
  §2 SSE events, §3 A2A, §4 MCP tools, §6 internal models, Appendix error codes)
- `docs/scope-policy.md` — locked scope naming convention
- `docs/use-cases/UC-11-hr-admin-assigns-cubicle.md` — 4-turn cubicle flow
- `docs/use-cases/UC-12-employee-self-service-asset-discovery.md` — dual-agent self-service
- `docs/use-cases/UC-13-employee-applies-for-leave.md` — leave apply + panel re-fetch
- `docs/use-cases/UC-14-employee-checks-own-leave-status.md` — panel / chat consistency
- `docs/use-cases/UC-15-hr-admin-pending-leaves-table.md` — pending leaves table + CIBA approve
- `docs/use-cases/UC-16-hr-admin-assignment-reporting-tables.md` — cubicle + device tabs
- `hr_server/service/hr_service.py` — existing service functions: `apply_leave` (line 72),
  `get_my_leave_requests` (line 54), `get_all_leave_requests` (line 148),
  `approve_leave_request` (line 196), `reject_leave_request` (line 243),
  `get_leaves_for_dashboard` (line 274)
- `it_server/service/store.py` — current asset seed (employee_id keys; Sprint 4 migrates to
  username keys)
- `orchestrator/auth/routes.py` — `ExchangeResponse` (line 172); `_extract_display_name`
  (line 256) for claim extraction pattern reference
- `common/auth/models.py` — `JWTClaims` dataclass (line 81); `OBOToken` (line 61)
- `common/a2a/models.py` — `ConsentRequiredPayload` location (strict-mode config at line 70)
