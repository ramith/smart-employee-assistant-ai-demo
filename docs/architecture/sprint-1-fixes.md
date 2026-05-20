# Sprint 1 — Stage 5 Council Fixes (binding addendum)

**Stage:** 4 close-out v2 / Stage 5 result
**Status:** Locked. Stage 6 implementing agents MUST read this BEFORE writing code; it overrides the source docs where conflicts exist.

This doc resolves every blocker the Stage 5 council surfaced. Each fix is keyed `F-NN` and references the council finding that surfaced it.

---

## F-01 — Two-phase A2A dispatch protocol (the biggest gap)

**Source finding:** python-pro #2, #4 ("how does `chat/routes.py` get the specialist's `poll_task`?"); architect §5.1 / §5.3.

**Decision:** **Two-call A2A pattern.**

```
Orchestrator → POST /a2a/message/send {tool, args}
Specialist  → response: {type: "consent_required", auth_req_id, auth_url, agent_label, action, scope, expires_in, is_refresh, prior_consent_at?}
              (specialist starts polling /oauth2/token in a background task,
               keyed by auth_req_id in its in-process map)

Orchestrator → SSE push {type: "ciba_url", ...} to SPA
Orchestrator → POST /a2a/await {auth_req_id} (the SECOND call)
Specialist  → long-polls own internal asyncio.Event, returns:
              - {type: "result", payload, token_jti, token_exp, token_iat} on success
              - {type: "error", error_id: "ERR-CIBA-NNN", reason} on denial/expiry/error
```

**Why two-call, not long-polling on the first:**
- The orchestrator MUST receive `auth_url` synchronously to push it to the SPA before the user can act. Holding the first POST open until token arrival would delay this push.
- The orchestrator can `await` the second POST for up to `expires_in + 30s` (default ~330s). Single HTTP connection, idempotent (specialist's map is keyed by auth_req_id).
- Cancellation: orchestrator can `POST /a2a/cancel {auth_req_id}` to abort the background polling task.

**New endpoint added** (extends `api-contracts.md` §3):
- `POST /a2a/await` — body `{auth_req_id: str}`, response is the discriminated union above.
- `POST /a2a/cancel` — body `{auth_req_id: str}`, response `{cancelled: bool}`.

**Specialist-side state:**
```python
# In hr_agent/it_agent specialist process (NOT shared with orchestrator process)
class SpecialistState:
    pending: dict[str, PendingCiba]  # keyed by auth_req_id

@dataclass
class PendingCiba:
    auth_req_id: str
    poll_task: asyncio.Task[OBOToken]
    completion: asyncio.Event
    result: OBOToken | None = None
    error: BaseException | None = None
```

The poll_task's `add_done_callback` sets `result`/`error` and triggers `completion.set()`. The `/a2a/await` handler does `await asyncio.wait_for(state.completion.wait(), timeout=...)` then returns `result` or raises `error`.

**Module-layout impact:**
- `common/a2a/server.py` — its `DispatchFn` now returns `ConsentRequired` and registers a follow-up handler. Add a second factory `build_a2a_await_router(state)`.
- `common/a2a/client.py` — adds `await_completion(auth_req_id, timeout)` method.
- `hr_agent/ciba/orchestrator.py` and `it_agent/ciba/orchestrator.py` — own the in-process `pending` dict and `add_done_callback` wiring.

---

## F-02 — Canonical `OBOToken` shape

**Source finding:** architect #2, python-pro #4-jti.

**Locked shape (place in `common/auth/models.py`):**
```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass(frozen=True, slots=True)
class OAuthToken:
    """Raw IS response wrapper. NEVER serialize via Pydantic — has asyncio types nowhere."""
    access_token: str
    token_type: str            # always "Bearer"
    expires_in: int            # IS-issued seconds-from-now
    expires_at: datetime       # computed at issuance: now + expires_in
    refresh_token: str | None  # only set if offline_access requested (T8/Q3: do not request)
    scope: str                 # space-separated
    id_token: str | None       # CIBA may issue; we don't use it

@dataclass(frozen=True, slots=True)
class OBOToken:
    """User-on-behalf-of token. Wraps OAuthToken plus DECODED + VERIFIED claims."""
    raw: OAuthToken
    sub: str          # user UUID
    act_sub: str      # specialist agent ID (depth-1 act.sub)
    aud: str          # specialist's OAuth Client ID (per F6)
    iss: str          # IS issuer URL
    iat: datetime
    jti: str          # see F-08 — required, not Optional

    def is_expired(self, buffer_s: int = 30) -> bool:
        return datetime.utcnow() >= self.raw.expires_at.replace(tzinfo=None) - timedelta(seconds=buffer_s)
```

**Resolves:**
- field name conflicts (`raw` vs flat fields, `iss` presence)
- type for `exp` (always `datetime` internally; serialize as int Unix seconds at API boundary)
- `jti` is non-optional (see F-08)

---

## F-03 — Canonical A2A response union

**Source finding:** architect #2.

**Locked names** (place in `common/a2a/models.py` as Pydantic v2 `BaseModel`s):

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

class ConsentRequiredPayload(BaseModel):
    type: Literal["consent_required"] = "consent_required"
    auth_req_id: str
    auth_url: str
    agent_label: str         # e.g. "HR Agent"
    action: str              # plain-language, e.g. "View your leave balance"
    scope: str               # space-separated
    binding_message: str     # the verbatim string IS will render
    expires_in: int          # seconds
    is_refresh: bool = False
    prior_consent_at: datetime | None = None

class ResultPayload(BaseModel):
    type: Literal["result"] = "result"
    data: dict                  # tool-specific result body
    token_jti: str
    token_exp: int              # Unix seconds (integer at the wire)
    token_iat: int              # Unix seconds

class ErrorPayload(BaseModel):
    type: Literal["error"] = "error"
    error_id: str               # one of ERR-CIBA-* / ERR-MCP-* / ERR-AGENT-*
    reason: str                 # short technical description (NOT user-facing)

A2AResponse = Annotated[
    Union[ConsentRequiredPayload, ResultPayload, ErrorPayload],
    Field(discriminator="type"),
]
```

**Locked names:** `data` (NOT `payload`); `token_exp: int` (Unix seconds, NOT float). Both names match `api-contracts.md §3` — `module-layout.md` `common/a2a/models.py` description must be updated to follow these.

`action_summary` is **dropped** in favor of `action` (matches api-contracts and copy-deck).

---

## F-04 — MCP token validator: `trusted_act_subs` is a SET

**Source finding:** architect #2.

`hr_server/auth/validators.py` and `it_server/auth/validators.py`:

```python
@dataclass(frozen=True)
class McpTokenValidationConfig:
    expected_aud: str                    # the paired agent's OAuth Client ID
    trusted_act_subs: frozenset[str]     # SET — usually one element, but allow multi-agent shared MCP later
    expected_iss: str
    jwks_url: str
    required_scopes: frozenset[str]      # tool-specific
```

The validator's 6-step check is:
1. JWT signature via JWKS
2. `iss == config.expected_iss`
3. `exp > now`
4. `aud == config.expected_aud`
5. `act.sub ∈ config.trusted_act_subs`
6. `config.required_scopes.issubset(token.scopes)`

On any failure: HTTP 401 with body `{"error_id": "ERR-MCP-NNN", "request_id": "..."}` per F-07 below.

---

## F-05 — Canonical `binding_message` template (single source of truth)

**Source finding:** architect #5.

**Source of truth:** `docs/ux/copy-deck.md` §5 / §6.

Implementations must import the templates from a single Python constant in `common/auth/binding_messages.py`:

```python
# common/auth/binding_messages.py
FRESH = "{agent_label} wants to {action} on your behalf — request {request_id_short}"
REFRESH = "{agent_label}'s previous access has expired — re-approve to {action} on your behalf — request {request_id_short}"

def render(template: str, *, agent_label: str, action: str, request_id: str) -> str:
    return template.format(agent_label=agent_label, action=action, request_id_short=request_id[:8])
```

`ciba_client.initiate()` accepts a pre-rendered `binding_message: str`. Callers (specialist's `ciba/orchestrator.py`) MUST use `render()` — never construct strings inline.

---

## F-06 — SSE session_id authentication (security gap)

**Source finding:** security #5.

`GET /events/{session_id}` (api-contracts §1) currently authenticates via `orch_sid` cookie but accepts any `session_id` in the path.

**Fix:** the SSE endpoint MUST assert `path_session_id == cookie.orch_sid` and reject mismatch with HTTP 403 + `{"error_id": "ERR-AUTH-009", "request_id": "..."}`.

```python
# orchestrator/events/sse_router.py
@router.get("/events/{session_id}")
async def stream(session_id: str, request: Request):
    cookie_sid = request.cookies.get("orch_sid")
    if not cookie_sid or cookie_sid != session_id:
        raise HTTPException(403, {"error_id": "ERR-AUTH-009", "request_id": ...})
    ...
```

Add `ERR-AUTH-009` to `error-catalog.md` if not present: "Cross-session SSE subscription attempt — path session_id does not match cookie."

---

## F-07 — MCP failure body shape

**Source finding:** security #3.

All MCP servers, on token validation failure: HTTP 401, body:
```json
{"error_id": "ERR-MCP-NNN", "request_id": "<X-Request-ID echo>"}
```

Specialist (hr_agent / it_agent) receives the 401, wraps as A2A `ErrorPayload(error_id="ERR-MCP-NNN", reason="<from log>")`, returns to orchestrator. Orchestrator's LLM is informed via tool error; user sees the friendly message from `error-catalog.md`.

---

## F-08 — `jti` is required; orchestrator generates fallback if IS omits

**Source finding:** python-pro #4.

**Decision:** assume IS always issues `jti` (it does on WSO2 IS 7.2 — verified by C8 probe payload). If it ever doesn't, `JWTValidator.validate()` raises `ERR-AUTH-010 missing_jti`. Do **not** fall back to a locally-generated UUID — the jti is a security-relevant claim used for revocation in Sprint 3, must come from IS.

`OBOToken.jti: str` (non-optional). `JWTValidator` verifies `jti` is present and non-empty.

---

## F-09 — `dataclass` vs Pydantic boundary

**Source finding:** python-pro #4.

**Rule:** any type that holds an `asyncio.Task`, `asyncio.Event`, `asyncio.Queue`, or any other non-serializable runtime object MUST be `@dataclass`. Pydantic `BaseModel` is reserved for types that cross HTTP / SSE boundaries.

This means:
- `Session`, `PendingCiba`, `CibaState`, `SpecialistState` — **dataclass**
- `OAuthToken`, `OBOToken`, `JWTClaims`, `AgentCard` — **dataclass** (could be Pydantic but no benefit and easier to keep consistent)
- `ChatRequest`, `ChatAck`, `ConsentRequiredPayload`, `ResultPayload`, `ErrorPayload`, `ExchangeRequest`, all SSE event payloads — **Pydantic v2** `BaseModel` (need JSON serialization)

State this at the top of `common/auth/models.py` and `common/a2a/models.py` so parallel implementers don't drift.

---

## F-10 — `asyncio.Task` defensive coding rules

**Source finding:** python-pro #5.

`hr_agent/ciba/orchestrator.py` and `it_agent/ciba/orchestrator.py` MUST follow these rules for the polling task:

```python
# 1. Catch only specific exceptions in the poll loop. NEVER `except Exception` or `except BaseException`.
async def poll_for_token(...):
    while not deadline_passed:
        try:
            response = await self.session.post(token_url, ...)
        except (CIBADeniedError, CIBAExpiredError):
            raise  # re-raise; will bubble to add_done_callback
        except httpx.NetworkError as e:
            log.warning("ciba_poll_network_error", exc_info=e)
            await asyncio.sleep(self.interval)
            continue
        # CancelledError is BaseException — NEVER caught here, propagates correctly

# 2. Wire add_done_callback to surface results/errors to the SpecialistState.completion event.
def _on_done(task: asyncio.Task[OBOToken]) -> None:
    if task.cancelled():
        state.error = asyncio.CancelledError()
    elif (exc := task.exception()) is not None:
        state.error = exc
    else:
        state.result = task.result()
    state.completion.set()

state.poll_task = asyncio.create_task(poll_for_token(...))
state.poll_task.add_done_callback(_on_done)

# 3. After done/cancelled, set state.poll_task = None so retries don't see stale handle.
# This goes inside _on_done() AFTER setting completion.
```

These three rules go into `common/auth/ciba_client.py`'s docstring as REQUIRED for callers, and into `module-layout.md`'s `ciba/orchestrator.py` description.

---

## F-11 — `RedactionFilter.filter()` correctness

**Source finding:** python-pro #6.

`common/logging/redaction.py`:

```python
class RedactionFilter(logging.Filter):
    """Strips JWT-shaped strings, auth_req_id values, and known secret patterns from log records.

    Mutates record.msg AND record.args (latter via tuple replacement, since tuples are immutable).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(str(record.msg))
        if record.args:
            # tuples are immutable; rebuild
            record.args = tuple(_redact(str(a)) for a in record.args)
        return True
```

NEVER write `record.args[i] = ...` — tuples are immutable; that raises TypeError. Always rebuild as a new tuple.

---

## F-12 — Wave reassignments

**Source finding:** python-pro #3.

- `orchestrator/agent_registry/cards.py` — move from W6 → **W4** (only deps are `common/a2a/agent_card.py` in W2)
- `orchestrator/agent_registry/discovery.py` — move from W6 → **W4** (only deps are `common/a2a/client.py` in W3)
- `orchestrator/chat/llm.py` — stays in W6 (genuinely depends on `agent_registry`)
- `hr_agent/mcp/client.py`, `it_agent/mcp/client.py` — stay in W5 BUT module spec MUST use dependency injection in `__init__` (no hard-coded `os.getenv`). Add this constraint to `module-layout.md`.

---

## F-13 — SSE event vocabulary final

**Source finding:** sprint-1.md §2 + naming review.

**Locked 6-event set:**
1. `session_ready` — `{type, user_label, server_time}` (on SSE connect)
2. `routing` — `{type, request_id, agent_label}` (orchestrator chose to call a specialist)
3. `ciba_url` — `{type, request_id, agent_id, agent_label, action, auth_url, binding_code, expires_in, scope, is_refresh, prior_consent_at}`
4. `ciba_state_change` — `{type, request_id, state: "VERIFYING"|"WORKING"|"DONE"|"DENIED"|"EXPIRED"|"ERROR", message?}`
5. `chat_message` — `{type, role: "assistant", content, request_id}`
6. `error` — `{type, error_id, message, request_id?}`

NO `widget_state`, NO `partial_result`. Partial results are delivered as a `chat_message` with the partial-result copy from `copy-deck.md` §7. `widget_state` is renamed to `ciba_state_change` (it was the same concept).

**Update `sequence-diagrams.md`** with a find-and-replace `widget_state` → `ciba_state_change` (single edit). [tracked in todos]

---

## F-14 — Operational locks (PM)

- **`LLM_FALLBACK_MODE=keyword`** is on by default in the demo runbook (NOT just a debug option). Keyword rules: `"leave"|"vacation"|"time off" → hr_agent.get_leave_balance`; `"laptop"|"asset"|"equipment" → it_agent.list_available_assets`.
- **Demo rehearsal** is a Sprint 1 task (S1.10) with stopwatch — must complete in ≤85s end-to-end. If exceeds, drop to single-specialist demo.

---

## F-15 — N28 hardening (security)

In addition to the runtime N28 boot-test, add a CI-level assertion in the test suite:
```python
def test_no_oauth_client_id_collision():
    """All specialists must have distinct OAuth Client IDs (T9 mitigation)."""
    ids = {
        os.environ["HR_AGENT_OAUTH_CLIENT_ID"],
        os.environ["IT_AGENT_OAUTH_CLIENT_ID"],
        os.environ["ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID"],
    }
    assert len(ids) == 3, "OAuth Client IDs collide; tokens would silently route across specialists"
```

---

## F-17 — MCP Server registration on IS 7.2 is metadata-only (CIBA `aud` correction)

**Source:** Stage 8 prep council review (architect, python-pro, security) + empirical c10 probe on 2026-05-07.

**Background.** F-06 in `docs/spikes/wso2-is-capability-memo.md` stated multi-resource CIBA was unsupported (aud collapsed to client_id). The user discovered IS 7.2 Console has a dedicated **"Add new MCP server"** registration entity with URI identifiers like `mcp://hr-server.local` — which suggested F-06 might have been over-broad and a single-resource CIBA against a registered MCP Server might bind `aud` correctly.

**Probe (`idp_capability_test/c10_ciba_single_resource.py`):** Registered `mcp://probe-hr-server.local` as MCP Server in IS Console with scope `probe.hr_mcp_read`; subscribed `probe-agent-b`'s Agent App to it. Ran CIBA with `resource=mcp://probe-hr-server.local` AND `scope="openid probe.hr_mcp_read"`.

**Outcome:** Token returned with `aud = <probe-agent-b's OAuth Client ID>` (NOT the MCP URI). Scope reduced to `openid` only — the `probe.hr_mcp_read` scope was stripped. Both the `resource=` parameter AND the requested MCP scope were silently ignored on the CIBA grant path.

**Conclusions:**
1. **F-06 confirmed for single-resource case.** IS 7.2 CIBA does NOT honor `resource=` parameter; aud always collapses to the calling OAuth Client ID.
2. **MCP Server registration is metadata-only on the CIBA path.** The Console's MCP Server entity is useful for: (a) RFC 9728 protected-resource-metadata discovery, (b) scope catalog management. It does NOT participate in CIBA token issuance audience-binding.
3. **The WSO2 reference at `mcp-auth/python/main.py` confirms this** — it uses `audience=client_id` even though the sample is built around the MCP-Server-registration concept.

**Decision (Path C Hybrid, simplified to Path A given probe outcome):** Validator stays at `aud == <agent's OAuth Client ID>`. Apply the architect's future-proofing recommendation as a low-cost type change:

`hr_server/auth/validators.py` and `it_server/auth/validators.py`:
```python
# OLD:
expected_aud: str
# NEW:
expected_aud: frozenset[str]   # accept any token whose aud is in this set
```

This makes the validator forward-compatible if WSO2 fixes CIBA's `resource=` handling in a future IS release (or if we adopt a different grant flow that DOES honor resource indicators); the env var continues to provide a single value for now (parsed into a one-element frozenset).

**Sprint impact: deferred to Sprint 2 polish.** Probe outcome locked Path A — `aud` is always the agent's OAuth Client ID; the frozenset is always size 1 with that value. The future-proofing has zero behavior delta today, so the ~1-hour cost (validator field type + env parser + 10 test fixtures) is dead-weight rework for the live demo. Sprint 2 picks it up alongside the multi-aud retest if WSO2 fixes CIBA's `resource=` handling in a future IS release.

**Setup doc impact:** the `docs/wso2-is-setup.md` rewrite includes an MCP Server registration step, but documents that this is for metadata/discovery — NOT for binding tokens via CIBA. Validators expect `aud=<agent-OAuth-Client-ID>`.

---

## F-16 — Sprint 2 hooks (NOT Sprint 1 work, just flagged)

- **T3 rate limiting** (consent fatigue prevention): `(specialist_oauth_client_id, login_hint) → 1 request / 5s` window. Sprint 2 task.
- **T6 X-Request-ID enforcement** policy: `common/a2a/server.py` decision — refuse missing header OR auto-generate with WARN log. **Default: auto-generate with WARN.** Lock in Sprint 2 if a stricter posture is needed.

---

## Summary: what Stage 6 implementing agents should do

1. **Read** `sprint-1.md` (overview), then this file (`sprint-1-fixes.md`) — this overrides where it conflicts.
2. **Reference** `api-contracts.md`, `module-layout.md`, `sequence-diagrams.md` for the bulk of the contracts; consult this fixes doc whenever a spec seems ambiguous.
3. **Pin** dataclass-vs-Pydantic per F-09 in every new module.
4. **Use** `common/auth/binding_messages.py` for any user-facing consent strings (F-05).
5. **Implement** the two-phase A2A protocol (F-01) — this is the single most consequential design lock.

Stage 6 unblocks once these fixes are reflected in the source docs OR implementing agents are told explicitly to read this addendum first. Recommend the latter for speed; defer source-doc edits to Sprint 1 polish.

---

## F-18 — Role-based scope denial path on IS 7.2 (Sprint 2A.3 finding)

**Surfaced:** 2026-05-08 manual test as `employee_user`, "please approve LV-004" and "issue MBP-14-001 to alice".

### Outcome

WSO2 IS 7.2 does **NOT** reject at CIBA initiation when the requested scope exceeds the user's role grant. It follows **Path C — silent scope downgrade + server-side enforcement**:

1. `/oauth2/ciba` accepts the request, returns `auth_req_id` + `auth_url` (HTTP 200).
2. User completes consent at IS — IS issues a token, but the token's `scope` claim contains only the scopes the user's role permits. Excess scopes are silently dropped.
3. The agent's MCP call to the resource server fails with HTTP 401 + `error_id=ERR-MCP-003` because the server's `required_scopes` guard catches the missing scope.

This is hybrid: IS issues a deliberately reduced token, the resource server enforces.

### Implication for the architecture

- **Defense-in-depth holds.** Server-side scope guard is authoritative; even if the agent had no error-handling, the user could not invoke a write tool.
- **No consent-denial widget** is shown for permission failures — the consent flow completes "successfully" before the denial surfaces.
- **The agent dispatcher must parse the upstream MCP response body** to lift `error_id` from the 401's `detail` dict, otherwise it surfaces the generic `ERR-MCP-005` and the orchestrator's `_friendly_error` falls to the default copy.
- **Audit narrative:** the IS access log shows the user's role-bound token issuance; the resource server's log shows the scope-denial. Two complementary records — exactly the audit story the security pitch wants.

### What changed in code (Sprint 2A.3 partial)

- `hr_agent/ciba/orchestrator.py` and `it_agent/ciba/orchestrator.py`: the `httpx.HTTPStatusError` handler now extracts `detail.error_id` from the response body. Falls back to `ERR-MCP-005` only when the body is unparseable.
- `orchestrator/chat/routes.py` `_ERROR_COPY`: rewrote `ERR-MCP-003` from "resource server is misconfigured. Contact admin." to **"You don't have permission to perform this action. Your administrator can grant the required role."** Added `ERR-MCP-005` mapping.
- `idp_capability_test/c11_role_denial.py`: capability probe (uncommitted; user can inspect) for empirical regression detection if IS upgrades change the denial path.

### Acceptance evidence (manual test, both HR and IT)

```
hr_server / it_server : ERR-MCP-003 (token validation failed)
hr_agent / it_agent   : upstream_id=ERR-MCP-003
orchestrator          : chat_fan_out error_id=ERR-MCP-003
SPA                   : "You don't have permission to perform this action..."
```

N30 / N31 mock-IS fixtures (Sprint 2B.3) should mirror Path C — issue a token without the required scope, then assert MCP returns 401 / `ERR-MCP-003`. Do not mock initiation-time `invalid_scope`; that path doesn't exist on IS 7.2.

---

## F-19 — WSO2 IS 7.2 does not register CIBA grants as user-session participants (Sprint 3 spike finding)

**Surfaced:** 2026-05-09 C12 capability spike (`docs/spikes/c12-bcl-spike-setup.md`) as `hr_admin_user`, end-to-end CIBA flow followed by RP-initiated `/oidc/logout`.

### Outcome

WSO2 IS 7.2 does **NOT** fan out OIDC Back-Channel Logout to agent applications when their CIBA-issued OBO tokens are revoked, because IS does not consider CIBA grants to enroll the agent app into the user's IS-side session. The `backchannel_logout_uri` field on Agent Applications is therefore inert for our architecture.

### Evidence (triple confirmation)

1. **IS Console → User Management → `hr_admin_user` → Active Sessions** shows **"No active sessions"** four minutes after a successful CIBA flow against HR-AGENT (rid `52e0c885-…`, auth_req_id `d27e3ccb-…`). MCP call returned 200; OBO token was minted and used. Yet IS lists no sessions for the user.

2. **BCL listener captured zero POSTs** during a subsequent `https://13.60.190.47:9443/oidc/logout?post_logout_redirect_uri=…` despite both HR-AGENT and IT-AGENT having `backchannel_logout_uri = http://localhost:8123/bcl` registered in their Protocol tab. Listener + reverse-SSH tunnel verified healthy via direct curl from the AWS VM.

3. **IS audit log** (`<is>/repository/logs/audit.log`) shows the Logout event registered successfully, but with the SP fields blank:

   ```
   [2026-05-09 05:47:13] INFO {AUDIT_LOG} - Action : Logout | Target : null | Data : {
     LoggedOutUser       : 1**********************************7   (hr_admin_user, redacted)
     ServiceProviderName : "null"
     RelyingParty        : "null"
     AuthenticatedIdPs   : ""
     RequestType         : "oidc"
   } | Result : Success
   ```

   `ServiceProviderName: null` + `RelyingParty: null` mean IS has no list of RPs to fan BCL out to. CIBA grants do not populate the session→SP mapping that BCL fan-out is keyed on.

### Why this happens

CIBA is a **decoupled authentication flow** — the user's authentication device and the requesting client are different. IS treats this as "a token was minted via consent" but does **not establish an OIDC session for the agent app** the way an authorization_code login does. Curity's practitioner literature (cited in `docs/spikes/sprint-3-logout-design-brainstorm.md` §9.4) describes this exact category of behaviour:

> "Service applications (bots, agents) that issue tokens without maintaining UI-level sessions face a fundamental challenge: they have no 'session' to terminate. Back-channel logout becomes moot if the app doesn't track session state."

### Implication for the architecture

- **Drop agent-side BCL receivers from Sprint 3 scope.** IS will never call them. The `backchannel_logout_uri` field on Agent Applications is left blank by default; future-us can ignore it.
- **The orchestrator IS the gateway** — empirically, not just by design intent. ScaleKit / Okta / AWS-AgentCore patterns converge here. Sprint 3 implements the **orchestrator-driven cache-bust** pattern (Option A in the brainstorm doc):
  - SPA Sign Out → orchestrator `/api/logout`
  - orchestrator: clear `orch_sid`, revoke token-A via `/oauth2/revoke`, fan-out cache-bust by `jti` to specialists, set `pending_ciba.cancel_event` for in-flight CIBA flows, return 302 → IS `/oidc/logout` for IS-side session cleanup.
  - specialists: drop `_CachedToken` for the user, add `jti` to in-memory denylist, MCP server enforces via denylist + 60 s introspection cache.
- **The "CAEP migration" framing still holds** — the orchestrator's internal cache-bust RPC is morally equivalent to a CAEP `session-revoked` event over Shared Signals Framework. We document CAEP as the production roadmap target.

### What changed in code

Nothing yet — this is a spike outcome that *unscopes* a chunk of Sprint 3B (agent-side BCL receivers). The implementation work for Sprint 3 is unchanged from the brainstorm doc's Option A.

### Files added by this spike (commit-worthy)

- `docs/spikes/c12-bcl-spike-setup.md` — operator runbook for the spike rig.
- `docs/spikes/sprint-3-logout-design-brainstorm.md` §-1 — verdict-locked summary.
- `idp_capability_test/c12_logout_capability.py` — auto-probe + manual recipe.
- `tools/bcl_listener.py` — Python BCL receiver used during the spike.
- `tools/_bcl_log/bcl_received.log` — captured zero entries during the run; **the empty file is itself the verdict.**
- `scripts/spike-bcl-prep-mac.sh` / `spike-bcl-up.sh` / `spike-bcl-down.sh` — operator helpers.
- `docker-compose.yml` `bcl-listener` service under profile `spike-bcl`.

### Reproducer

Full setup walkthrough in `docs/spikes/c12-bcl-spike-setup.md`. Short version:

```bash
./scripts/spike-bcl-prep-mac.sh         # one-time
./scripts/spike-bcl-up.sh                # start listener + reverse-SSH tunnel
# register http://localhost:8123/bcl on each agent app in IS Console
# sign in, mint a CIBA token, then drive /oidc/logout
cat tools/_bcl_log/bcl_received.log      # empty → IS did not fire BCL
./scripts/spike-bcl-down.sh
```

---

## F-20 — WSO2 IS 7.2 accepts `/oauth2/revoke` with `auth_req_id` but treats it as a no-op (Sprint 3 spike finding)

**Surfaced:** 2026-05-09 C14 capability probe (`idp_capability_test/c14_authreqid_revoke.py`) as `employee_user` (sub `2048ad8c-16a6-4ec1-bb63-b38300118f28`).

### Question probed

Does IS support revoking a pending CIBA `auth_req_id` before the user has approved at the consent screen? The mitigation for Q-LOGOUT-4 ("ghost approval") in Sprint 3 D3.1 depended on a positive answer.

### Outcome — F-20 FAIL (soft)

WSO2 IS 7.2 returns **HTTP 200** to `/oauth2/revoke` with `token=<auth_req_id>` (and the same for `token_type_hint=auth_req_id` and `=ciba_request`) — but the `auth_req_id` remains valid. Polling `/oauth2/token grant_type=urn:openid:params:grant-type:ciba` after the revoke still returns `authorization_pending`. IS treats the revoke as a silent no-op for `auth_req_id`s.

The non-standard `/oauth2/ciba/revoke` endpoint returns 404.

### Evidence

```
auth_req_id : 8dc598ca-a57e-41cb-baf8-…
expires_in  : 300
interval    : 2

/oauth2/revoke shape A (no token_type_hint)        : HTTP 200 (empty)
/oauth2/revoke shape B (token_type_hint=auth_req_id): HTTP 200 (empty)
/oauth2/revoke shape C (token_type_hint=ciba_request): HTTP 200 (empty)
/oauth2/ciba/revoke (non-standard)                 : HTTP 404 (empty)

Poll /oauth2/token (after 3 s back-off):
  HTTP 400
  { "error": "authorization_pending", "error_description": "Authorization pending" }
```

### Implication for Sprint 3

- **Q-LOGOUT-4 ghost-approval caveat is REAL.** The orchestrator's local `cancel_event.set()` is the only cancellation primitive available; IS continues to honour the `auth_req_id` until natural expiry (`expires_in=300`).
- **3B.2 will NOT wire `auth_req_id` revoke.** Per Stage 5 §6 OQ-2 fallback, document the caveat in `docs/demo-runbook.md` and accept it.
- The race is bounded: a "ghost" approval after logout can mint a token-B with no consumer (poll loop is cancelled). If the token-B somehow reaches an MCP server (e.g. captured via observability), the denylist check rejects it — provided the fan-out reached the MCP server. With F-21 (below) also FAIL, the MCP-server backstop becomes the only line of defense, and only if the fan-out succeeded.

### Caveat about the verdict

`HTTP 200` from `/oauth2/revoke` is *technically spec-compliant* per RFC 7009 §2.2 ("The authorization server responds with HTTP status code 200 if the token has been revoked successfully or if the client submitted an invalid token"). IS is following the letter of the spec. The user-visible outcome — that the auth_req_id continues to work — is the failure mode, not the response code.

### Reproducer

```bash
cd idp_capability_test
EMPLOYEE_USER_SUB=<uuid> python3 c14_authreqid_revoke.py
```

---

## F-21 — WSO2 IS 7.2 does not propagate parent-token revocation to CIBA-issued OBO tokens (Sprint 3 spike finding)

**Surfaced:** 2026-05-09 C13 capability probe (`idp_capability_test/c13_introspection_capability.py`) as `employee_user` with admin-credential introspection.

### Question probed

Sprint 3 Stage 4 BLOCK-A: does revoking token-A (Pattern C parent) make CIBA-issued token-B introspect as `active=false`? The §5 error-matrix backstop story for half-fan-out (R-LOGOUT-7) and orchestrator-crash (EX-4) depended on a positive answer.

### Outcome — F-21 FAIL

WSO2 IS 7.2 treats CIBA-issued OBO tokens as **independent grants** with no parent linkage. Revoking token-A via `/oauth2/revoke` succeeds (token-A introspects as `active=false` within ≤5 s) but **token-B remains `active=true`** — even though token-B was minted via a CIBA flow whose actor was the orchestrator-mcp-client (token-A's grant chain).

This is structurally consistent with F-19: IS treats CIBA grants as session-independent. F-19 closed the BCL question; F-21 closes the introspection question. Both point at the same architectural property of WSO2 IS 7.2.

### Evidence

```
Token-A jti : c4d320ac-08fe-4682-ba59-d05bf4a5cbcf
              client_id=Ry9Wx_Q7w2FSi27miUpYr3O0xR4a (orchestrator-mcp-client)
              scope="email openid profile"
Token-B jti : df428ab7-f7b7-487c-81b3-f214e55ad88c
              client_id=aNpk7lwTHkw94iwC6f5VCO5ffNIa (HR-AGENT-OAuth-Client)
              scope="hr_self_rest openid"
              token JWT carries act.sub=51de717a-... (hr_agent — CIBA actor)

C13.1 introspect token-A : active=true   (sanity)
C13.2 introspect token-B : active=true   (sanity)
C13.3 /oauth2/revoke token-A: HTTP 200   (accepted)
C13.4 introspect token-A : active=false  ✓ (revoke propagates locally)
C13.5 introspect token-B : active=true   ✗ — F-21 FAIL
```

### Implication for Sprint 3

Per Stage 5 L-2 lock: **ship with SECURITY-DEGRADED labels** (do not escalate to a Sprint 4 hardening sprint).

Concrete required follow-ups:

1. **Tech-arch §5 error matrix** — relabel rows 1, 2, 3 as **SECURITY-DEGRADED**:
   - "`/oauth2/revoke` returns 5xx" — backstop claim removed; surviving claim is "token expires naturally within TTL".
   - "Single fan-out leg returns 5xx" — introspection backstop replaced by "captured token survives until TTL on missed leg; denylist on remaining 3 legs partially defends".
   - "All fan-out legs fail" — already SECURITY-DEGRADED.
   - "Orchestrator process restarts mid-cascade" — same.
2. **Demo runbook** — add operator-action note: on `logout_fanout_partial WARN`, restart affected receivers within demo window OR accept the 1 h replay window for the captured token.
3. **R-LOGOUT-7b** — the test now asserts the SECURITY_DEGRADED label is emitted on all-legs-failure; no behavioural change to fan-out.
4. **Demo narrative** — sharpen the story: *"Denylist is the security boundary; introspection is a staleness backstop for tokens IS itself revokes (token-A, admin-terminate). For CIBA OBO tokens, the orchestrator-driven cascade is the only revocation primitive — which is exactly why the gateway pattern matters."*

### Side observation worth noting

The IS introspection response for token-B does **not include the `act` claim** (it strips it from the JWT payload), even though the JWT itself carries `act.sub=51de717a-…`. If any future Sprint 3 code wants to use introspection to check `act.sub`, it must decode the JWT directly rather than rely on the introspection payload.

### Reproducer

```bash
# 1. Sign in as employee_user, trigger an HR query, approve consent.
# 2. Capture token-A and token-B (one-shot debug prints in
#    orchestrator/auth/routes.py + hr_agent/ciba/orchestrator.py;
#    see git history of this commit for the exact one-liners).
# 3. Run:
cd idp_capability_test
TOKEN_A=<paste> TOKEN_B=<paste> python3 c13_introspection_capability.py
# Expect: F-21 FAIL — token-B still active.
```

### F-19 + F-20 + F-21 unified statement

WSO2 IS 7.2 treats CIBA-issued OBO grants as **fully independent of any parent identity context** for revocation purposes. Specifically: (a) CIBA grants are absent from the user-session table, so RP-initiated logout / admin-terminate do not fire BCL to the agent apps (F-19); (b) the `auth_req_id` is non-revocable while pending (F-20); (c) the issued OBO access token is not killed by revoking the actor's parent grant (F-21).

This is the precise empirical reason the **gateway pattern is required, not preferred**, for our architecture: the orchestrator's internal cache-bust fan-out is the only path that propagates revocation to CIBA-issued tokens. Stage 1 §1 demo arc already uses this framing; F-21 simply tightens the technical receipts.

---

## F-19 addendum / F-20 / F-21 — source-code confirmation (2026-05-09)

After the empirical probes (C12 / C13 / C14) and the IS audit-log analysis, the assistant performed a source-code dive against `/Users/ramith/code/identity-inbound-auth-oauth/components` (head of `main`). Full analysis: [`docs/spikes/sprint-3-is-source-analysis.md`](../spikes/sprint-3-is-source-analysis.md). Bottom-line updates to F-19/20/21:

### F-19 addendum — was a probe artifact

`OIDCLogoutServlet.java:272–291` has two distinct branches in `/oidc/logout` handling:

```java
if (StringUtils.isNotBlank(clientId) || StringUtils.isNotBlank(idTokenHint)) {
    redirectURL = processLogoutRequest(request, response);   // ← BCL fan-out path
    ...
} else {
    OIDCSessionDataCacheEntry cacheEntry = new OIDCSessionDataCacheEntry();
    setStateParameterInCache(request, cacheEntry);
    addSessionDataToCache(opBrowserState, cacheEntry);       // ← no clientId, no BCL
}
sendToFrameworkForLogout(request, response, logoutContext);
```

`DefaultLogoutTokenBuilder.buildLogoutToken()` iterates EVERY participant in `sessionParticipants` — no CIBA exclusion. The C12 spike URL was `https://13.60.190.47:9443/oidc/logout?post_logout_redirect_uri=…` — no `id_token_hint`, no `client_id`. That hit the second branch, which never fires BCL by design. The empty BCL listener log was expected behaviour for a malformed logout request, NOT evidence that CIBA grants aren't session participants.

**Corrected statement:** WSO2 IS DOES walk session participants and fire BCL when `/oidc/logout` receives a valid `id_token_hint` (or `client_id`). CIBA flows enrol the agent app as a session participant on the same `sessionContextId` as the user's Pattern C login (audit-log evidence corroborates).

**Sprint 3 implication:** Option C (hybrid with agent-side BCL receivers) is viable for D3.2 admin-terminate. The locked Sprint 3 Q3 design already uses `id_token_hint` on the `/oidc/logout` redirect, so the BCL fan-out path will fire as intended.

### F-20 confirmation — design-level, not bug

`OAuth2Service.revoke*()` and `AuthReqStatus` enum show:
- `/oauth2/revoke` only handles `access_token` and `refresh_token`. No code path for `auth_req_id`.
- `AuthReqStatus` enum: `REQUESTED, AUTHENTICATED, TOKEN_ISSUED, EXPIRED, FAILED, CONSENT_DENIED`. No `REVOKED`.

The 200-on-`auth_req_id` is RFC 7009-compliant ("server may return 200 for unknown tokens") — but at the DB level, the auth_req_id is unchanged. CIBA does not currently support pre-approval revocation. Q-LOGOUT-4 ghost-approval caveat is permanent.

### F-21 confirmation — architectural, not bug

`DefaultOAuth2RevocationProcessor.revokeAccessToken()` is a single-row UPDATE:
```java
…getAccessTokenDAOImpl(accessTokenDO.getConsumerKey())
    .revokeAccessTokens(new String[]{accessTokenDO.getAccessToken()});
```

The `IDN_OAUTH2_ACCESS_TOKEN` schema (per `AccessTokenDAOImpl` INSERT path) has no `actor_token`, no `parent_grant_id`, no `request_id` column. The `act` JWT claim is JWT-body only — no DB back-reference. There is no graph traversal, no cascade, no event hook that any extension uses to propagate revocation.

**Sprint 3 implication:** Token-A revoke will never invalidate OBO tokens. **Denylist on receivers is the only revocation primitive for OBO tokens.** Gateway pattern is *required*, not preferred — the demo narrative now has source-code receipts.
