# Sprint 4 — Technical architecture

**Stage:** 6 (technical architecture — locked after Stage 5 API design)
**Date:** 2026-05-10
**Branch (entry):** `sprint-3-build` @ `b497616` (Sprint 3 close + hardening pass)
**Branch (work):** `sprint-4-build` (cut at Stage 9 kickoff).
**Read order:** [`sprint-4.md`](sprint-4.md) (binding) → [`sprint-4-stage-4-ux-design.md`](sprint-4-stage-4-ux-design.md) → [`sprint-4-stage-5-api-design.md`](sprint-4-stage-5-api-design.md) → this doc → Stage 7 (slice plan).
**Supersedes:** nothing. First Stage 6 doc for Sprint 4. Pulls forward Stage 5 §8 OQ-1 through OQ-6 and resolves each.

> **Amendment 2026-05-10 (post-document, user challenge):** Decision B simplified — **drop the `groups` claim and `roles` field.** `/auth/exchange` returns ONLY `scopes: list[str]`. SPA derives `isHrAdmin = scopes.includes("hr_approve_rest")` for navigation gating (canonical probe — `hr_approve_rest` is HR-Admin-exclusive per `docs/scope-policy.md` §2). Server-side authority unchanged.
>
> **The following sections of this document are SUPERSEDED by the amendment above and are obsolete (treat as struck-through):**
> - §2.1 — `JWTClaims.groups` field addition (REMOVED — claim model adds only `username` + `email`).
> - §8.2 — `_derive_roles_and_scopes` helper (replaced by `_derive_scopes` returning `list[str]` derived from `token_a.scope.split()`).
> - §8.3 — fail-closed behaviour for missing `groups` (OBSOLETE — no `groups` claim is consulted).
> - §8.4 — operator dependency on `groups` claim mapping (OBSOLETE).
> - §9.1 pre-flight check #10 — `groups` claim probe (REMOVED from `scripts/check-is-config.sh`).
>
> **Stage 6.5 reconciliation amendment 2026-05-10:** Stage 8 reviewers found that the production `hr_server`/`it_server` runtime mounts `mcp/tools.py` (Sprint 1 canned scaffold), NOT `service/` + `rest_api/` modules. The reconciliation pass at [`sprint-4-stage-6.5-reconciliation.md`](sprint-4-stage-6.5-reconciliation.md) decisions D1–D11 supersede portions of §3 (module layout). Specifically:
> - The new MCP tools (D1–D5 cubicle, E1 get_my_assets) are added to the EXISTING `hr_server/mcp/tools.py` and `it_server/mcp/tools.py`. NO new `tools_v2.py` or parallel module.
> - The legacy canned-data dicts (`_CANNED_LEAVE_BALANCES`, `_CANNED_ASSIGNED_ASSETS`) are REPLACED with delegations into `hr_service`/`it_service` (preserving the existing route paths + scope guards).
> - `it_server/auth/jwt_validator.py` is BUILT FROM SCRATCH in S4.0 (current file is a Sprint 0 stub raising `NotImplementedError`). Mirror the working `hr_server/auth/jwt_validator.py:JWTValidator` class.
> - `hr_server/main.py` and `it_server/main.py` gain `app.include_router(rest_api_router)` for the new REST surfaces.
> - `hr_agent/mcp/client.py` and `it_agent/mcp/client.py` gain new methods slice-by-slice (existing 3 HR client methods + 5 new = 8 total HR client methods).

This document is the technical specification for Sprint 4 implementation. Stage 5 locked the API contract; this doc translates it into concrete file-and-symbol, data-model, and operator-script decisions. Implementation must not start until Stage 7 (slice plan) has carved the work into UC-walkthrough-ending slices and Stage 8 (team review) has signed off.

---

## §1. Goal and scope summary

Sprint 4 pivots the demo from infrastructure/IAM hardening (Sprints 1–3) to business-domain richness. Stage 5 specifies **11 new REST endpoints** (5 orchestrator-proxy, 5 HR-Server, 1 IT-Server), **6 new MCP tool definitions** (5 HR-agent, 1 IT-agent), **1 SSE event field extension** (`action_text` on `ciba_url`), **1 A2A payload field extension** (`action_text` on `ConsentRequiredPayload`), and **1 `/auth/exchange` response field extension** (`roles` + `scopes`). This Stage 6 doc maps that contract onto:

- Concrete file paths and public-symbol surfaces for each new and modified module.
- Token-validator extensions (audience handling, claim plumbing for `username` / `email` / `groups`).
- Cubicle and IT-asset data models (in-memory, single-uvicorn-worker, no persistence).
- Multi-turn cubicle chat plumbing (LLM primary, keyword-fallback identical).
- Pre-flight operator script that audits IS configuration before the Stage 11 manual test.

The `BLOCK-I` invariant from Sprint 3 (single uvicorn worker per service; in-process state) is reasserted. **No Redis ever** (per memory `feedback_compose_secret_alignment.md`). All cubicle data, asset data, leave records, and lookup tables live in process-local Python dicts/lists, re-seeded on container restart.

### §1.1 Stage 5 open question resolutions (table)

| OQ | Stage 5 question | Stage 6 resolution | Section |
|---|---|---|---|
| **OQ-1** | How does HR Server know whose leaves to return for `GET /api/me/leaves`? | (b) **Derive `sub` from token-A claim only.** Orchestrator does NOT forward `user_sub` as query parameter (would leak IS UUID). HR Server B1 reads `claims.sub`, ignores any query string. | §2.4, §3.B |
| **OQ-2** | How does the leave-requests reporting response surface `employee_email`? | (a) **Persist `user_email` on `store.leave_requests` records at `apply_leave` time.** Email comes from the `email` claim. No retrospective fix; reporting reads the record verbatim. | §4.4 |
| **OQ-3** | HR Server token-A audience for new REST reporting paths (token-A's `aud == orchestrator-mcp-client` while MCP tools require `aud == hr_agent`). | **(b) Different validators on different paths.** REST reporting paths use the existing `hr_server/auth/jwt_validator.py:JWTValidator` (already accepts a list of audiences via `pyjwt`). MCP paths keep the strict `hr_server/auth/validators.py:HRServerTokenValidator`. Add `ORCH_MCP_CLIENT_ID` to the REST validator's audience list at startup. | §2.3 |
| **OQ-4** | Cubicle seed for `hr_admin_user` self-service (UC-12 EX-1). | Pre-seed C-002 on floor 1 to `employee_user`. `hr_admin_user` is left unassigned by design — UC-12 EX-1 demos cleanly via the admin path. | §4.6 |
| **OQ-5** | A2A `ConsentRequiredPayload` strict-mode compatibility with optional `action_text`. | Adding `action_text: str \| None = None` to a `model_config = ConfigDict(strict=True)` Pydantic v2 model is valid; `None` is accepted in strict mode for explicitly-typed `Optional` fields. | §7.2 |
| **OQ-6** | Reports handler file location and router registration. | New router lives at `orchestrator/reports/routes.py`. Wired in `orchestrator/main.py` next to the chat router via `app.include_router(reports_router)` with `tags=["reports"]`. | §3.A |

**The most consequential resolution is OQ-3.** The HR Server already has two distinct token validators (REST path and MCP path), and the REST validator already accepts a list of audiences. Cleanest path: add the orchestrator's MCP Client ID to the REST validator's audience list and leave the MCP validator alone. The Stage 5 prompt's option (a) (unify both validators) would have been a bigger refactor than implied. Option (c) (RFC 8693 token exchange per page load) is rejected as too heavyweight. **Option (b) wins on minimum diff.**

### §1.2 Stage 5 contract amendments recommended

Two minor amendments. Neither changes the wire shape; both clarify intent.

1. Stage 5 §B1 says `GET /api/me/leaves` "ignores any `user_sub` query parameter". Recommend explicitly stating the orchestrator MUST NOT include `user_sub` on the proxied URL. Enforced by the proxy primitive in §3.A.
2. Stage 5 §A6 implies the orchestrator fetches leave-request metadata from HR Server before invoking HR Agent for `action_text` construction. Stage 5 §7 lists per-id detail endpoints as out-of-scope, so this is awkward. **Recommend**: HR Agent owns the metadata resolution server-side at CIBA-binding-message-construction time. The orchestrator passes `{request_id}` and HR Agent reads it from the existing service layer. Saves one RTT and avoids a new endpoint. Locked in §6.4.

---

## §2. Token validation extensions

This section answers Stage 5 §4 ("Identity claim plumbing") with concrete code-change specifications.

### §2.1 `JWTClaims` model additions

`common/auth/models.py:JWTClaims` (existing dataclass at lines 81–114) gains three optional fields. The dataclass is `frozen=True, slots=True` — both flags are preserved. Field defaults are required (existing call sites construct positionally), so the new fields come AFTER existing fields.

```python
# common/auth/models.py — JWTClaims, after existing fields (around line 94)
username: str | None = None       # IS user attribute claim (operator A3)
email: str | None = None          # OIDC email claim
groups: tuple[str, ...] = ()      # IS role group membership; tuple for frozen safety
```

`groups` defaults to `()` (immutable tuple) rather than `[]` because `JWTClaims` is `frozen=True` — a mutable default would break the freezing semantic. Callers that need a list build it explicitly: `list(claims.groups)`.

### §2.2 Where the claim extractor is updated

`common/auth/jwt_validator.py:validate()` (the shared validator used by the MCP path; called from `hr_server/auth/validators.py:258`) constructs `JWTClaims` from the decoded payload. The construction call is updated to pull the three new claims:

```python
# common/auth/jwt_validator.py — inside validate(), at JWTClaims construction
claims = JWTClaims(
    sub=payload["sub"], iss=payload["iss"], aud=payload["aud"],
    exp=int(payload["exp"]), iat=int(payload["iat"]),
    jti=payload.get("jti"), act=payload.get("act"),
    scope=payload.get("scope"), aut=payload.get("aut"),
    # Sprint 4:
    username=payload.get("username"),
    email=payload.get("email"),
    groups=tuple(payload.get("groups") or []),
)
```

The `or []` defends against IS returning `None` for `groups`. The REST-path validator (`hr_server/auth/jwt_validator.py:JWTValidator.validate_token`) returns the raw `payload` dict directly — it does NOT construct `JWTClaims`. Sprint 4 keeps this asymmetry. Code that needs `username` / `email` from a REST handler reads them off the dict (`payload.get("username")`). REST handlers in `hr_server/rest_api/server.py` already build a `_AuthContext` (line 56) — Sprint 4 extends it with `username` and `email` attributes pulled from the payload.

### §2.3 OQ-3 — HR Server REST validator audience handling (LOCKED)

**Resolution: option (b) — different validators on different paths.**

The HR Server has two validator implementations:

| File | Used by | Audience strategy |
|---|---|---|
| [`hr_server/auth/jwt_validator.py:88`](../../hr_server/auth/jwt_validator.py) (REST) | `hr_server/rest_api/server.py` REST handlers | Already accepts a list via `pyjwt`'s `audience=` parameter. Construction at `hr_server/rest_api/server.py:42-51`. |
| [`hr_server/auth/validators.py:214`](../../hr_server/auth/validators.py) (MCP) | `hr_server/mcp/tools.py` per-tool calls | Single `expected_aud` (`hr_server/config.py:98`). Strictly enforces hr_agent's OAuth Client ID. |

**The change is one block in `hr_server/rest_api/server.py:42-51`** plus a new env var:

```python
import os

_audiences = [config.CLIENT_ID]
if config.SPA_CLIENT_ID:
    _audiences.append(config.SPA_CLIENT_ID)

# Sprint 4 OQ-3 — accept the orchestrator's MCP client ID for new reporting paths.
# Token-A is forwarded by the orchestrator with aud=ORCH_MCP_CLIENT_ID. The MCP
# tool path uses a DIFFERENT validator and is unaffected.
_orch_mcp_aud = os.getenv("HR_SERVER_ACCEPT_ORCH_MCP_AUD")
if _orch_mcp_aud:
    _audiences.append(_orch_mcp_aud)
```

The same change applies to `it_server/rest_api/server.py` for the IT-side reporting path (C1). Two new env vars are introduced:

| Env var | Service | Source |
|---|---|---|
| `HR_SERVER_ACCEPT_ORCH_MCP_AUD` | hr_server | `ORCH_MCP_CLIENT_ID` from orchestrator's `.env` |
| `IT_SERVER_ACCEPT_ORCH_MCP_AUD` | it_server | Same |

Wired in `docker-compose.yml` as a passthrough from the host environment. Operator runbook step: confirm both vars are populated before bringing the fleet up.

**Why this is the right boundary.** The MCP-tool security boundary is unchanged: token-C with `aud=hr_agent_client_id` is the only thing that gets to call `assign_cubicle`. The new REST surface is explicit (orchestrator → `hr_server`) and isolated to the reporting path. If a forged token has `aud=ORCH_MCP_CLIENT_ID` and the right scope, the worst it can do is read aggregate leave data — which is what `hr_read_rest` already authorises. No privilege escalation across the boundary.

**Test that protects this contract.** `tests/hr_server/rest_api/test_audience_segregation.py` (NEW) asserts:
1. Token-A with `aud=ORCH_MCP_CLIENT_ID` and `hr_read_rest` → 200 on `/api/reports/leave-requests`.
2. Same token presented to an MCP tool endpoint → 401 (audience mismatch on the strict validator).
3. Token-C with `aud=hr_agent_client_id` presented to `/api/reports/leave-requests` → 401 (REST validator does NOT include hr_agent audience).

### §2.4 Username + email claim extraction in MCP tools

Per Stage 5 §4.3:

```python
# Inside any Sprint 4 MCP tool that needs username
claims = await deps.validator.validate_token(jwt_token, required_scopes=...)
if not claims.username:
    raise HTTPException(
        status_code=401,
        detail={"error_id": "ERR-AUTH-007", "message": "username claim required but absent on token"},
    )
# Proceed with cubicle / asset lookup using claims.username
```

**Fail-closed rule (locked):** if a tool requires `username` and it's absent, return 401 — do NOT fall back to `claims.sub`. Silently substituting `sub` would produce silently-wrong behaviour (cubicle lookup miss returning `assigned: false` when the user is actually assigned).

**Tool-by-tool requirement matrix:**

| Tool | Requires `username`? | Required by Stage 6? |
|---|---|---|
| D1 `get_cubicle_summary` | No | No |
| D2 `get_vacant_cubicles_on_floor` | No | No |
| D3 `assign_cubicle` | No (target passed as args) | No (admin's own claim not needed) |
| D4 `get_my_cubicle` | **Yes** | Username-keyed lookup |
| D5 `lookup_employee` | No | Searches store; not caller identity |
| E1 `get_my_assets` | **Yes** | Username-keyed lookup (post-migration) |
| B1 `GET /api/me/leaves` | No (uses `sub`) | OQ-1: `sub` only |

**Pushback to Stage 5 (minor):** Stage 5 §4.1 lists D3 and E1 as needing `username`. D3 doesn't — the admin passes `employee_username` as a tool argument, not derived from the admin's own claim. Stage 5's table is slightly over-tight; this matrix replaces it for implementation purposes.

### §2.5 New error codes (confirmation only)

Stage 5 §6 introduced four new business-error codes. Stage 6 confirms wire-level surfacing without changes. `ERR-AUTH-007` is repurposed to also cover "required claim absent" (in addition to scope-precheck semantic); error message disambiguates at runtime.

---

## §3. Module-layout deltas

Per [`module-layout.md`](module-layout.md): one module = one responsibility; routes split from service logic; service split from store; tests mirror under `tests/`.

### §3.A New module: `orchestrator/reports/`

#### `orchestrator/reports/__init__.py` (NEW, empty)

Standard package marker.

#### `orchestrator/reports/proxy.py` (NEW)

Orchestrator-proxy primitive: cookie session → Bearer token-A → backend → pass-through. Reusable for all five read endpoints. Two public functions.

```python
async def proxy_get(
    *, request: Request, backend_base_url: str, backend_path: str,
    required_scope: str | None, session_store: SessionStore,
    config: OrchestratorConfig,
    query_params: Mapping[str, str] | None = None, timeout_s: float = 5.0,
) -> dict
```

**Behaviour:**
1. Validate cookie session (orch_sid). 401 if absent / invalid / `Session.terminating == True` (Sprint 3 BLOCK-G).
2. If `required_scope` set, pre-flight on `token_a.scope`. 403 if missing.
3. Forward GET to backend with `Authorization: Bearer <token-A>` and `X-Request-ID`.
4. Return parsed JSON body verbatim (backend's envelope is the wire).
5. Map upstream errors to `ERR-API-PROXY-001` / 502.

```python
async def proxy_post(
    *, request: Request, backend_base_url: str, backend_path: str,
    required_scope: str | None, body: dict,
    session_store: SessionStore, config: OrchestratorConfig,
    timeout_s: float = 10.0,
) -> dict
```

Same shape for write endpoints. Used internally if the orchestrator needs server-to-server mutation (not used for approve/reject — those go through A2A).

**Private helpers:**
- `_load_session_or_401(request, session_store, config) -> Session` — common cookie validation; rejects terminating sessions.
- `_preflight_scope_check(session, required_scope) -> None` — 403 on missing scope.
- `_build_backend_headers(session, request) -> dict[str, str]` — `Authorization` + `X-Request-ID`.

**Test counterpart:** `tests/orchestrator/reports/test_proxy.py` (NEW). Coverage: 401 cookie missing; 401 terminating session; 403 scope missing; 200 happy-path; 502 on backend 5xx + httpx error; header propagation.

**Why a separate file:** the primitive is reused by 5+ handlers. Inlining in `routes.py` would obscure handler logic. Separation matches the `agent_registry/cards.py` + `agent_registry/discovery.py` precedent.

#### `orchestrator/reports/routes.py` (NEW)

FastAPI router for A1, A3, A4, A5, A6, A7. Constructed via a factory matching `build_chat_router` (`orchestrator/chat/routes.py:55`) and `build_auth_router` (`orchestrator/auth/routes.py:284`).

```python
@dataclass
class ReportsRouterDeps:
    config: OrchestratorConfig
    session_store: SessionStore
    sse_channel: SseChannel  # for A6/A7 to push ciba_url events on approve/reject

def build_reports_router(deps: ReportsRouterDeps) -> APIRouter
```

The router exposes:
- `GET  /api/me/leaves` → `MyLeavesResponse`
- `GET  /api/reports/leave-requests` (`?status=...`) → `LeaveRequestsReportResponse`
- `GET  /api/reports/cubicle-assignments` → `CubicleAssignmentsResponse`
- `GET  /api/reports/device-assignments` → `DeviceAssignmentsResponse`
- `POST /api/reports/leave-requests/{request_id}/approve` → `ApproveRejectAck`
- `POST /api/reports/leave-requests/{request_id}/reject` → `ApproveRejectAck`

**Private surface:**
- `_invoke_hr_agent_approve(deps, request_id, reviewer_name, sse_channel) -> None` — calls HR Agent A2A directly without inserting a chat message; pushes `ciba_url` SSE event on `consent_required`.
- `_construct_action_text_for_approve(request_id, store_metadata) -> str` — builds `"Approve {username}'s leave from {start_date}"`. Uses HR-Agent-resolved metadata at CIBA-construction time (per §1.2 amendment).

**Test counterpart:** `tests/orchestrator/reports/test_routes.py` (NEW). Coverage parallels Stage 5 §9 testing hooks. Includes regression for OQ-3 (synthetic token with wrong audience must 401 before forward).

**Why this lives at `orchestrator/reports/`:** mirrors `orchestrator/auth/` and `orchestrator/chat/`. The `tags=["reports"]` annotation surfaces in OpenAPI and `tools/grep-trace.sh` can isolate report-route logs.

#### Wiring in `orchestrator/main.py`

```python
from orchestrator.reports.routes import build_reports_router, ReportsRouterDeps

reports_deps = ReportsRouterDeps(config=config, session_store=session_store, sse_channel=sse_channel)
app.include_router(build_reports_router(reports_deps))
```

Single line in `main.py`. No middleware change. Same SSE channel reused for `ciba_url` / `ciba_state_change` events from approve/reject.

### §3.B HR Server changes

#### `hr_server/rest_api/server.py` (MODIFIED)

Adds five new route handlers (B1, B2, B3, B4, B5). Each one mirrors the existing pattern (`get_holidays` at line 139; `get_leave_balance` at line 159):

1. `_authenticate(request)` — validates Bearer token, populates `_AuthContext`.
2. `_require_scope(ctx, *scopes)` — 403 if scope missing.
3. Call into `service.hr_service`.
4. Return `JSONResponse` with the body.

Handler signatures (each is `async def name(request: Request) -> JSONResponse`):

- `get_my_leaves_rest` — scope `hr_self_rest`; calls `hr_service.get_my_leave_requests(sub, first_name, last_name)`; envelope `{data, count}`.
- `get_leave_requests_report` — scope `hr_read_rest`; calls `hr_service.get_leaves_for_dashboard(status=...)`.
- `get_cubicle_assignments_report` — scope `hr_read_rest`; calls new `hr_service.get_all_cubicle_assignments()`.
- `approve_leave_request_rest` — scope `hr_approve_rest`; **Bearer token-C**; calls `hr_service.approve_leave_request(...)`.
- `reject_leave_request_rest` — scope `hr_approve_rest`; Bearer token-C.

The new routes are appended to the `Starlette` app's `routes=` list at the bottom of `server.py`.

**Test counterpart:** `tests/hr_server/rest_api/test_server.py` (extend) + `tests/hr_server/rest_api/test_reports.py` (NEW).

#### `hr_server/service/hr_service.py` (MODIFIED)

New public functions (signatures only — full bodies in implementation):

```python
async def get_my_cubicle(username: str) -> Dict
async def get_all_cubicle_assignments() -> List[Dict]
async def assign_cubicle(
    cubicle_id: str, employee_username: str,
    employee_email: str, employee_sub: str,
) -> Dict
async def get_cubicle_summary() -> Dict
async def get_vacant_cubicles_on_floor(floor: int) -> Dict
async def lookup_employee(username_or_email: str) -> Dict
```

`hr_service.py` is the single business-logic surface shared by REST and MCP. New cubicle-write logic (`assign_cubicle`) goes here once, called from both D3 (MCP) and any future direct REST mutator.

**Test counterpart:** `tests/hr_server/service/test_hr_service.py` (extend) + `tests/hr_server/service/test_cubicle_service.py` (NEW) covering R-CUBICLE-1..4 from `sprint-4.md` §4.

#### `hr_server/service/store.py` (MODIFIED)

Adds the cubicle data model + a parallel `_SEED_USERS` lookup. Full spec in §4.

#### `hr_server/mcp/tools.py` (MODIFIED)

Adds five new tool handlers (D1–D5) following the existing pattern (`get_leave_balance` at line 244; entry/dispatch at lines 286, 349, 415).

Each new handler: validates token via `deps.validator.validate_token(jwt, required_scopes=...)`, reads `claims.username` if needed (D4 only), calls into `service.hr_service`, returns the Stage 5 §3 Pydantic response model.

**Test counterpart:** `tests/hr_server/mcp/test_tools.py` (extend).

### §3.C IT Server changes

| File | Operation | Notes |
|---|---|---|
| `it_server/rest_api/server.py` | MODIFIED | Adds C1 (`GET /api/reports/device-assignments`); same audience env var pattern as §2.3. |
| `it_server/service/it_service.py` | MODIFIED | New public functions: `async def get_my_assets(username: str) -> List[Dict]`; `async def get_all_asset_assignments() -> List[Dict]`. |
| `it_server/service/store.py` | REWRITTEN | One-shot data migration: drop `employee_id`; rekey `_SEED_ASSETS` by `username`. Add `_SEED_USERS`. See §5. |
| `it_server/mcp/tools.py` | MODIFIED | Drop `employee_id` arg from `get_my_assets` (lines 130-176); change scope to `it_assets_self_rest`. **Planned breaking change** per Stage 5 §E1. |

### §3.D Orchestrator auth changes

`orchestrator/auth/routes.py` (MODIFIED): `ExchangeResponse` (line 172) gains `roles: list[str]` and `scopes: list[str]` per Stage 5 Decision B / §A8. Derivation in §8.

### §3.E Common changes

| File | Operation |
|---|---|
| `common/auth/models.py` | MODIFIED — `JWTClaims` adds `username`, `email`, `groups` (§2.1). |
| `common/auth/jwt_validator.py` | MODIFIED — populate new claim fields (§2.2). |
| `common/a2a/models.py` | MODIFIED — `ConsentRequiredPayload` adds `action_text: str \| None = None` (§7.2). |
| `orchestrator/events/sse.py` | MODIFIED — `CibaUrlEvent` adds `action_text` (§7.1). |

### §3.F SPA changes

`client/app.js` (MODIFIED):
1. `onCibaUrlEvent` (line ~694) extends `actionText` lookup per Stage 4 §6: `const actionText = event.action_text || scopeToAction(scope);`.
2. `completeLogin` (line ~378) reads `roles` and `scopes` from `ExchangeResponse`; stashes on a module-level `userContext`.
3. New `renderReportsPage()` for the `/reports` path (Stage 4 §4).
4. New `renderMyLeavesPanel()` invoked on home page first paint and on every `chat_message` SSE settle.
5. `SCOPE_ACTION_MAP` (line ~122) and `SCOPE_GERUND_MAP` (line ~133) gain three entries from Stage 4 §6.
6. New `renderReportsButton()` conditionally renders the "Reports" header link when `roles.includes("HR Admin")`.

`client/index.html` (MODIFIED): new `<div id="reports-page" hidden>` sibling to `<main id="chat-main">`; tab structure with `role="tablist"`; new `<section id="my-leaves-panel">` below the chat composer; new "Reports" header button.

`client/styles.css` (MODIFIED): new rules per Stage 4 §8 — `.data-table`, `.skeleton-bar`, `.status-pill`, `.pill--*`, `.btn-approve`, `.btn-reject`, `.consent-widget[data-write-action]`. Reuses tokens from `client/styles.css:13`.

### §3.G Module-layout summary table

| Path | Operation | Test counterpart |
|---|---|---|
| `orchestrator/reports/__init__.py` | NEW | — |
| `orchestrator/reports/proxy.py` | NEW | `tests/orchestrator/reports/test_proxy.py` (NEW) |
| `orchestrator/reports/routes.py` | NEW | `tests/orchestrator/reports/test_routes.py` (NEW) |
| `orchestrator/auth/routes.py` | MOD | `tests/orchestrator/auth/test_routes.py` (extend) |
| `orchestrator/main.py` | MOD | `tests/orchestrator/test_main.py` (extend) |
| `orchestrator/events/sse.py` | MOD | `tests/orchestrator/events/test_sse.py` (extend) |
| `orchestrator/chat/routes.py` | MOD — propagate `action_text` | `tests/orchestrator/chat/test_routes.py` (extend) |
| `orchestrator/chat/keyword_fallback.py` | MOD — cubicle rules | `tests/orchestrator/chat/test_keyword_fallback.py` (extend) |
| `hr_server/rest_api/server.py` | MOD — 5 routes + audience env var | `tests/hr_server/rest_api/test_reports.py` (NEW) |
| `hr_server/service/hr_service.py` | MOD — 6 new fns | `tests/hr_server/service/test_cubicle_service.py` (NEW) |
| `hr_server/service/store.py` | MOD — cubicles + users lookup | `tests/hr_server/service/test_store.py` (extend) |
| `hr_server/mcp/tools.py` | MOD — D1–D5 | `tests/hr_server/mcp/test_tools.py` (extend) |
| `it_server/rest_api/server.py` | MOD — C1 + audience env var | `tests/it_server/rest_api/test_reports.py` (NEW) |
| `it_server/service/it_service.py` | MOD — 2 new fns | `tests/it_server/service/test_it_service.py` (extend) |
| `it_server/service/store.py` | REWRITTEN — username keys | `tests/it_server/service/test_store.py` (extend) |
| `it_server/mcp/tools.py` | MOD — `get_my_assets` arg drop; new scope | `tests/it_server/mcp/test_tools.py` (extend) |
| `common/auth/models.py` | MOD — `JWTClaims` fields | `tests/common/auth/test_models.py` (extend) |
| `common/auth/jwt_validator.py` | MOD — populate new fields | `tests/common/auth/test_jwt_validator.py` (extend) |
| `common/a2a/models.py` | MOD — `action_text` on consent | `tests/common/a2a/test_models.py` (extend) |
| `client/app.js` | MOD | n/a (manual + UC tests) |
| `client/index.html` | MOD | n/a |
| `client/styles.css` | MOD | n/a |
| `scripts/check-is-config.sh` | NEW | n/a (operator script) |

---

## §4. Cubicle data model

### §4.1 Seed shape and counts

Per `sprint-4.md` §2 item 1 and Stage 5 §A4 / §D3, Sprint 4 seeds **100 cubicles across 4 floors**, 25 per floor.

```python
# hr_server/service/store.py — Sprint 4 addition
_FLOOR_RANGES: list[tuple[int, int, int]] = [
    (1, 1, 25), (2, 26, 50), (3, 51, 75), (4, 76, 100),
]

def _build_seed_cubicles() -> list[dict]:
    out: list[dict] = []
    for floor, start, end in _FLOOR_RANGES:
        for n in range(start, end + 1):
            out.append({
                "cubicle_id": f"C-{n:03d}",
                "floor": floor,
                "occupied": False,
                "assigned_to_sub": None,
                "assigned_to_username": None,
                "assigned_to_email": None,
                "assigned_at": None,
            })
    return out

_SEED_CUBICLES: list[dict] = _build_seed_cubicles()
```

The pre-seed of `C-002` to `employee_user` (per OQ-4) is applied as a post-build mutation:

```python
def _apply_demo_seed_assignments() -> None:
    """Pre-seed C-002 → employee_user so UC-12 self-service has data on Day 1."""
    sub = os.getenv("HR_SERVER_EMPLOYEE_USER_SUB")
    if not sub:
        logger.warning("[CUBICLE SEED] HR_SERVER_EMPLOYEE_USER_SUB unset — C-002 left unassigned")
        return
    _SEED_CUBICLES[1].update({
        "occupied": True,
        "assigned_to_sub": sub,
        "assigned_to_username": "employee_user",
        "assigned_to_email": "employee_user@example.com",
        "assigned_at": "2026-04-01",
    })
```

Called from `reset_data()` after the seed is rebuilt. If the env var is missing, the assignment is skipped — the cubicle becomes a normal vacant cubicle and the demo's UC-12 EX-1 path ("you have no cubicle") fires for both users. Acceptable graceful degradation.

### §4.2 Cubicle record field names

Per Stage 5 §D3 / §A4 and `sprint-4.md` §7:

| Field | Type | Returned in REST? | Returned in MCP? | Notes |
|---|---|---|---|---|
| `cubicle_id` | `str` | ✅ | ✅ | e.g. `"C-027"` |
| `floor` | `int` (1–4) | ✅ | ✅ | |
| `occupied` | `bool` | Internal only | Internal only | Computed from `assigned_to_*` presence |
| `assigned_to_sub` | `str \| None` | **NEVER** | **NEVER** | Internal join key (per `sprint-4.md` §7) |
| `assigned_to_username` | `str \| None` | ✅ as `employee_username` | ✅ as `assigned_to_username` | |
| `assigned_to_email` | `str \| None` | ✅ as `employee_email` | ✅ as `assigned_to_email` | |
| `assigned_at` | `str \| None` (ISO date) | ✅ | ✅ | YYYY-MM-DD only — no time |

The `assigned_to_username → employee_username` mapping happens in the `get_all_cubicle_assignments()` projection in `hr_service.py`. Stage 5 §A4's response uses `employee_username` (matches UC-16 column header naming); the internal store uses `assigned_to_username` (matches the verb "assigned to"). Single point of mapping.

### §4.3 `assign_cubicle` idempotency rule

Locked per Stage 5 §D3:

| Input | Existing state | Behaviour | Return body |
|---|---|---|---|
| `(C-027, jane.doe)` | unoccupied | Mutate; set `assigned_at = today` | `{success: true, cubicle_id, floor, assigned_to_username, assigned_to_email, assigned_at}` |
| `(C-027, jane.doe)` | already `jane.doe` | No-op | Same body, **with original `assigned_at`** |
| `(C-027, jane.doe)` | `bob.smith` holds it | No-op | `{error: "cubicle_already_occupied", current_holder_username, current_holder_email}` |
| `(C-999, jane.doe)` | not found | No-op | `{error: "cubicle_not_found", cubicle_id: "C-999"}` |

All four return HTTP 200 (per Stage 5 §6: business errors live in 200 bodies; only auth errors return non-200).

**Race protection (UC-11 EX-1).** Sprint 4 is single-uvicorn-worker per BLOCK-I; therefore `assign_cubicle` is atomic at the Python-coroutine level. The TOCTOU race ("Cubicle C-027 was just assigned to bob.smith between turns 2 and 3") is the multi-admin case where two HR Admins race CIBA. The server-side `if occupied: return error` check is the only defense (per `sprint-4.md` §5 item 6 — no optimistic concurrency for Sprint 4).

### §4.4 `email` on leave-request store records (OQ-2 resolution)

Stage 5 OQ-2 resolves to add `user_email` to `store.leave_requests` records at `apply_leave` time. The signature of `hr_service.apply_leave` (existing line 72) gains a keyword-only `email` parameter:

```python
async def apply_leave(
    sub: str, first_name: str, last_name: str,
    leave_type: str, start_date: str, end_date: str, reason: str,
    *,
    email: str | None = None,    # Sprint 4 — required for reports projection
) -> Dict
```

Inside the function, the store record adds `"user_email": email`. Sprint 4 callers (REST and MCP) MUST pass `email` (sourced from `claims.email` or `_AuthContext`). The reports projection treats missing `user_email` as `""` — never crashes. Existing tests pass `email=None` and continue to work.

### §4.5 `_SEED_USERS` lookup table (HR Server)

Per `sprint-4.md` §6 and Stage 5 §D5:

```python
# hr_server/service/store.py — Sprint 4 addition
_SEED_USERS: dict[str, dict] = {
    "employee_user":  {"username": "employee_user",  "email": "employee_user@example.com",  "sub": None},
    "hr_admin_user":  {"username": "hr_admin_user",  "email": "hr_admin_user@example.com",  "sub": None},
    "jane.doe":       {"username": "jane.doe",       "email": "jane.doe@example.com",       "sub": None},
    "bob.smith":      {"username": "bob.smith",      "email": "bob.smith@example.com",      "sub": None},
}

def lookup_user_by_username_or_email(query: str) -> dict | None:
    """Case-insensitive. If query contains '@', search by email; else by username."""
    q = query.lower()
    if "@" in q:
        for u in _SEED_USERS.values():
            if u["email"].lower() == q:
                return dict(u)
        return None
    return dict(_SEED_USERS[q]) if q in _SEED_USERS else None
```

`sub` values populated at container startup from `HR_SERVER_USER_SUBS_JSON` (a JSON map of username → sub). If unset, `sub=None`; demo accounts can still be looked up but `assign_cubicle` writes `None` to `assigned_to_sub`. Acceptable degradation; the cubicle record's `assigned_to_sub` is internal and never displayed.

### §4.6 OQ-4 — Seed adjustment confirmation

`_SEED_CUBICLES[1]` (i.e. `C-002` on floor 1) is pre-assigned to `employee_user`. Only pre-seeded assignment. All 99 other cubicles start `occupied=False`. Stage 11 demo flow:
- HR Admin asks for vacant summary → `{floor_1: {total: 25, vacant: 24, occupied: 1}, ...}`.
- Employee asks "where is my cubicle?" → D4 returns C-002 on floor 1.
- HR Admin assigns a new cubicle to a third user (jane.doe) → vacant count on the chosen floor decrements.

---

## §5. IT seed data migration

### §5.1 New `_SEED_ASSETS` shape

Per `sprint-4.md` §7 ("rewrites the legacy IT seed data to drop the numeric employee_id field"):

```python
# it_server/service/store.py — Sprint 4 rewrite
_SEED_ASSETS: list[dict] = [
    {"asset_id": "AST-12345", "username": "jane.doe",      "type": "laptop",  "model": "MBP 14 M3",      "status": "outstanding"},
    {"asset_id": "AST-12346", "username": "jane.doe",      "type": "phone",   "model": "iPhone 15",      "status": "returned"},
    {"asset_id": "AST-22001", "username": "bob.smith",     "type": "laptop",  "model": "Dell XPS 13",    "status": "outstanding"},
    {"asset_id": "AST-22002", "username": "bob.smith",     "type": "monitor", "model": "Dell U2723QE",   "status": "outstanding"},
    {"asset_id": "AST-30115", "username": "employee_user", "type": "laptop",  "model": "MBP 16 M3",      "status": "outstanding"},
    {"asset_id": "AST-30116", "username": "employee_user", "type": "headset", "model": "AirPods Pro 2",  "status": "returned"},
]
```

`employee_id` is dropped completely. `username` replaces it. Numeric IDs `1042`, `2017`, `3110` are not retained — they were placeholders with no link to real IS user records.

### §5.2 Parallel `_SEED_USERS` dict

Mirror of `hr_server.store._SEED_USERS`, minus `sub` (IT-Server doesn't initiate CIBA, so it doesn't need sub):

```python
_SEED_USERS: dict[str, dict] = {
    "employee_user":  {"username": "employee_user",  "email": "employee_user@example.com"},
    "hr_admin_user":  {"username": "hr_admin_user",  "email": "hr_admin_user@example.com"},
    "jane.doe":       {"username": "jane.doe",       "email": "jane.doe@example.com"},
    "bob.smith":      {"username": "bob.smith",      "email": "bob.smith@example.com"},
}
```

### §5.3 New service helpers

```python
# it_server/service/store.py
def get_assets_for_username(username: str) -> list[dict]:
    return [a for a in assets if a["username"] == username]

def lookup_user_by_username(username: str) -> dict | None:
    return dict(_SEED_USERS[username.lower()]) if username.lower() in _SEED_USERS else None
```

The Sprint 1 function `get_assets_for_employee` (line 65) is **deleted, not aliased**. Aliasing creates a parallel API that quietly diverges; a hard rename forces all call sites to update. Same applies to the existing MCP tool's `employee_id` parameter (`it_server/mcp/tools.py:130-176`). This is the migration tax for Sprint 4.

### §5.4 Tests that will break

Sprint 4's data migration breaks any test that hard-codes the old numeric employee IDs. Grep on `tests/` finds one test file:

| Test | Usage | Sprint 4 update |
|---|---|---|
| `tests/it_server/mcp/test_tools.py` | Asserts `employee_id="1042"` and uses legacy `GetMyAssetsArgs.employee_id` parameter | Rewrite assertions to use `username="jane.doe"` etc.; drop `employee_id` from args |

Plus any orchestrator-side test that sets up a synthetic IT response with the old shape. A sweep `grep -rn "employee_id" tests/` is part of slice 2's diff review.

**Stage 7 implementation note:** the data migration is a single commit. Running tests immediately after will fail in `tests/it_server/mcp/test_tools.py`. The slice plan must group the migration commit with the test-update commit so each green slice is testable.

---

## §6. Multi-turn cubicle chat plumbing

### §6.1 Decision: LLM-driven primary, keyword-fallback identical

`sprint-4.md` §10 risk R7 worries that the LLM "doesn't naturally produce four messages in sequence" and recommends "the existing keyword-fallback path... with explicit four-step intent labels." Stage 6 confirms: the LLM is the primary router AND the keyword fallback covers the same four intents — both produce identical user-visible outcomes. Rationale:

- LLM produces fluent multi-turn conversation natively (Gemini handles "show me vacant cubicles" → "floor 2" → "C-027 to jane.doe" without scripting).
- Keyword fallback is the deterministic safety net for `LLM_FALLBACK_MODE=keyword` (default in the demo runbook). It must hit the same tools.
- One implementation per intent on the agent side — both LLM and keyword router produce the same `ToolCall(agent_id, tool_id, args)` shape.

### §6.2 Keyword-fallback rule additions

Add to `orchestrator/chat/keyword_fallback.py:DEFAULT_RULES` (line 97) — four new `KeywordRule` entries:

| Rule keywords | tool_id | args extraction |
|---|---|---|
| `"vacant cubicle"`, `"available cubicle"`, `"cubicle summary"`, `"show vacant"` | `hr.get_cubicle_summary` | none |
| `"floor 1"`, `"floor 2"`, `"floor 3"`, `"floor 4"` | `hr.get_vacant_cubicles_on_floor` | `{floor: int}` from regex digit |
| `"assign c-"`, `"assign cubicle"` | `hr.assign_cubicle` | `{cubicle_id, employee_username}` from `C-NNN` regex + "to <name>" |
| `"my cubicle"`, `"where is my cubicle"`, `"where do i sit"` | `hr.get_my_cubicle` | none |

The keyword router is rule-ordered (existing comment at line 99: "Specific verbs first; dedup-by-agent in route() means the first match per agent wins"). So `"assign c-027"` matches `assign_cubicle` before `"cubicle"` matches anything else.

Args extraction (cubicle_id from regex, floor digit from regex, username from "to <token>") happens in a small helper `_extract_cubicle_args(message: str, tool_id: str) -> dict` added to `keyword_fallback.py`. Implementation guideline: regex-only, no NLP. Test counterpart: `tests/orchestrator/chat/test_keyword_fallback.py` (extend) with one test per regex path.

**Limitation honesty:** the fallback handles canonical phrasings ("Assign C-027 to jane.doe"). Off-script ("can you put jane in cubicle twenty-seven") falls through to no-match and the orchestrator surfaces "I don't understand". The LLM is the path for natural phrasings; the fallback is the demo's script-conformance safety net. Acceptable for Sprint 4 demo scope.

### §6.3 LLM prompt augmentation

The orchestrator-side LLM (`orchestrator/chat/llm.py` per `module-layout.md`) needs the new tool definitions in its tool registry. Each new MCP tool gets an `AgentCard` entry that the LLM sees when listing tools via `discover_agents()`. The HR Agent's `AgentCard` gains five new tool descriptors — one per (D1, D2, D3, D4, D5) — each carrying `tool_id`, `scope_required`, plain-English `description`, and `args_schema` for parameterised tools.

### §6.4 Routing payload shape (orchestrator → HR Agent A2A)

For turn 3 of UC-11, the orchestrator's `MessageSendParams` to HR Agent is:

```python
{"tool": "hr.assign_cubicle",
 "args": {"cubicle_id": "C-027", "employee_username": "jane.doe"}}
```

`employee_email` is NOT supplied by the orchestrator. **HR Agent calls `hr.lookup_employee(username_or_email="jane.doe")` itself** before initiating CIBA, to resolve `email` and `sub`. If `found=false`, HR Agent returns `ErrorPayload(error_id="ERR-EMPLOYEE-001", reason="employee not found")` to the orchestrator without initiating CIBA. If found, HR Agent constructs `action_text = "Assign cubicle C-027 to jane.doe"` and includes it in the `ConsentRequiredPayload`. **The orchestrator does NOT call `lookup_employee` directly** — it's an HR-Agent-internal helper. Concentrates the "must resolve employee before CIBA" logic where it belongs.

**Pushback to Stage 5 (minor):** Stage 5 §D5 lists `lookup_employee` as a separately-callable MCP tool with `hr_read_rest`. Since the HR Agent calls it via OBO with token-C bearing `hr_read_rest` (the agent has both `hr_read_rest` for lookup and `hr_assets_write_rest` for assign in its scope set), this is consistent. No contract change required.

### §6.5 Multi-turn state management

The orchestrator does NOT track "we're on turn 2 of a cubicle flow". The chat is stateless at the routing layer; each user message is independently routed. Multi-turn coherence comes from:
- The user's natural conversation flow (each message implies the next intent).
- The LLM's context window (sees prior turns and routes accordingly).
- The keyword fallback's intent rules (specific to each turn's typical phrasing).

Intentionally simple. Adding a multi-turn state machine would be over-engineering for a demo. Trade-off: if the user types "show me vacant cubicles" twice in a row, the agent answers twice (no de-dup). Acceptable.

---

## §7. SSE event schema extension

### §7.1 `ciba_url` payload — `action_text`

Per Stage 5 §F1, `CibaUrlEvent` (`orchestrator/events/sse.py:70`) gains:

```python
# Sprint 4 addition (after binding_message)
action_text: str | None = None
```

Default `None`; existing producers and consumers unaffected. SPA's `onCibaUrlEvent` falls back to `scopeToAction(scope)` when `action_text` is absent (Stage 4 §6).

**Producer side:** the orchestrator's chat router (or reports router for A6/A7) reads `consent.action_text` from the A2A `ConsentRequiredPayload` and includes it when emitting `CibaUrlEvent`. Single line change at the existing `sse_channel.push(CibaUrlEvent(...))` call in `orchestrator/chat/routes.py` — adds `action_text=consent.action_text` to the kwargs.

### §7.2 `ConsentRequiredPayload` — `action_text` (A2A)

Per Stage 5 §G1 and OQ-5:

```python
# common/a2a/models.py — ConsentRequiredPayload (line 51), after existing fields
action_text: str | None = None
```

**Strict-mode compatibility (OQ-5 LOCKED):** `model_config = ConfigDict(strict=True)` on Pydantic v2 means types must match exactly (no string-to-int coercion). Adding `action_text: str | None = None` is valid because `None` is an explicit member of the `str | None` union — strict mode doesn't reject it. Existing tests in `tests/common/a2a/test_models.py` and any A2A round-trip tests are re-run after the field addition. No regression expected; the A2A wire serialiser uses `mode="json", exclude_none=True` per `common/a2a/server.py`, so omitted `action_text` is preserved on the wire.

### §7.3 SPA-side rendering

Per Stage 4 §6 and `client/app.js:694`:

```javascript
// onCibaUrlEvent (line ~721): action_text takes precedence over scope-derived fallback.
const actionText = event.action_text || scopeToAction(scope);
```

The amber-tint logic (Stage 4 §6) is independent of `action_text`. It triggers from scope membership:

```javascript
const isWriteScope = ["hr_assets_write_rest", "hr_approve_rest", "it_assets_write_rest"]
  .some(s => (scope || "").includes(s));
$("consent-widget").toggleAttribute("data-write-action", isWriteScope);
```

Both pieces (action_text precedence + amber tint) are independent additions; neither blocks the other.

---

## §8. `/auth/exchange` response extension

### §8.1 `ExchangeResponse` field additions

Per Stage 5 Decision B / §A8:

```python
# orchestrator/auth/routes.py — ExchangeResponse (line 172), after existing fields
roles: list[str]    # e.g. ["HR Admin"] or ["Employee"]
scopes: list[str]   # e.g. ["openid", "hr_self_rest", "hr_read_rest", ...]
```

### §8.2 Derivation rule (locked)

In the `/auth/exchange` handler (`orchestrator/auth/routes.py:426-536`), after `result = await deps.pattern_c.exchange(...)`:

```python
def _derive_roles_and_scopes(result: PatternCExchangeResult) -> tuple[list[str], list[str]]:
    """Sprint 4 — derive roles + scopes for SPA navigation gating."""
    scope_str: str = result.token_a.scope or ""
    scopes = scope_str.split() if scope_str else []

    def _decode_unverified(token: str | None) -> dict[str, Any]:
        """Decode without sig verify; signatures already validated upstream."""
        if not token:
            return {}
        try:
            return pyjwt.decode(token, options={"verify_signature": False})
        except pyjwt.PyJWTError as e:
            logger.warning("auth_exchange_decode_for_roles_failed | %s", e)
            return {}

    id_payload = _decode_unverified(result.token_a.id_token)
    access_payload = _decode_unverified(result.token_a.access_token)
    groups_raw = id_payload.get("groups") or access_payload.get("groups") or []
    if isinstance(groups_raw, list):
        roles = [str(g) for g in groups_raw if g]
    elif isinstance(groups_raw, str):
        # WSO2 IS sometimes serialises a single group as a bare string.
        roles = [groups_raw] if groups_raw else []
    else:
        roles = []
    return roles, scopes
```

The derivation is a private module-level helper for testability.

### §8.3 Fail-closed behaviour for missing `groups`

If `groups` is absent from BOTH id_token and access_token, `roles` returns `[]`. The SPA reads `roles`:
- `roles.includes("HR Admin")` evaluates `false`.
- Reports header button and "View Reports" panel link are hidden.
- The user is treated as Employee for navigation purposes.

This is the safe-fail direction. Server-side `hr_read_rest` enforcement is unchanged — even if the SPA somehow surfaces the Reports nav (via dev tools), the orchestrator pre-flight check on `token_a.scope` is the actual security boundary. `roles = []` is a UX hint that fails closed (less navigation visible), not a security degradation.

### §8.4 Operator dependency — `groups` claim mapping

Per `sprint-4.md` §3 ask A3 (`username` and `email` claims), Sprint 4 also depends on `groups` being mapped. WSO2 IS 7.x emits `groups` by default for users in roles, but the access token claim set may need explicit mapping in IS Console (Service Provider → Claim Configuration → Requested Claims). The pre-flight script `scripts/check-is-config.sh` (§9) verifies before Stage 11.

If `groups` is absent and the operator cannot map it in time, the fallback derives admin role from scope membership:

```python
# Fallback if groups claim is missing
if not roles:
    if "hr_read_rest" in scopes or "hr_approve_rest" in scopes:
        roles = ["HR Admin"]
    else:
        roles = ["Employee"]
```

Documented as a workaround in `docs/wso2-is-setup.md` and not the primary path.

---

## §9. Pre-flight script: `scripts/check-is-config.sh`

A bash script that audits IS configuration before Stage 11 manual test. Exits 0 on full pass; 1 on any failure with per-check pass/fail output. Runs against the live IS at `13.60.190.47:9443` (or `IS_BASE_URL` env override).

### §9.1 Concrete checks (locked)

| # | Check | Pass criterion |
|---|---|---|
| 1 | API resource scope `hr_assets_write_rest` registered | GET `/api/server/v1/api-resources/scopes` returns it |
| 2 | API resource scope `it_assets_self_rest` registered | Same |
| 3 | HR Admin role grants 9 scopes (per `sprint-4.md` §6) | Role's permission list contains all 9 |
| 4 | Employee role grants 5 scopes | Role's permission list contains all 5 |
| 5 | `hr_agent` OAuth App subscribed to `hr_assets_write_rest` | App's API subscriptions include the scope |
| 6 | `it_agent` OAuth App subscribed to `it_assets_self_rest` | Same |
| 7 | `orchestrator-mcp-client` subscribed to `hr_read_rest`, `it_assets_read_rest` | Same |
| 8 | Sample access token (Pattern C against employee_user) carries `username` claim | Decoded payload has key |
| 9 | Same token carries `email` claim | Same |
| 10 | Same token carries `groups` claim | Same |
| 11 | Same token carries `username == "employee_user"` | Value match |

Each check logs `[PASS] <description>` or `[FAIL] <description> — <reason>` and a summary line:

```
========================================
 SUMMARY: 11 checks, 11 PASS, 0 FAIL
========================================
```

### §9.2 Sample-token issuance approach

Checks 8–11 require a sample token. Two options:

- **(a) `client_credentials` grant** — token has the app's identity, NOT the user's. Won't have `username` / `email` / `groups`. **Rejected.**
- **(b) Run `c1_pattern_c.py` spike with `employee_user` credentials** — issues a real user token. Decode and assert. **Locked.**

The script invokes the existing `idp_capability_test/c1_pattern_c.py` Python helper or a slim bash wrapper. Credentials come from env vars `IS_TEST_USER` / `IS_TEST_PASSWORD` (operator runbook).

### §9.3 Where this script runs

- **Day 1 of Stage 9 (kickoff):** operator runs the script. Any failure blocks implementation start until IS config is fixed.
- **Pre-Stage-11 (manual test prep):** re-run to confirm no IS config drift.
- **CI:** NOT run automatically (depends on a live IS).

Documented in `docs/wso2-is-setup.md` and referenced from the Stage 11 runbook.

---

## §10. Risks and mitigations specific to architecture

These are architecture-layer risks (distinct from `sprint-4.md` §10 which is sprint-level). Each has a concrete technical mitigation.

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| AR-1 | **Token-A audience mismatch on reporting paths.** OQ-3 resolution depends on the operator setting `HR_SERVER_ACCEPT_ORCH_MCP_AUD` and `IT_SERVER_ACCEPT_ORCH_MCP_AUD`. If either is unset, `GET /api/reports/...` returns 401. | Medium | High (blocks Reports page) | Pre-flight script asserts both env vars are set; docker-compose has them as required (errors at startup if absent); Stage 9 runbook lists the env-var step explicitly. |
| AR-2 | **`username` / `email` claim absent.** Operator A3 not done — `claims.username` is `None` on D4, D5, E1. UC-11 / UC-12 break with 401 / `ERR-AUTH-007`. | Medium | High (blocks UC-11, UC-12) | `scripts/check-is-config.sh` checks 8–11 verify claims are present; runs Day 1 of Stage 9. Failure blocks implementation. Fallback (§8.4) gives synthetic admin role from scope membership but does NOT substitute for `username` (no fallback for `username` — fail-closed is the right answer). |
| AR-3 | **Multi-turn cubicle routing fragility.** LLM picks wrong tool on Turn N (e.g. routes "show me vacant cubicles" to `get_my_cubicle` instead of `get_cubicle_summary`). | Medium | Medium (UC-11 demo wobble, recoverable with retry) | Keyword fallback covers canonical phrasings (§6.2) and is the runbook default. UC-11 BA spec lists exact admin prompts; demo rehearsal sticks to those. LLM prompt augmentation (§6.3) adds clear tool descriptors. |
| AR-4 | **IT seed migration breaks N tests.** `tests/it_server/mcp/test_tools.py` hard-codes `employee_id="1042"`. Migration commit fails CI. | High | Low (purely test churn) | §5.4 lists affected tests. Stage 7 slice plan groups the migration commit with the test-update commit so each green slice is testable. |
| AR-5 | **Cubicle data lost on container restart.** All cubicle assignments live in `hr_server.store.cubicles` — no persistence. A demo restart wipes them. | Low | Low (runbook restarts at known points) | Acknowledged in `sprint-4.md` §5 item 1 as out-of-scope. Runbook step: do not restart `hr_server` mid-demo unless re-running the cubicle assignment flow first. The pre-seeded `C-002 → employee_user` survives. |
| AR-6 | **`/auth/exchange` `roles` derivation produces empty list.** If `groups` is absent from BOTH tokens, SPA hides Reports nav for HR Admin. | Low (per A3 verification) | Medium (admin demo blocked) | §8.3 + §8.4: fail-closed default + scope-based fallback. Pre-flight script verifies the claim. |
| AR-7 | **A2A `action_text` not propagated through serial fan-out.** UC-12 has two consents (HR + IT); the second's `action_text` must be carried correctly. If a stale `action_text` from the first leaks into the second, the widget shows wrong copy. | Low | Medium (visible UX bug) | Each `ConsentRequiredPayload` carries its own `action_text`; SSE emission reads from the just-received payload, not from session-cached state. Test in `tests/orchestrator/chat/test_routes.py` covers serial fan-out with two distinct `action_text` values. |

---

## §11. Slice handoff to Stage 7

Stage 7 (slice plan) carves Sprint 4 implementation into UC-walkthrough-ending slices. Each slice ends with `tools/run-tests.sh --strict` green and at least one UC manually walkable.

Recommended **6-slice structure**:

1. **S4.1 — Identity claim plumbing + `/auth/exchange` extension.** All §2 token validator changes, `JWTClaims` field additions, `ExchangeResponse` extension. Verified by extended `tests/common/auth/test_jwt_validator.py` + `tests/orchestrator/auth/test_routes.py`. Manual gate: `scripts/check-is-config.sh` Day-1 run. UC: none (foundation slice).
2. **S4.2 — IT seed migration + parallel `users` dict.** All §5 changes. Verified by rewritten `tests/it_server/mcp/test_tools.py` + new `tests/it_server/service/test_store.py` extensions. UC: UC-12 IT-leg dry run.
3. **S4.3 — HR cubicle data model + REST + MCP read-side.** §4 store + §3.B `get_my_cubicle`, `get_cubicle_summary`, `get_vacant_cubicles_on_floor`, `lookup_employee`, `get_all_cubicle_assignments`. Verified by `tests/hr_server/service/test_cubicle_service.py` + `tests/hr_server/rest_api/test_reports.py`. UC: UC-11 turns 1+2, UC-12 HR-leg, UC-16 Cubicles tab.
4. **S4.4 — HR cubicle write-side (`assign_cubicle`) + UC-11 turns 3+4.** D3 MCP tool wired; HR Agent's CIBA path includes `action_text` construction; A2A + SSE `action_text` plumbed end-to-end. Verified by extended MCP tool tests + new `tests/orchestrator/chat/test_routes.py` cases for `action_text` propagation. UC: UC-11 full 4-turn flow.
5. **S4.5 — Reports proxy + Reports page + My Leaves panel.** All §3.A orchestrator/reports + Stage 4 §3 / §4 SPA work. Verified by `tests/orchestrator/reports/test_proxy.py` + `test_routes.py`. UC: UC-13, UC-14, UC-15 (read-only Pending Leaves), UC-16 (all three tabs).
6. **S4.6 — Approve/Reject + write-scope amber accent + final cohesion.** A6, A7 endpoints; HR Server B4, B5; SPA approve/reject buttons. Verified by extended chat/reports tests + manual UC-15 click-to-consent flow. UC: UC-15 full Approve and Reject.

Each slice ends with `tools/run-tests.sh --strict` green. Test count target: Sprint 3 baseline 898 + ~14 new R-tests ≈ **912 minimum**. Stage 10 (test plan) refines.

The pre-flight script `scripts/check-is-config.sh` is delivered as part of S4.1 and runs before each slice's manual gate. Any IS config drift mid-sprint is caught early.

---

## §12. References

### Stage docs (Sprint 4)

- [`docs/architecture/sprint-4.md`](sprint-4.md) — Stage 3 binding sprint plan. §6 scope lock, §7 identity model, §8 reporting data flow, §10 risks.
- [`docs/architecture/sprint-4-stage-1-product-review.md`](sprint-4-stage-1-product-review.md) — PM brief (historical).
- [`docs/architecture/sprint-4-stage-4-ux-design.md`](sprint-4-stage-4-ux-design.md) — UX. §3 home page, §4 reports page, §5 cubicle copy deck, §6 action_text, §7 approve/reject.
- [`docs/architecture/sprint-4-stage-5-api-design.md`](sprint-4-stage-5-api-design.md) — API. §2 decisions A/B, §3 endpoint specs, §4 identity claim plumbing, §5 envelopes, §6 error codes, §8 OQ-1..6.

### Reference docs

- [`docs/architecture/sprint-3-tech-arch.md`](sprint-3-tech-arch.md) — most recent tech-arch shape reference.
- [`docs/architecture/api-contracts.md`](api-contracts.md) — canonical API contract document. Sprint 4 additions ride alongside; no Stage 6 supersession.
- [`docs/architecture/module-layout.md`](module-layout.md) — file-and-symbol map.
- [`docs/scope-policy.md`](../scope-policy.md) — locked naming convention `<resource>_<action>_rest`.
- [`docs/wso2-is-setup.md`](../wso2-is-setup.md) — IS operator runbook (extended in Stage 9).

### Use cases

- [`docs/use-cases/UC-11-hr-admin-assigns-cubicle.md`](../use-cases/UC-11-hr-admin-assigns-cubicle.md)
- [`docs/use-cases/UC-12-employee-self-service-asset-discovery.md`](../use-cases/UC-12-employee-self-service-asset-discovery.md)
- [`docs/use-cases/UC-13-employee-applies-for-leave.md`](../use-cases/UC-13-employee-applies-for-leave.md)
- [`docs/use-cases/UC-14-employee-checks-own-leave-status.md`](../use-cases/UC-14-employee-checks-own-leave-status.md)
- [`docs/use-cases/UC-15-hr-admin-pending-leaves-table.md`](../use-cases/UC-15-hr-admin-pending-leaves-table.md)
- [`docs/use-cases/UC-16-hr-admin-assignment-reporting-tables.md`](../use-cases/UC-16-hr-admin-assignment-reporting-tables.md)

### Existing-code attachment points

- [`hr_server/auth/jwt_validator.py:88`](../../hr_server/auth/jwt_validator.py) — REST `validate_token`; accepts list audience (OQ-3 attaches here).
- [`hr_server/auth/validators.py:214`](../../hr_server/auth/validators.py) — MCP `HRServerTokenValidator.validate_token`; F-04 + Sprint 3 denylist.
- [`hr_server/rest_api/server.py:42-51`](../../hr_server/rest_api/server.py) — `_audiences` construction; OQ-3 env var added here.
- [`hr_server/rest_api/server.py:77-115`](../../hr_server/rest_api/server.py) — `_authenticate` + `_AuthContext`; new handlers reuse this pattern.
- [`hr_server/service/hr_service.py:54`](../../hr_server/service/hr_service.py) — `get_my_leave_requests` (B1 calls).
- [`hr_server/service/hr_service.py:72`](../../hr_server/service/hr_service.py) — `apply_leave` (Sprint 4 adds `email=` parameter; §4.4).
- [`hr_server/service/hr_service.py:148`](../../hr_server/service/hr_service.py) — `get_all_leave_requests`.
- [`hr_server/service/hr_service.py:196`](../../hr_server/service/hr_service.py) — `approve_leave_request` (B4).
- [`hr_server/service/hr_service.py:243`](../../hr_server/service/hr_service.py) — `reject_leave_request` (B5).
- [`hr_server/service/hr_service.py:274`](../../hr_server/service/hr_service.py) — `get_leaves_for_dashboard` (B2).
- [`hr_server/service/store.py:101`](../../hr_server/service/store.py) — `ensure_user`; pattern for `_SEED_USERS`.
- [`hr_server/mcp/tools.py:286`](../../hr_server/mcp/tools.py) — existing tool entry pattern; D1–D5 follow it.
- [`it_server/service/store.py:16-23`](../../it_server/service/store.py) — `_SEED_ASSETS` (rewritten — §5).
- [`it_server/service/store.py:65-70`](../../it_server/service/store.py) — `get_assets_for_employee` (deleted; replaced by `get_assets_for_username`).
- [`it_server/mcp/tools.py:130-176`](../../it_server/mcp/tools.py) — `get_my_assets` with legacy `employee_id` (Sprint 4 breaking change).
- [`orchestrator/auth/routes.py:172`](../../orchestrator/auth/routes.py) — `ExchangeResponse` (Sprint 4 adds `roles`+`scopes`).
- [`orchestrator/auth/routes.py:256-276`](../../orchestrator/auth/routes.py) — `_extract_display_name` + `_DISPLAY_NAME_CLAIMS` — pattern for `_derive_roles_and_scopes`.
- [`orchestrator/auth/routes.py:426-536`](../../orchestrator/auth/routes.py) — `/auth/exchange` handler; `_derive_roles_and_scopes` insertion after line 478.
- [`orchestrator/chat/keyword_fallback.py:97`](../../orchestrator/chat/keyword_fallback.py) — `DEFAULT_RULES`; cubicle rules added here.
- [`orchestrator/chat/routes.py:55-65`](../../orchestrator/chat/routes.py) — chat router imports.
- [`orchestrator/events/sse.py:70`](../../orchestrator/events/sse.py) — `CibaUrlEvent` (Sprint 4 adds `action_text`).
- [`common/auth/models.py:81-114`](../../common/auth/models.py) — `JWTClaims` dataclass.
- [`common/a2a/models.py:51-81`](../../common/a2a/models.py) — `ConsentRequiredPayload` (Sprint 4 adds `action_text`; OQ-5).
- [`client/app.js:122`](../../client/app.js) — `SCOPE_ACTION_MAP`; new entries.
- [`client/app.js:133`](../../client/app.js) — `SCOPE_GERUND_MAP`; new entries.
- [`client/app.js:378`](../../client/app.js) — `completeLogin`; reads `roles` + `scopes`.
- [`client/app.js:694`](../../client/app.js) — `onCibaUrlEvent`; `action_text` precedence.

### External specs

- OAuth 2.0 RFC 6749, OIDC Core 1.0 (id_token, claims, `groups`), WSO2 IS 7.x docs (claim configuration, role-scope assignment).

---

End of Sprint 4 — Technical architecture (Stage 6).
