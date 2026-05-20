# API Contracts — Smart Employee Agent POC (Sprint 1)

**Date:** 2026-05-07
**Status:** Stage 4 sign-off artifact.
**Stack:** FastAPI · async Python 3.11+ · Pydantic v2 · httpx · SSE push
**Architecture:** orchestrator :8090 · hr_agent :8001 · it_agent :8002 · hr_server :8000 · it_server :8004
**Companion docs (same Stage 4 batch):** `module-layout.md`, `sequence-diagrams.md`

---

## Table of Contents

1. [Orchestrator REST API](#1-orchestrator-rest-api)
2. [SSE Event Payloads](#2-sse-event-payloads)
3. [A2A Protocol (orchestrator → specialists)](#3-a2a-protocol)
4. [MCP Tool Definitions](#4-mcp-tool-definitions)
5. [WSO2 IS Consumer Contracts](#5-wso2-is-consumer-contracts)
6. [Internal Data Models](#6-internal-data-models)

---

## 1. Orchestrator REST API

The orchestrator is the only component that faces the SPA. It owns session management
(cookie-keyed, single-process in-memory), Pattern C auth, and SSE push. Token-A never
leaves this process; the SPA only ever holds an opaque session cookie.

### Pydantic v2 request/response models + route table

The routes are all standard FastAPI; the table below gives the binding, the Pydantic
models give the wire shapes. Token-A never appears in any response body.

```
Route                         Method  Auth           Returns   Notes
─────────────────────────────────────────────────────────────────────────
GET  /auth/login?redirect=    —       none           302       Sets __Host-orch_pkce cookie (state, Max-Age=300, HttpOnly, Secure, SameSite=Lax); redirects to IS /oauth2/authorize
GET  /agent-callback          —       none           200 HTML   Registered redirect_uri on orchestrator-mcp-client. Returns a self-contained HTML relay page that POSTs {code, state} to /auth/exchange and navigates to / on success
POST /auth/exchange           JSON    none           200+cookie Creates session; sets orch_sid (HttpOnly, Secure, SameSite=Lax)
POST /auth/logout             —       orch_sid       200       Sprint 1: clears session+cookie. Sprint 3: also calls IS /oauth2/revoke + specialist cache-bust.
POST /api/chat                JSON    orch_sid       200 ack   Full response via SSE. Rejects 429 if request in-flight (ERR-AGENT-006).
POST /api/ciba/cancel         JSON    orch_sid       200       Cancels polling task for auth_req_id (ERR-CIBA-010)
GET  /events/{session_id}     —       orch_sid       200 SSE   text/event-stream; each event = data:<json>\n\n
GET  /healthz                 —       none           200       No auth; used by docker-compose health-check
```

```python
from pydantic import BaseModel, Field
from datetime import datetime
import uuid as _uuid


class ExchangeRequest(BaseModel):
    code: str
    state: str
    code_verifier: str

class ExchangeResponse(BaseModel):
    session_id: str
    user_display_name: str
    expires_at: datetime            # token-A expiry; SPA uses for re-auth UX (UC-06)

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4096)
    request_id: str = Field(default_factory=lambda: str(_uuid.uuid4()))
    # SPA SHOULD supply; orchestrator propagates as X-Request-ID on all downstream hops

class ChatAck(BaseModel):
    request_id: str                 # echoed so SPA can correlate SSE events

class CibaCancelRequest(BaseModel):
    auth_req_id: str
    request_id: str | None = None

class CibaCancelResponse(BaseModel):
    ok: bool
    auth_req_id: str

class LogoutResponse(BaseModel):
    ok: bool = True

class HealthResponse(BaseModel):
    status: str = "ok"
    uptime_s: float

class ErrorEnvelope(BaseModel):
    error_id: str           # ERR-AUTH-NNN | ERR-CIBA-NNN | etc.
    message: str            # user-facing copy from error-catalog.md
    request_id: str | None = None
```

---

## 2. SSE Event Payloads

All events are delivered as `data: <json>\n\n` on the `/events/{session_id}` stream.
The `type` field is the discriminant; the SPA switches on it to update the UI.
SSE is used rather than WebSocket because the push direction is exclusively
server-to-client — SSE's native browser `EventSource` auto-reconnects without
library code, and the single-direction constraint keeps the auth surface smaller
(the session cookie already authenticates the stream).

```typescript
// TypeScript shapes for the SPA. Each maps 1-to-1 to a Pydantic model in
// orchestrator/sse/events.py (§6 shows the Python side).

// Discriminant union — switch on `type`
type SseEvent =
  | RoutingEvent
  | CibaUrlEvent
  | CibaStateChangeEvent
  | ChatMessageEvent
  | SseErrorEvent;

// ── routing ──────────────────────────────────────────────────────────────────
// Emitted when the orchestrator has chosen a specialist and is about to send
// the A2A request. Allows the SPA to show "Routing to HR Agent..." before
// the CIBA widget appears. request_id ties back to the /api/chat call.
interface RoutingEvent {
  type: "routing";
  agent_id: string;       // e.g. "hr_agent"
  agent_label: string;    // e.g. "HR Agent"  — display name from AgentCard
  request_id: string;     // uuid
}

// ── ciba_url ─────────────────────────────────────────────────────────────────
// Emitted when a specialist has initiated CIBA and returned auth_url.
// The SPA renders the Consent Widget from this payload.
// `is_refresh` is true when the CIBA is a token-expiry re-auth (UC-06);
// the SPA should label the widget "Re-authorizing..." instead of "Authorizing...".
// `prior_consent_at` is set on re-auth to tell the widget when the user last
// approved this agent (so it can show "You approved this 47 minutes ago").
interface CibaUrlEvent {
  type: "ciba_url";
  request_id: string;
  agent_id: string;
  agent_label: string;
  action: string;          // plain-language summary from binding_message template
  auth_url: string;        // IS consent URL — open in new tab
  binding_code: string;    // short code shown in both the widget and the IS page
  expires_in: number;      // seconds until auth_req_id expires (typically 300)
  scope: string;           // space-separated scope string, for display
  is_refresh: boolean;
  prior_consent_at: string | null;  // ISO-8601 or null
}

// ── ciba_state_change ─────────────────────────────────────────────────────────
// Emitted by the orchestrator as polling progresses.
// Maps to the Consent Widget visual states in error-catalog.md §mapping.
//   VERIFYING — user clicked Approve on auth_url; IS confirmed; polling in progress
//   WORKING   — token received; MCP call in progress
//   DONE      — MCP returned; result is in the subsequent chat_message
//   DENIED    — user clicked Deny at IS (ERR-CIBA-005..008)
//   EXPIRED   — auth_req_id timed out (ERR-CIBA-009)
//   ERROR     — unrecoverable (ERR-CIBA-001..004, ERR-AGENT-*, ERR-MCP-*)
interface CibaStateChangeEvent {
  type: "ciba_state_change";
  request_id: string;
  agent_id: string;
  state: "VERIFYING" | "WORKING" | "DONE" | "DENIED" | "EXPIRED" | "ERROR";
  message?: string;   // optional user-facing copy for DENIED/EXPIRED/ERROR
}

// ── chat_message ──────────────────────────────────────────────────────────────
// The orchestrator's LLM-composed reply (or partial reply in denial scenarios).
// Always role="assistant". request_id links it back to the originating chat POST.
interface ChatMessageEvent {
  type: "chat_message";
  role: "assistant";
  content: string;
  request_id: string;
}

// ── error ─────────────────────────────────────────────────────────────────────
// Unrecoverable or session-level errors that must replace or interrupt the chat.
// `code` matches an ERR-* ID from error-catalog.md. request_id is optional
// because session-level errors (ERR-AUTH-009, ERR-INFRA-004) may occur outside
// a request context.
interface SseErrorEvent {
  type: "error";
  code: string;         // ERR-AUTH-NNN | ERR-CIBA-NNN | ERR-AGENT-NNN | ...
  message: string;      // user-facing copy from error-catalog.md
  request_id?: string;
}
```

---

## 3. A2A Protocol

A2A messages are JSON-RPC 2.0 sent over plain HTTPS POST. The method is always
`message/send`. Token-A is forwarded as the Bearer credential so the specialist can
validate that the caller is the trusted orchestrator-agent and extract the user's
`sub` as `login_hint` for CIBA. JSON-RPC 2.0 is used rather than a bespoke REST
shape because it provides a well-known envelope for method dispatch and structured
error objects, which maps cleanly onto the `consent_required` / `result` / `error`
response union without inventing a new discriminant scheme.

### Endpoint

```
POST http://<specialist>:<port>/a2a/message/send
```

### Required headers

```
Authorization: Bearer <token-A>
X-Request-ID: <uuid4>           # must be propagated from the originating /api/chat call
Content-Type: application/json
```

### Python models (Pydantic v2)

```python
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field
import uuid


# ── Request ───────────────────────────────────────────────────────────────────

class A2AParams(BaseModel):
    """Parameters for message/send."""
    tool: str            # MCP tool name, e.g. "get_leave_balance"
    args: dict[str, Any] = Field(default_factory=dict)
    # Passed through so the specialist can include it in its own MCP call header
    # and in the binding_message template (S1.11b).
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class A2ARequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    method: Literal["message/send"] = "message/send"
    params: A2AParams


# ── Response variants ─────────────────────────────────────────────────────────

class ConsentRequiredPayload(BaseModel):
    """
    Returned immediately when the specialist has initiated CIBA.
    The orchestrator forwards auth_url to the SPA via SSE and awaits a
    separate notification (an async callback or in-process queue entry)
    once polling completes. agent_label and action are display strings
    for the Consent Widget.
    """
    type: Literal["consent_required"]
    auth_req_id: str
    auth_url: str
    binding_code: str       # the short binding_message code shown to the user
    agent_label: str        # e.g. "HR Agent"
    action: str             # plain-language description, e.g. "View your leave balance"
    expires_in: int         # seconds (from IS /oauth2/ciba response)
    scope: str              # space-separated requested scopes


class ResultPayload(BaseModel):
    """
    Returned after polling completes and the MCP call succeeds.
    `data` is the raw MCP tool output; the orchestrator's LLM composes
    the user-facing sentence from it.
    """
    type: Literal["result"]
    data: dict[str, Any]
    # jti of the OBO token used — for the session map (S1.11a).
    token_jti: str
    token_exp: int   # unix epoch


class ErrorPayload(BaseModel):
    """
    Returned on CIBA failure (denied, expired) or MCP failure.
    reason maps to an ERR-* code for the orchestrator to surface correctly.
    """
    type: Literal["error"]
    reason: str      # ERR-CIBA-005 | ERR-CIBA-009 | ERR-MCP-004 | ...
    message: str     # ops-level detail (not shown to user)


A2AResponsePayload = ConsentRequiredPayload | ResultPayload | ErrorPayload


class A2ASuccessResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str
    result: A2AResponsePayload


class A2AErrorObject(BaseModel):
    """
    JSON-RPC error object. codes mirror the ERR-AGENT-* namespace.
    -32001 = token validation failure (ERR-AGENT-001 / ERR-AGENT-002)
    -32002 = missing X-Request-ID (ERR-AGENT-003)
    -32003 = unknown tool (ERR-AGENT-005)
    -32004 = CIBA initiation hard failure (ERR-CIBA-001..004)
    -32005 = internal specialist error (no ERR-* mapping; ops only)
    Standard JSON-RPC codes (-32700 parse error, -32600 invalid request,
    -32601 method not found) are also emitted where applicable.
    """
    code: int
    message: str
    data: dict[str, Any] | None = None


class A2AErrorResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str
    error: A2AErrorObject


A2AResponse = A2ASuccessResponse | A2AErrorResponse
```

### JSON-RPC error code table

| Code | Meaning | ERR-* mapping | HTTP status on the A2A endpoint |
|------|---------|---------------|--------------------------------|
| -32700 | Parse error | — | 400 |
| -32600 | Invalid request (malformed JSON-RPC envelope) | — | 400 |
| -32601 | Method not found | ERR-AGENT-005 (unknown tool) | 404 |
| -32001 | Token validation failure (bad sig, wrong iss, act.sub not in allowlist) | ERR-AGENT-001 / ERR-AGENT-002 | 401 |
| -32002 | Missing X-Request-ID | ERR-AGENT-003 | 400 |
| -32003 | Argument validation failure | ERR-LLM-002 | 422 |
| -32004 | CIBA initiation hard failure (misconfigured agent app) | ERR-CIBA-001..004 | 500 |
| -32005 | Internal specialist error | ERR-MCP-004 / ERR-MCP-005 | 502 |

### Wire example (request)

```json
{
  "jsonrpc": "2.0",
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "method": "message/send",
  "params": {
    "tool": "get_leave_balance",
    "args": {},
    "request_id": "f9e8d7c6-b5a4-3210-fedc-ba9876543210"
  }
}
```

### Wire example (consent_required response)

```json
{
  "jsonrpc": "2.0",
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "result": {
    "type": "consent_required",
    "auth_req_id": "60c3da35-...",
    "auth_url": "https://13.60.190.47:9443/authz/ciba?...",
    "binding_code": "ABC-123",
    "agent_label": "HR Agent",
    "action": "View your leave balance",
    "expires_in": 300,
    "scope": "openid hr.read"
  }
}
```

---

## 4. MCP Tool Definitions

MCP servers expose tools via the standard Model Context Protocol
(`application/json` over HTTP, per langchain-mcp-adapters >= 0.1.18 conventions).
The `aud` and `act.sub` are both checked on every request because `aud` alone is the
agent's OAuth Client ID (F6) — an attacker who knows the Client ID could craft a token
that passes an `aud`-only check; the `act.sub` check is the actual cross-agent defence
(threat model T5 / N16 / N21).

### Token validation contract (both MCP servers)

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class McpTokenValidationPolicy:
    """
    Injected at MCP server startup from environment variables.
    Validated on every inbound Bearer token before the tool is invoked.
    """
    expected_iss: str              # IS issuer URL, e.g. "https://13.60.190.47:9443/oauth2/token"
    expected_aud: str              # The paired agent's OAuth Client ID (F6).
                                   # Must equal EXPECTED_AGENT_OAUTH_CLIENT_ID env var.
                                   # Logged at startup for N28 collision detection.
    expected_act_sub: str          # The paired agent's Agent identity ID (UUID).
    required_scope_per_tool: dict[str, FrozenSet[str]]
    # e.g. {"get_leave_balance": frozenset({"hr.read"}),
    #        "approve_leave":     frozenset({"hr.write"})}
```

Validation sequence per request:
1. Verify JWT signature via IS JWKS (`/oauth2/jwks`).
2. Check `iss == expected_iss`.
3. Check `exp > now`.
4. Check `aud == expected_aud` (ERR-MCP-001).
5. Check `act.sub == expected_act_sub` (ERR-MCP-002).
6. Check tool's required scope set is a subset of `scope` claim (ERR-MCP-003).

### HR Server tools (hr_server :8000)

```python
from __future__ import annotations
from pydantic import BaseModel
from typing import Literal


# ── get_leave_balance ─────────────────────────────────────────────────────────
# Required scope: hr.read
# employee_id defaults to the `sub` claim of the inbound token (the user's UUID).
# Specialist passes no employee_id for self-service; managers pass an explicit one.

class GetLeaveBalanceArgs(BaseModel):
    employee_id: str | None = None  # defaults to token.sub

class LeaveBalanceResult(BaseModel):
    employee_id: str
    leave_days: int
    leave_type: str        # "annual" | "sick" | "unpaid"
    as_of_date: str        # ISO-8601 date


# ── get_leave_history ─────────────────────────────────────────────────────────
# Required scope: hr.read

class GetLeaveHistoryArgs(BaseModel):
    employee_id: str | None = None
    limit: int = 10        # max 50

class LeaveHistoryEntry(BaseModel):
    leave_id: str
    start_date: str
    end_date: str
    days: int
    status: Literal["approved", "pending", "rejected"]
    type: str

class GetLeaveHistoryResult(BaseModel):
    employee_id: str
    entries: list[LeaveHistoryEntry]


# ── approve_leave ─────────────────────────────────────────────────────────────
# Required scope: hr.write
# Manager-only. Sprint 1: canned response; scope validation is enforced even though
# approve_leave is not yet exposed in the demo query so it tests the scope guard.

class ApproveLeaveArgs(BaseModel):
    leave_id: str

class ApproveLeaveResult(BaseModel):
    leave_id: str
    status: Literal["approved"]
    approved_by: str       # act.sub of the token (the agent acting for the manager)
    approved_at: str       # ISO-8601 datetime


# ── MCP tool registry (used by langchain-mcp-adapters to expose the server) ──
HR_TOOLS: dict[str, type] = {
    "get_leave_balance": GetLeaveBalanceArgs,
    "get_leave_history": GetLeaveHistoryArgs,
    "approve_leave":     ApproveLeaveArgs,
}

HR_SCOPE_POLICY: dict[str, frozenset[str]] = {
    "get_leave_balance": frozenset({"hr.read"}),
    "get_leave_history": frozenset({"hr.read"}),
    "approve_leave":     frozenset({"hr.write"}),
}
```

### IT Server tools (it_server :8004)

```python
# ── list_available_assets ─────────────────────────────────────────────────────
# Required scope: it.read
# Returns asset catalogue (not user-specific). employee_id ignored.

class ListAvailableAssetsArgs(BaseModel):
    asset_type: str | None = None   # filter: "laptop" | "monitor" | "phone" | None

class AssetEntry(BaseModel):
    asset_id: str
    model: str
    type: str
    available_count: int

class ListAvailableAssetsResult(BaseModel):
    assets: list[AssetEntry]


# ── get_my_assets ─────────────────────────────────────────────────────────────
# Required scope: it.read
# Returns assets assigned to the requesting user (token.sub) or to employee_id
# when the caller has it.read and the sub is a manager.

class GetMyAssetsArgs(BaseModel):
    employee_id: str | None = None  # defaults to token.sub

class AssignedAsset(BaseModel):
    asset_id: str
    model: str
    type: str
    assigned_since: str   # ISO-8601 date

class GetMyAssetsResult(BaseModel):
    employee_id: str
    assets: list[AssignedAsset]


IT_TOOLS: dict[str, type] = {
    "list_available_assets": ListAvailableAssetsArgs,
    "get_my_assets":         GetMyAssetsArgs,
}

IT_SCOPE_POLICY: dict[str, frozenset[str]] = {
    "list_available_assets": frozenset({"it.read"}),
    "get_my_assets":         frozenset({"it.read"}),
}
```

---

## 5. WSO2 IS Consumer Contracts

Ground-truth wire shapes from probes C1 and C8. Parameters marked
`(empirical)` are verified against live IS 7.2. Parameters without that
tag are IS-documented defaults (not individually probed). The orchestrator
and specialist code must implement exactly these shapes — any deviation will
produce the error classes documented in F1–F7 of the capability memo.

### 5.1 `/oauth2/authorize` — Pattern C (SPA → IS)

```
GET https://<IS_HOST>/oauth2/authorize

Required parameters (application/x-www-form-urlencoded via query string):
  client_id             = <orchestrator-mcp-client OAuth Client ID>
  response_type         = code
  redirect_uri          = http://localhost:8090/agent-callback   (registered on the orchestrator-mcp-client app)
  scope                 = openid orchestrate
  state                 = <cryptographically random, 128-bit, base64url>
  code_challenge        = <S256(code_verifier)>
  code_challenge_method = S256
  requested_actor       = <orchestrator-agent UUID>          (empirical — C1 probe)

Non-standard parameter (empirical):
  requested_actor: IS 7.2 specific. Causes the consent screen to name the agent.
                   Produces act.sub in the issued token-A.

Note: the SPA is served by the orchestrator at :8090, so the callback lands back
on the orchestrator (GET /agent-callback) — there is no separate SPA host.

Happy-path redirect:
  http://localhost:8090/agent-callback?code=<code>&state=<state>

Denial redirect:
  http://localhost:8090/agent-callback?error=access_denied&state=<state>
```

### 5.2 `/oauth2/authn` — App-Native Auth (agents minting actor_token)

```
# Step 1: POST /oauth2/authorize with response_mode=direct
POST https://<IS_HOST>/oauth2/authorize
Authorization: Basic <base64(oauth_client_id:oauth_client_secret)>
Content-Type: application/x-www-form-urlencoded

  client_id             = <agent's Agent App OAuth Client ID>
  response_type         = code
  redirect_uri          = http://localhost:9999/agent-callback
  scope                 = openid internal_login
  response_mode         = direct                     (empirical — C4/C8)
  code_challenge        = <S256(verifier)>
  code_challenge_method = S256

Response (JSON):
  { "flowId": "<uuid>",
    "nextStep": { "authenticators": [{"authenticatorId": "<id>"}] } }

# Step 2: POST /oauth2/authn  (empirical — C4/C8)
POST https://<IS_HOST>/oauth2/authn
Content-Type: application/json

  {
    "flowId": "<flowId>",
    "selectedAuthenticator": {
      "authenticatorId": "<authenticatorId>",
      "params": { "username": "<agent_id>", "password": "<agent_secret>" }
    }
  }

Response (JSON):
  { "authData": { "code": "<auth_code>" } }
  # or top-level "code" key on some IS versions — handle both (C8 line 107)

# Step 3: POST /oauth2/token (auth code exchange)
POST https://<IS_HOST>/oauth2/token
Authorization: Basic <base64(oauth_client_id:oauth_client_secret)>
Content-Type: application/x-www-form-urlencoded

  grant_type    = authorization_code
  client_id     = <agent's Agent App OAuth Client ID>
  code          = <auth_code>
  code_verifier = <verifier>
  redirect_uri  = http://localhost:9999/agent-callback

Response:
  { "access_token": "<actor_token>", "token_type": "Bearer", ... }
  # actor_token claims: sub=<agent UUID>, aut=AGENT, scope="openid internal_login"
```

### 5.3 `/oauth2/token` — auth code exchange (Pattern C backend leg)

```
POST https://<IS_HOST>/oauth2/token
Authorization: Basic <base64(orchestrator-mcp-client-id:secret)>
Content-Type: application/x-www-form-urlencoded

  grant_type         = authorization_code
  client_id          = <orchestrator-mcp-client OAuth Client ID>
  code               = <code from callback>
  code_verifier      = <original PKCE verifier>
  redirect_uri       = http://localhost:8090/agent-callback
  actor_token        = <orchestrator-agent's I4 token>      (empirical — C1)
  actor_token_type   = urn:ietf:params:oauth:token-type:access_token

  # actor_token MUST be in the POST body, NOT the Authorization header (empirical — C1)

Response (token-A):
  {
    "access_token":  "<token-A>",
    "token_type":    "Bearer",
    "expires_in":    3600,
    "scope":         "openid orchestrate",
    "id_token":      "<id_token>"
  }

  token-A decoded claims (empirical — I1):
  {
    "sub": "<user UUID>",
    "aut": "APPLICATION_USER",
    "iss": "https://13.60.190.47:9443/oauth2/token",
    "act": { "sub": "<orchestrator-agent UUID>" },
    "aud": "<orchestrator-mcp-client OAuth Client ID>",
    "scope": "openid orchestrate",
    "exp": <now + 3600>
  }
```

### 5.4 `/oauth2/token` — client_credentials (internal use, e.g. orchestrator healthcheck)

```
POST https://<IS_HOST>/oauth2/token
Authorization: Basic <base64(client_id:secret)>
Content-Type: application/x-www-form-urlencoded

  grant_type = client_credentials
  scope      = openid

Response:
  { "access_token": "...", "token_type": "Bearer", "expires_in": 3600 }
```

### 5.5 `/oauth2/ciba` — specialist initiates CIBA (empirical — C8)

```
POST https://<IS_HOST>/oauth2/ciba
Authorization: Basic <base64(agent_oauth_client_id:agent_oauth_client_secret)>
Content-Type: application/x-www-form-urlencoded
Accept: application/json

  scope                = openid hr.read            (or it.read; space-separated)
  login_hint           = <user_sub from token-A>   (user's UUID — empirical C8)
  binding_message      = "<agent_label> wants to <action> — request <req_id_short>"
                         # template enforced at ciba_client.initiate_ciba() (S1.11b)
  actor_token          = <agent's I4 token (App-Native Auth output)>
  notification_channel = external                  (must be pre-configured on Agent App)

  # multi-audience resource= parameter is silently ignored (F6).
  # Do NOT pass resource= — it produces no effect and may confuse future readers.
  # Do NOT pass offline_access in scope — no refresh tokens per T8/Q3 decision (F7).

Response (200 OK):
  {
    "auth_req_id": "<opaque string>",
    "auth_url":    "https://13.60.190.47:9443/authz/ciba?...",
    "interval":    2,
    "expires_in":  300
  }

Error responses:
  400 unauthorized_client  — CIBA grant not enabled on Agent App (ERR-CIBA-001)
  400 invalid_request      — notification_channel not set to external (ERR-CIBA-002)
  400 invalid_scope        — scope not registered on the Agent App (ERR-CIBA-003)
  400 invalid_request      — malformed login_hint (ERR-CIBA-004)
```

### 5.6 `/oauth2/token` — CIBA polling (empirical — C8)

```
POST https://<IS_HOST>/oauth2/token
Authorization: Basic <base64(agent_oauth_client_id:agent_oauth_client_secret)>
Content-Type: application/x-www-form-urlencoded

  grant_type   = urn:openid:params:grant-type:ciba
  auth_req_id  = <from /oauth2/ciba response>

  # actor_token is NOT sent on polling — empirical finding F5 (C8 probe).
  # IS carries the actor binding internally via auth_req_id.

Polling responses:
  200 { "access_token": "...", "expires_in": 3600, "scope": "openid hr.read" }
      → token-B / token-C issued; poll loop exits

  400 { "error": "authorization_pending" }
      → user has not yet clicked Approve; sleep `interval` seconds and retry

  400 { "error": "slow_down" }
      → back off; increase interval by 5s

  400 { "error": "expired_token" }
      → auth_req_id expired before consent; surface ERR-CIBA-009

  400 { "error": "access_denied" }
      → user clicked Deny; surface ERR-CIBA-005..008 depending on context

  token-B decoded claims (empirical — I8):
  {
    "sub": "<user UUID>",            # same as token-A.sub
    "aut": "APPLICATION_USER",
    "iss": "https://13.60.190.47:9443/oauth2/token",
    "act": { "sub": "<agent UUID>" },
    "aud": "<agent's OAuth Client ID>",   # F6: NOT a resource URI
    "scope": "openid hr.read",
    "exp":  <now + 3600>
  }
```

---

## 6. Internal Data Models

These are the shared runtime types. They live in `common/models.py` (single source of
truth imported by orchestrator, hr_agent, it_agent). `OBOToken` carries the decoded
JWT claims alongside the raw string to avoid repeated decode overhead and to make
claim access type-safe throughout the codebase.

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import FrozenSet, Literal


# ── OAuthToken ────────────────────────────────────────────────────────────────
# Wraps a raw token response from any IS /oauth2/token call.
# expires_at_utc is pre-computed at creation time so callers never have to
# add expires_in to wall-clock themselves, avoiding timezone bugs.

@dataclass
class OAuthToken:
    access_token: str
    scope: str
    expires_in: int           # seconds, as returned by IS
    expires_at_utc: datetime  # = utcnow() + timedelta(seconds=expires_in)
    refresh_token: str | None = None
    id_token: str | None = None

    def is_expired(self, buffer_s: int = 30) -> bool:
        """True if the token will expire within buffer_s seconds."""
        return datetime.now(tz=timezone.utc).timestamp() >= (
            self.expires_at_utc.timestamp() - buffer_s
        )


# ── OBOToken ──────────────────────────────────────────────────────────────────
# An OAuthToken whose access_token has been decoded and whose act claim has
# been validated. Used for token-A (orchestrator session) and token-B/C
# (specialist OBO tokens). The sub/act_sub fields are always present — if
# they are missing the token is rejected before this dataclass is constructed.

@dataclass
class OBOToken:
    raw: OAuthToken
    sub: str          # user UUID
    act_sub: str      # agent UUID (act.sub claim)
    aud: str          # audience (orchestrator-mcp-client client ID or agent OAuth client ID)
    iss: str          # token issuer URL
    jti: str          # JWT ID — used for the session map and revocation ledger
    scope: str        # space-separated scope string

    @property
    def access_token(self) -> str:
        return self.raw.access_token

    def is_expired(self, buffer_s: int = 30) -> bool:
        return self.raw.is_expired(buffer_s)


# ── CibaState ─────────────────────────────────────────────────────────────────
# Tracks a single in-flight CIBA round-trip within a specialist agent process.
# polling_task is an asyncio.Task so the orchestrator can cancel it when the
# user closes the browser (UC-05) or calls /api/ciba/cancel (UC-04 variant B).

CibaStatus = Literal[
    "INITIATED",   # POST /oauth2/ciba succeeded; waiting for user
    "VERIFYING",   # user opened auth_url; IS confirmed; polling in progress
    "WORKING",     # token received; MCP call in progress
    "DONE",        # MCP call returned; result sent to orchestrator
    "DENIED",      # user denied (access_denied from IS)
    "EXPIRED",     # auth_req_id expired before consent
    "CANCELLED",   # cancelled by orchestrator (browser closed / widget cancel)
    "ERROR",       # unrecoverable error during CIBA or MCP
]

@dataclass
class CibaState:
    auth_req_id: str
    agent_id: str
    request_id: str           # X-Request-ID correlation
    started_at: datetime
    expires_at: datetime      # started_at + timedelta(seconds=expires_in)
    status: CibaStatus = "INITIATED"
    polling_task: asyncio.Task | None = None
    obo_token: OBOToken | None = None   # populated when DONE
    error_code: str | None = None        # ERR-* code if status == ERROR


# ── Session ───────────────────────────────────────────────────────────────────
# The orchestrator's in-memory session record, keyed by session_id (cookie value).
# token_a is never sent to the SPA — it lives here only.
# sse_queue: the asyncio.Queue that feeds the /events/{session_id} SSE generator.
# pending_ciba: at most one entry per agent_id in the serial fan-out model (Q2).
# cached_obo: allows specialists to cache token-B/C across requests within TTL.
#   Key is (agent_id, scope_frozenset) to invalidate per-scope if needed.

@dataclass
class Session:
    session_id: str
    user_sub: str
    user_display_name: str
    token_a: OBOToken
    sse_queue: asyncio.Queue              # type: asyncio.Queue[dict]
    pending_ciba: dict[str, CibaState] = field(default_factory=dict)
    # key: auth_req_id
    cached_obo: dict[tuple[str, FrozenSet[str]], OBOToken] = field(default_factory=dict)
    # key: (agent_id, frozenset(scopes))
    # Sprint 3 — session map for revocation (S1.11a skeleton):
    completed_ciba_log: list[dict] = field(default_factory=list)
    # each entry: {agent_id, auth_req_id, jti, exp, completed_at}

    def is_token_a_expired(self) -> bool:
        return self.token_a.is_expired()


# ── AgentCard ─────────────────────────────────────────────────────────────────
# Static configuration for each specialist. Loaded at orchestrator startup from
# environment variables or a YAML file. public_key_jwks_uri is reserved for
# signed A2A envelopes (roadmap); not used in Sprint 1.
# allowed_scopes is the union of all scopes the agent may request via CIBA —
# used by the orchestrator to pre-validate LLM tool calls before sending A2A.

@dataclass(frozen=True)
class AgentCard:
    id: str                    # e.g. "hr_agent"
    label: str                 # e.g. "HR Agent"
    base_url: str              # e.g. "http://hr_agent:8001"
    public_key_jwks_uri: str   # e.g. "http://hr_agent:8001/.well-known/jwks.json"
    allowed_scopes: FrozenSet[str]
    available_tools: FrozenSet[str]
    oauth_client_id: str       # the agent's Agent App OAuth Client ID
    # used by MCP servers to log N28 collision detection at startup
```

---

## Appendix — Error code quick-reference

The table below maps every code the SPA may receive (via SSE `error.code` or HTTP
`ErrorEnvelope.error_id`) to the module that raises it and the widget state it triggers.
Canonical copy is `docs/ux/error-catalog.md`.

| Code | Origin | Widget state |
|------|--------|-------------|
| ERR-AUTH-001..005 | orchestrator auth routes | Full-page error (Critical) |
| ERR-AUTH-006..009 | token validation in orchestrator or specialists | Full-page error (Critical) |
| ERR-AUTH-010 | orchestrator /auth/logout | Toast (Medium) |
| ERR-CIBA-001..004 | specialist CIBA initiation | ERROR (red banner) |
| ERR-CIBA-005..008 | specialist CIBA polling (user denied) | DENIED |
| ERR-CIBA-009 | specialist CIBA polling (expired_token) | EXPIRED |
| ERR-CIBA-010..012 | orchestrator cancel / SPA skip | CANCELLED |
| ERR-AGENT-001..002 | specialist A2A inbound validation | ERROR |
| ERR-AGENT-003 | specialist A2A missing header | 400 to orchestrator |
| ERR-AGENT-004..005 | orchestrator routing | Inline chat (High) |
| ERR-AGENT-006 | orchestrator /api/chat | 429 to SPA |
| ERR-MCP-001..002 | MCP server token validation | ERROR |
| ERR-MCP-003 | MCP server scope check | Inline chat (High) |
| ERR-MCP-004..005 | MCP server backend error | Inline chat (High) |
| ERR-INFRA-001..005 | any component IS connectivity | Full-page error (Critical) |
| ERR-LLM-001..004 | orchestrator LLM routing | Inline chat (High) |
