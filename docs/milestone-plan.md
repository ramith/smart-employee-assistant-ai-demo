# Milestone Plan — v4 (CIBA architecture)

**Date:** 2026-05-07
**Supersedes:** `milestone-plan-v3-rfc8693-archived.md` (kept for historical reference; do not implement against it)
**Status:** Active. Sprint 1 starts after pre-Sprint-1 council review of this document.

This is the canonical milestone plan for the **Smart Employee Agent** POC after the M0 capability spike. The architecture is **per-agent CIBA-based on-behalf-of (OBO) delegation**, not RFC 8693 chained token-exchange (depth-2 nested `act` is not supported on WSO2 IS 7.2 — see `docs/spikes/wso2-is-capability-memo.md`).

---

## §0. Executive summary

The POC demonstrates **identity-first AI agent governance**: every agent action a user's request triggers is **explicitly user-consented in real time**, with each consent producing a narrowly-scoped, auditable OAuth OBO token bound to (user, specialist-agent, resource). Architecture: an SPA-fronted orchestrator coordinates specialist agents (HR, IT) over A2A JSON-RPC; each specialist initiates a CIBA flow when invoked, the user approves through a consent widget, and the specialist receives a token with `sub=user, act.sub=this-agent` to call its MCP backend.

**Demo headline:** "Every agent action requires user approval. The token *is* the authority."

**Demo shape:** serial fan-out (orchestrator routes through one specialist at a time); two specialists max in the canonical demo query.

**What this POC is not:** production-grade. Single-process, in-memory state, self-signed cert, correlation-IDs-in-logs as the audit story (signed envelopes are roadmap), no UAE Pass federation yet.

---

## §0.1 Current reality / addenda (post-Sprint-5 migration — authoritative)

> **This section is the single source of truth for changes made after the dated sprint
> docs below. Where a sprint sign-off/stage doc disagrees with this section, this section
> wins.** Updated 2026-05-19.

**1. LLM provider: Gemini → OpenAI via the WSO2 AMP AI Gateway.**
The orchestrator and the public info widget no longer use Google Gemini. They use
OpenAI through the AMP AI Gateway (OpenAI-compatible). Concretely:
- Module `orchestrator/llm/amp_client.py` (`OpenAILLMClient`) replaced the old
  `orchestrator/llm/gemini.py` (`GeminiLLMClient`). It wraps
  `langchain_openai.ChatOpenAI`.
- Env: `OPENAI_API_KEY`, `OPENAI_BASE_URL` (the AMP gateway), `OPENAI_API_HEADER`
  (default `api-key`), `OPENAI_MODEL` (currently `gpt-4.1`; code default `gpt-4o`).
  The old `GEMINI_API_KEY` / `GEMINI_MODEL` are gone.
- **Router uses OpenAI function-calling via `ChatOpenAI.bind_tools()`** — the tool
  catalogue is injected as function schemas and the model returns structured
  `tool_calls`. There is **no JSON-array parsing** of the router output; the old
  `parse_router_output` was removed.
- Observability: `amp-instrumentation` + `traceloop-sdk` export spans to the AMP
  console (`AMP_OTEL_ENDPOINT`), plus `opentelemetry-instrumentation-langchain`.
- Resilience: `max_retries=5` on the OpenAI client (gateway 5xx/upstream resets are
  transient); on any LLM failure the chat degrades to the keyword router. The composer
  now also runs on no-tool turns when there is prior history, so short follow-ups
  ("yes", "go ahead") get a real reply instead of a flat "I don't know how to help".

**2. Actor-token cache is capped at 10 seconds (was effectively ~1 hour).**
In `common/auth/actor_token_provider.py`:
- `ACTOR_TOKEN_CACHE_MAX_TTL_SECONDS = 10` caps how long the in-process cache trusts a
  minted actor token, regardless of the IS-issued `exp` (~1h). The underlying JWT keeps
  its real `exp` and is still valid downstream; only our cache re-mints sooner.
- `REFRESH_BUFFER_SECONDS = 2` (was 30). The buffer MUST be < the cap or every token is
  born stale.
- New **short-circuit branch**: WSO2 IS 7.3 may return `flowStatus=SUCCESS_COMPLETED`
  with `authData.code` directly on `POST /oauth2/authorize` when a prior IS session for
  the OAuth client is still valid. The mint detects this and skips the `/authn` step,
  going straight to `/token`. (Surfaced once re-mints became frequent under the 10s cap.)

**3. IS agent deactivation only takes effect on the next mint (revocation finding).**
Live-verified 2026-05-19: deactivating an agent in the IS Console makes IS **reject the
next fresh token mint** (`ABA-60003: login.fail.message`), but IS does **not** invalidate
already-issued JWTs, and there is **no per-call introspection backstop** (deferred —
see [`project_introspection_deferred`]). Net effect: the deactivation lag is bounded by
the actor-token cache TTL — now **~10 seconds** (it was up to ~1 hour before the cap).
This is the dominant control until introspection-per-call or push revocation lands.
The composer renders a plain-language "the agent doesn't have permission to perform this
action right now" reply on the resulting `ERR-CIBA-009` failure.

---

## §1. Architecture

### §1.1 Topology (locked)

```
┌────────┐     login       ┌──────────────┐
│  SPA   │ ─Pattern C────▶ │  WSO2 IS     │
│ (3001) │ ◀──token-A──── │  (9443, VM)  │
└────┬───┘                  └──────┬───────┘
     │ Bearer token-A             │ /oauth2/ciba
     ▼                            │ POST per specialist
┌──────────────┐                  │
│ Orchestrator │                  │
│ (8090) LLM   │                  │
└──┬─────────┬─┘                  │
   │         │                    │
   │ A2A     │ A2A                │
   │ token-A │ token-A            │
   ▼         ▼                    │
┌──────┐  ┌──────┐                │
│ HR   │  │ IT   │ ◀──────────────┤
│ Agent│  │ Agent│ (each does its │
│(8001)│  │(8002)│  own CIBA →    │
└──┬───┘  └──┬───┘  user consent) │
   │         │
   │ Bearer  │ Bearer
   │ token-B │ token-C
   ▼         ▼
┌─────────┐ ┌─────────┐
│HR Server│ │IT Server│
│  (MCP)  │ │  (MCP)  │
│ (8000)  │ │ (8004)  │
└─────────┘ └─────────┘
```

**Tokens:**
- **token-A** (orchestrator session): `sub=user, act.sub=orchestrator-agent` — produced by Pattern C login. Forwarded to specialists on A2A as Bearer.
- **token-B** (HR specialist's OBO): `sub=user, act.sub=hr_agent` — produced by HR's own CIBA flow. Consumed by HR-MCP.
- **token-C** (IT specialist's OBO): `sub=user, act.sub=it_agent` — same shape, produced by IT's own CIBA flow.

`token-B` and `token-C` are **independent**; there is no nested chain between them.

### §1.2 Identity entities in WSO2 IS

| Entity | Type | Role |
|---|---|---|
| `orchestrator-app` | Standard-Based App (OIDC, public/PKCE) | The SPA front-door. Pattern C login. No client_secret. |
| `orchestrator-mcp-client` | Standard-Based App (OIDC, confidential) | Backend client for the orchestrator's `/oauth2/token` code-exchange. **Reason this is a separate app from `orchestrator-app`:** SPA is public (PKCE, no secret); Pattern C `/token` exchange with `actor_token` requires a confidential authenticator. The two-app pattern is *purely* "public front-door + confidential backend code-exchange," NOT the v3 Token-Exchange rationale (TX is gone in v4). |
| `orchestrator-agent` | Agent identity | Named in `requested_actor=` at login. Identity captured in token-A's `act.sub`. |
| `hr_agent` | Agent identity | Initiates CIBA when invoked over A2A. Auth Apps' OAuth Client ID/Secret used for `/oauth2/ciba`. |
| `it_agent` | Agent identity | Same shape as hr_agent. |
| `Orchestrator API` | API Resource | `aud` of token-A. Scopes: `orchestrate`. |
| (no separate API resources for HR/IT) | — | Per F6, CIBA-issued tokens bind `aud` to the calling agent's OAuth Client ID. MCP servers validate that exact value. |

### §1.3 Why per-agent CIBA (the design choice)

The original v3 plan assumed RFC 8693 chained token-exchange would produce a depth-2 nested `act` ladder (`sub=user, act.sub=hr_agent, act.act.sub=orchestrator-agent`). M0 spike confirmed:
- WSO2 IS 7.2 does not implement this (returns `"Impersonator is not found in subject token"`)
- The WSO2 expert directly confirmed it's not on the current IS roadmap

Per-agent CIBA replaces this with per-specialist user consent + depth-1 OBO. This is **not a workaround** — it is the architecture for this demo. See `memory/project_council_decisions_2026_05_07.md` Q1.

---

## §2. Token flow (the five hops)

### Hop 1 — User login (SPA → orchestrator)
- Browser hits `https://localhost:3001` → SPA redirects to `https://13.60.190.47:9443/oauth2/authorize?client_id=<orchestrator-app>&requested_actor=<orchestrator-agent>&...&scope=openid orchestrate`
- User authenticates as their normal corporate identity (probe.user in dev)
- IS shows consent screen referencing `orchestrator-agent` as the actor
- User approves; redirected back to SPA with `code`
- SPA POSTs code to orchestrator backend (`/auth/exchange`); orchestrator backend calls `/oauth2/token` authenticated as `orchestrator-mcp-client`, with `actor_token=` (orchestrator-agent's I4 token)
- Result: **token-A** (`sub=user, aut=APPLICATION_USER, act.sub=orchestrator-agent, aud=<orchestrator-app>, scope=openid orchestrate`)
- Orchestrator stores token-A in session keyed by browser cookie

### Hop 2 — Orchestrator → specialist (A2A)
- User chats: "find me my leave balance"
- Orchestrator's LLM (OpenAI via the AMP gateway) selects `hr_agent` from agent-card discovery
- Orchestrator sends A2A `message/send` request to `http://hr_agent:8001/a2a` with `Authorization: Bearer <token-A>` and `X-Request-ID: <correlation-id>`
- HR-agent validates token-A: signature via JWKS, `iss`, `act.sub` is in HR's trusted-peer allowlist (`{orchestrator-agent}`)
- HR-agent extracts `sub` from token-A → user's UUID for use as `login_hint` in CIBA

### Hop 3 — Specialist initiates CIBA
- HR-agent mints its own actor_token via App-Native Auth (3-step `/oauth2/authn`) → I4 token with `sub=<hr_agent-id>, aut=AGENT`
- HR-agent POSTs `/oauth2/ciba` authenticated as **hr_agent's auto-created Agent App** (Basic auth = OAuth Client ID/Secret), body:
  - `scope=openid hr.read` (or whatever scopes the specific tool needs)
  - `login_hint=<user UUID extracted from token-A.sub>`
  - `binding_message="HR Agent wants to read your leave balance for request <correlation-id>"`
  - `actor_token=<hr_agent's I4 token>`
  - `notification_channel=external`
- IS responds: `{ auth_req_id, auth_url, interval, expires_in }`

### Hop 4 — User consents
- HR-agent returns `auth_url` to orchestrator (in the A2A response body)
- Orchestrator pushes `auth_url` to SPA via WebSocket/SSE
- SPA renders the **Consent Widget** showing `binding_message` + agent name + Approve/Deny buttons
- User clicks Approve → browser navigates to `auth_url` (IS-rendered) → IS shows the OAuth consent page → user confirms
- IS records the consent against the `auth_req_id`

### Hop 5 — Specialist polls + uses token
- HR-agent polls `/oauth2/token` with `grant_type=urn:openid:params:grant-type:ciba` + `auth_req_id` (no `actor_token` on poll, per F5)
- Polls every `interval` seconds; handles `authorization_pending`, `slow_down`, `expired_token`, `access_denied`
- Once user approves, IS returns **token-B**: `{sub=user, aut=APPLICATION_USER, act.sub=hr_agent, aud=<hr_agent-OAuth-Client-ID>, scope=openid hr.read}`
- HR-agent calls hr_server (MCP) with `Authorization: Bearer <token-B>` and `X-Request-ID: <correlation-id>`
- HR-server validates token-B: signature, `iss`, `act.sub` is hr_agent (trusted), `aud` matches hr_agent's OAuth Client ID, scope contains `hr.read`
- HR-server returns leave-balance data
- HR-agent returns A2A response to orchestrator
- Orchestrator's LLM composes user-facing answer

### IT flow is identical, structurally
Same as HR flow with hr_agent → it_agent. **Serial:** orchestrator does HR fully (Hops 2-5) before starting IT. No parallel fan-out per Q2 decision.

---

## §3. Sprint plan (3 sprints)

### Sprint 1 — Happy path build (~10 working days)

**Goal:** demoable end-to-end happy path. User logs in, asks "find my leave balance," approves the consent widget, gets answer. Same for "what laptops are available." Both work in serial.

**Definition of Done:**
- D1.1 — User can log in via SPA; orchestrator session has token-A.
- D1.2 — User chat triggers LLM-driven specialist routing (HR or IT).
- D1.3 — Specialist initiates CIBA; auth_url reaches the SPA Consent Widget.
- D1.4 — User clicks Approve; specialist receives OBO token within polling budget.
- D1.5 — Specialist calls its MCP backend; MCP returns canned data; specialist returns A2A response.
- D1.6 — Orchestrator's LLM renders user-facing answer.
- D1.7 — Two-specialist serial demo query ("leave + laptops") works end-to-end. CIBA scopes use canonical 4-tier names (`hr_self_rest` for HR, `it_assets_read_rest` for IT). Wave 6 simplified `hr.read`/`it.read` strings must not appear in any CIBA initiation call by Sprint 1 close.

**Tasks (in build order):**

| # | Task | Owner | Depends on |
|---|---|---|---|
| S1.1 | IS Console: register all entities per §1.2; capture client IDs/secrets into env files; enable CIBA + Notification Channel=External on each Agent App (per F2/F3) | user | — |
| S1.2 | `common/auth/wso2_is_client.py`: async helpers for App-Native Auth (3-step), Pattern C `/token` exchange, JWKS validation, peer-trust act-chain check | dev | S1.1 |
| S1.3 | `common/auth/ciba_client.py`: async `initiate_ciba()`, `poll_for_token()`, `acquire_obo()` per python-pro spec — typed dataclasses, full error-class hierarchy, retry/back-off per RFC | dev | S1.2 |
| S1.4a | Orchestrator backend (auth + session): FastAPI, login routes (`/auth/login`, `/auth/callback`, `/auth/exchange`), in-memory session store (single-process per Q5), SSE event-stream `/events/{session_id}` for pushing `auth_url` to SPA. Reuse `_archive/agent.before-v3/session.py` `SessionStore` dataclass — swap PKCE state fields for `pending_ciba: dict[auth_req_id, CibaState]`. **Hard dependency for S1.5–S1.10.** | dev | S1.2 |
| S1.4b | Orchestrator LLM integration: OpenAI (bind_tools function-calling) tool-routing + agent-card discovery + **keyword-fallback mode** (`LLM_FALLBACK_MODE=keyword`: "leave"→hr_agent, "laptop"/"asset"→it_agent — deterministic for demo). Can slip to day 8 without blocking auth plumbing. | dev | S1.4a |
| S1.5 | HR-agent backend: FastAPI + A2A endpoint (`/a2a/message/send`), CIBA initiation, polling state per request, MCP client to hr_server | dev | S1.2, S1.3, S1.4a |
| S1.6 | IT-agent backend: identical shape to HR with `it.*` scope and `/it_server` target | dev | S1.5 |
| S1.7 | HR/IT MCP servers: trivial FastAPI tools (`get_leave_balance`, `list_available_assets`) with token validation per §4 (validate `aud == own-paired-agent-OAuth-Client-ID` AND `act.sub == paired-agent-id`) | dev | S1.2 |
| S1.8 | SPA Consent Widget per `docs/consent-widget-spec.md`: serial widget; agent display name + icon, plain-language scope, demoted correlation-id, `binding_message` rendering, expiry countdown, Approve/Deny prominence, latency states (Awaiting → Verifying with your identity provider → Working → Done). Reuse legacy `_archive/agent.before-v3/` callback close-on-success pattern. | dev | S1.4a, consent-widget-spec.md |
| S1.9 | `X-Request-ID` correlation header: generated at SPA, propagated through every A2A and MCP call. Logged at every hop. (Q3 audit MLP) | dev | S1.4a–S1.7 |
| S1.10 | End-to-end demo script: probe.user logs in, asks both queries serially, verifies happy path | dev | all above |
| **S1.11** | **Forward-looking infrastructure (Sprint 3 prep, must land in Sprint 1):** (a) orchestrator session map skeleton: record `(session_id, agent_id, jti, exp, auth_req_id)` on every CIBA completion (no revocation logic yet); (b) `binding_message` template config: `f"{agent_label} wants to {action_summary} on your behalf — request {correlation_id_short}"`; enforce at `ciba_client.initiate_ciba()`; (c) log-redaction baseline: regex strip `Bearer\s\S+`, JWT-shaped strings, `auth_req_id`, `actor_token`. Shared logger middleware. | dev | S1.4a |

**Out of scope for Sprint 1:** denial UX, error fallbacks beyond hard-coded strings, audit log aggregation, revocation, signed envelopes, parallel fan-out.

**Sprint 1 risks:**
- LLM tool-routing flakiness (OpenAI picks wrong specialist) — mitigate with deterministic keyword fallback in dev.
- WebSocket/SSE for auth_url push — first time this team builds it; budget 1 buffer day.
- IS rate-limiting CIBA initiations during dev iteration — keep `expires_in` at 300s and use distinct binding_messages per test.

### Sprint 2 — Polish + correlation + denial (~5 working days)

**Goal:** the demo handles the unhappy paths gracefully and the audit story is defensible.

**Definition of Done:**
- D2.1 — User clicks Deny on a consent widget → orchestrator gracefully reports "HR access not granted; here's what I could find" instead of erroring.
- D2.2 — User closes browser mid-CIBA → orchestrator detects the dead WebSocket and cancels the polling loop.
- D2.3 — `auth_req_id` expires before user clicks → graceful timeout message.
- D2.4 — Every log line at every hop has `X-Request-ID`. Manual `grep` reconstructs the user→orchestrator→hr→hr-mcp chain end-to-end.
- D2.5 — Token expires mid-task (T+3600s) → next user request triggers re-CIBA, framed as "Re-authorizing HR Agent (your access expired)."
- D2.6 — N-tests N18–N26 (council BA recommendations) all pass.
- D2.7 — `hr.admin` can successfully invoke `approve_leave_request` via HR Agent; IS issues token with `scope=hr_approve_rest`; hr_server approves and returns `{success: true}`. **N32** passes.
- D2.8 — `hr.admin` can successfully invoke `issue_asset` via IT Agent (UC-07); IS issues token with `scope=it_assets_write_rest`; it_server records assignment and returns `{success: true, asset_id}`. **N33** passes.
- D2.9 — `probe.user` calling `approve_leave_request` or `issue_asset` is denied at IS (role check); ERR-CIBA-003 (or ERR-CIBA-005) emitted; user sees copy-deck §7.17 message; **no write-tier token ever appears in the IS audit log for `probe.user`**. **N30 + N31** pass.
- D2.10 — All scope strings in CIBA initiation calls, MCP token validators, and test fixtures use the canonical 4-tier names (`hr_basic_rest`, `hr_self_rest`, `hr_read_rest`, `hr_approve_rest`, `it_assets_read_rest`, `it_assets_write_rest`). A grep for `hr\.read|hr\.write|it\.read|it\.assign` returns zero hits in non-archived source files.
- D2.11 — `ERR-MCP-003` (insufficient scope) is reachable and tested: a token-B with `scope=hr_self_rest` presented to `approve_leave_request` returns 401 with `ERR-MCP-003`. Verifies server-side scope guard is enforced even if IS's role check were bypassed by misconfiguration. **N34** passes.

**Tasks:**

| # | Task | Notes |
|---|---|---|
| S2.1 | Denial UX: orchestrator handles `access_denied` from CIBA poll, returns partial-result LLM response | UX from council UX review §4 |
| S2.2 | Browser-closed detection: SPA WebSocket close → orchestrator cancels in-flight `auth_req_id` polling | python-pro Risk 3 mitigation |
| S2.3 | `auth_req_id` expiry path | Graceful timeout |
| S2.4 | Structured logging: every component emits JSON log lines with `request_id, hop, agent_id, action, ts` | Q3 MLP |
| S2.5 | Token-expiry mid-task UX: re-CIBA reuses widget | UX from council UX review §4 |
| S2.6 | N-tests N18–N26 written and passing (per BA review) | See §5 |
| S2.7 | Demo polish: loading states, copy improvements, error messages | UX |

### Sprint 3 — Revocation (~7 working days, narrow scope per Q4)

**Goal:** logging out invalidates everything. Admin-terminate works.

**Definition of Done:**
- D3.1 — User clicks Sign Out in SPA → orchestrator revokes token-A → all in-flight specialist CIBA flows are cancelled → all already-issued specialist tokens (token-B, token-C) become invalid within 5 seconds.
- D3.2 — Admin terminates user's session via IS Console → same effect within 5s.
- D3.3 — R-tests R1–R16 (per security review §4) all pass.
- D3.4 — `binding_message` for re-issued tokens after partial revoke is clearly distinct.

**Tasks:**

| # | Task |
|---|---|
| S3.1 | Orchestrator session map: `session_id → [{agent_id, auth_req_id, jti, exp}]` populated as CIBA round-trips complete |
| S3.2 | Sign-out endpoint: orchestrator calls `/oauth2/revoke` for token-A; fan-out cache-bust to specialists |
| S3.3 | Specialist denylist: in-memory `set[jti]` populated by cache-bust; checked on every MCP call |
| S3.4 | MCP server token introspection: 2s positive cache, hard fail on `active=false` |
| S3.5 | Back-channel logout endpoint on orchestrator (BCL spec) |
| S3.6 | `auth_req_id` cancellation: if user logs out while CIBA is pending, orchestrator must signal IS to invalidate. Capability test C10 needed first (probe whether IS supports this). |
| S3.7 | R-tests R1–R16 written and passing |

---

## §4. Threat model (revised for CIBA)

| Threat | Vector | Mitigation in scope |
|---|---|---|
| **T1 actor_token theft** | Specialist's I4 token leaks (logs, memory dump, HTTP traces) → attacker initiates CIBA posing as that specialist | Strict log redaction (regex strip JWT-shaped strings); in-process actor-token cache capped at **10s** (`ACTOR_TOKEN_CACHE_MAX_TTL_SECONDS`, see §0.1) — the JWT's own `exp` is still IS-issued (~1h) but the cache re-mints every 10s, which also bounds agent-deactivation lag to ~10s; memory-only storage. |
| **T2 auth_req_id interception** | Network MITM (mitigated by TLS but cert is self-signed in dev) | `verify=False` confined to dev env via `IDP_INSECURE_TLS=1` flag; production must pin a real cert. |
| **T3 consent fatigue / phishing** | Attacker spams CIBA prompts; user click-throughs | `binding_message` includes specialist name + scope + correlation_id; rate-limit per (client_id, login_hint) — 1 prompt / 5s; refuse if >N in window. |
| **T4 orchestrator impersonation** | Attacker registers their own MCP-Client app, presents Pattern C token to specialist | Specialist's peer_trust allowlist enforces `act.sub ∈ {orchestrator-agent-id}` on inbound A2A. |
| **T5 token replay across audiences** | Token-B (`aud=<hr_agent-OAuth-Client-ID>`) presented to it_agent or it_server | MCP server validates **both** `aud == own-paired-agent-OAuth-Client-ID` AND `act.sub == paired-agent-id`. The `act.sub` check is the actual cross-agent defense; `aud` alone (per F6) is just the OAuth client identity, easily aliased. |
| **T6 audit-correlation gap** | Attacker drops `X-Request-ID` on A2A leg | Specialist refuses A2A request without `X-Request-ID` header (Sprint 2). |
| **T7 long-running task token expiry** | Task runs >3600s and dies mid-flight | Document the constraint; advise demo scenarios fit in 1hr. (Production: re-CIBA + checkpoint, future.) |
| **T8 Agent App credential = audience authority** | Per F6, `aud` is the agent's OAuth Client ID, so leaking that client_id+secret lets an attacker mint tokens that pass MCP `aud` validation. Worse than a normal client-secret leak because it doubles as the resource identity. | Strict secret-management hygiene; rotate Agent App secrets if compromise suspected; refuse `offline_access` scope on CIBA initiation by default (no refresh tokens minted, per F7 + Q3 decision). |
| **T9 client_id collision / typo** | A copy-paste error in an env file silently routes one specialist's tokens to a different MCP server's expected `aud`, bypassing the cross-audience check by accident | MCP server logs its `EXPECTED_AGENT_OAUTH_CLIENT_ID` at startup; CI test that asserts no two specialists share an OAuth Client ID; N-test added to §5. |

**Residual demo risks accepted (NOT defended against — by Q3 decision):**
- *Actor_token theft window = full token TTL* (1hr). No mid-stream detection.
- *Dev TLS unverified* (`IDP_INSECURE_TLS=1` to localhost/VM cert).
- *Log integrity unsigned.* Application logs and IS audit logs are stitched via `X-Request-ID` only — a tampered or missing log line is undetectable. The "identity-first audit" pitch depends on log-stream integrity, which we are not cryptographically defending in this demo.
- *IS audit log access ownership undefined.* Reconstructing the full chain `user → orchestrator → hr_agent → hr-mcp` requires joining application logs (dev-team owned) with IS-side CIBA event logs (platform-team owned). No SLA, no access process, no test of this dependency for the demo.

**Roadmap (NOT in M0/Sprint 1/2/3):** signed A2A envelopes binding the chain hash, DPoP on actor_token, mutual-TLS to IS, append-only audit chain log, multi-replica state via Redis. See §7.

---

## §5. N-tests catalog (CIBA-aware)

Numbering preserved from v3 where applicable. New tests N18+ added per BA + security council reviews.

| # | Description | Sprint |
|---|---|---|
| N1 | Pattern C login produces token-A with depth-1 act (probe I1 already proves) | 1 |
| N4 | Specialist validates token-A's signature + iss + act.sub allowlist | 1 |
| N6 | Specialist refuses inbound A2A request with malformed token | 1 |
| N7 | Specialist refuses inbound A2A with `act.sub` not in trusted-peer set | 1 |
| N12 | User clicks Deny on a specialist's CIBA → orchestrator returns partial answer with explanation | 2 |
| N16 | hr_server refuses token-C (it_agent's OBO) — wrong audience | 1 |
| N18 | Mid-flow Deny: HR consents OK, IT denied → orchestrator returns "HR shows X; IT access denied" | 2 |
| N19 | All Deny: orchestrator surfaces "I couldn't act on either request" | 2 |
| N20 | `auth_req_id` timeout: user idle past 300s → graceful expiry message | 2 |
| N21 | Cross-specialist replay: capture token-B, present to it_server → 401 | 1 |
| N22 | actor_token theft: stolen actor_token used with wrong login_hint → IS rejects | 1 |
| N23 | Browser closed during polling → orchestrator cancels cleanly, no zombie polls | 2 |
| N24 | Parallel CIBA on same user (rapid double-send) → singleflight by (user_sub, agent, scopes) | 2 |
| N25 | Token-B presented directly to hr_server bypassing hr_agent → 401 (aud mismatch) | 1 |
| N26 | Missing `X-Request-ID` on A2A request → specialist auto-generates + warns OR refuses (decide & enforce) | 2 |
| N27 | **Consent fatigue under serial fan-out:** moderated 5-user usability bar — measure consent-read-time on widget #2 vs #1 to detect muscle-click. Acceptance: ≥80% of users pause on widget #2 (read or hover before clicking). | 2 |
| N28 | **client_id collision detection (T9):** boot MCP server with deliberately-wrong `EXPECTED_AGENT_OAUTH_CLIENT_ID` env → all calls return 401 with clear log line "configured client_id does not match incoming token aud". | 1 |
| N29 | **Token-expiry re-CIBA (UC-06):** set agent token TTL to 60s; run UC-02; wait 70s; resubmit — assert Session Refresh widget fires and new token-B' is issued with `scope=openid hr_self_rest`. | 2 |
| N30 | **Role-denial — Employee → `hr_approve_rest` (UC-08):** `probe.user` triggers CIBA with `scope=openid hr_approve_rest`. IS must deny (`invalid_scope` at init OR `access_denied` at consent). Acceptance: `ERR-CIBA-003` emitted; hr_server never reached. | 2 |
| N31 | **Role-denial — Employee → `it_assets_write_rest` (UC-08):** same pattern with `it_assets_write_rest`. Acceptance: `ERR-CIBA-003` emitted; it_server never reached. | 2 |
| N32 | **HR Admin write happy path — `hr_approve_rest` (UC-07 sibling):** `hr.admin` asks "approve leave LR-001"; CIBA `scope=hr_approve_rest`; hr_server returns `{success:true}`. Acceptance: token has `act.sub=hr_agent`, scope contains `hr_approve_rest`. | 2 |
| N33 | **HR Admin write happy path — `it_assets_write_rest` (UC-07):** `hr.admin` issues laptop; CIBA `scope=it_assets_write_rest`; it_server returns `{success:true, asset_id, assigned_to}`. | 2 |
| N34 | **Cross-scope replay — write-scope token to wrong server:** capture token-C with `scope=it_assets_write_rest`; present to hr_server → 401 `ERR-MCP-001` (aud mismatch). Distinct from N21 because the scope tier adds a second layer of enforcement beyond aud. | 2 |
| R1–R13 | Sprint 2 → moved to Sprint 3 (revocation tests) | 3 |
| R14 | Pending CIBA at logout: user logs out while consent widget visible; subsequent Approve produces invalid token | 3 |
| R15 | Half-fan-out logout: token-B revoked OK, token-C revoke fails → it_agent's introspect catches active=false | 3 |
| R16 | Audit chain integrity post-logout: correlation-ID logs show complete revocation events | 3 |

---

## §6. Definition of Done — overall (M1 sign-off)

The POC ships when:
1. ✓ Sprint 1 D1.1–D1.7 all green
2. ✓ Sprint 2 D2.1–D2.6 all green
3. ✓ Sprint 3 D3.1–D3.4 all green
4. ✓ N-tests N1, N4, N6, N7, N12, N16, N18–N26 passing in CI (mocked-IdP layer)
5. ✓ R-tests R14–R16 passing manually against live IS
6. ✓ Spike memo (`docs/spikes/wso2-is-capability-memo.md`) updated with any new findings discovered during build
7. ✓ Demo script runnable end-to-end in <2 minutes from `docker compose up`
8. ✓ User experience document scenarios A–D updated to reflect actual UX

---

## §7. Roadmap (out of scope, named for future)

| Item | Why deferred | When |
|---|---|---|
| Signed A2A envelopes | Council security recommendation, beyond demo MLP | Production hardening |
| Depth-2 nested act via TX | Not yet supported on WSO2 IS 7.2 | When IS roadmap adds it; per Q1 decision, NOT a transitional concern for this demo |
| Multi-audience CIBA | F6: not supported | Same as above |
| Multi-replica deployment | Single-process is fine for demo (Q5) | Production |
| UAE Pass federation | Out of POC scope | When tenancy demands |
| Refresh-token-driven session extension | F7: available but unused per decision | Future production sessions |
| ELK / structured log aggregation | Correlation-ID-in-logs is sufficient for M1 (Q3) | Production audit |
| TLS pinning + real cert (no self-signed) | Demo-acceptable on IP+self-signed | Production hardening (any non-toy data) |
| Log redaction hardening (CI regex check, multiple rounds) | S1.11 baseline is sufficient for demo | Production hardening |
| IS audit log access ownership + SLA | Undefined for demo (named as residual risk in §4) | Production / compliance review |
| Refresh-token-driven session extension (`offline_access`) | F7: available, but refused at the CIBA-client config layer per Q3+T8 mitigation | Future production sessions |

---

## §8. Where to look

| Concern | File |
|---|---|
| Capability proofs | `idp_capability_test/c{0,1,4,8}_*.py` (passing) |
| Empirical findings | `docs/spikes/wso2-is-capability-memo.md` (canonical) |
| IdP setup walkthrough | `docs/wso2-is-setup.md` |
| User-facing scenarios | `docs/user-experience.md` |
| Scope policy | `docs/scope-policy.md` |
| Agent-card schema | `docs/agent-card-schema.md` |
| CIBA grant config (WSO2 doc) | `docs/configuring-ciba-grant-type.md` |
| Council decisions | `memory/project_council_decisions_2026_05_07.md` |
| Sister sample (closer to our model) | `../hotel-booking-agent-autogen-agent-iam/` |
| Legacy demo (patterns to reuse / supersede) | `_archive/agent.before-v3/` |
| Archived v3 plan | `docs/milestone-plan-v3-rfc8693-archived.md` (do not implement) |
