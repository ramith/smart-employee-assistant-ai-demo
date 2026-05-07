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
