# Milestone Plan v3 — Identity-First AI Agent Governance POC

**Date:** 2026-05-06
**Status:** v3 — integrates all BLOCKers and FIXes from the v2 re-verification round (architect-reviewer, security-engineer, ai-engineer, api-designer). Supersedes v1 (direct MCP A2A) and v2 (initial orchestrator + agent-card pivot). Adopts the orchestrator + agent-card pattern (Path B hybrid) per [LinkedIn article: From Delegation to Action](https://www.linkedin.com/pulse/from-delegation-action-building-secure-multi-agent-a2a-thilakasiri-zckhc/) and SME guidance, with the security tightenings the reviewers flagged.

## v2→v3 changelog (deltas)
- **§2.1 spike**: added P13 (hr-server `aud` compatibility).
- **§2.5 agent-card schema**: added `schema_version`, `api_version`; namespaced skill IDs (`hr.approve_leave`); `auth.issuer`/`auth.audience` documented **advisory only**, never used as token-exchange parameters; orchestrator hardcodes Asgardeo issuer in JWT validator.
- **§2.6 migration**: `agent/` is **copied + frozen at tag `pre-v3-orchestrator`**, not "kept" — eliminates move-vs-copy ambiguity.
- **§3.3 token flow**: added Hop 4 — hr-agent → hr-server re-mints via RFC 8693 (resolves `aud` mismatch BLOCK).
- **§3.4 routing**: LLM sees opaque `agent_id` enum (`hr-agent`, `it-agent`); orchestrator maps `agent_id`→canonical URL server-side from a private registry.
- **§3.4 transport**: HTTP endpoint is `POST /a2a` (single endpoint, method only in JSON-RPC body); skill IDs namespaced.
- **§3.4 token cache**: cache key is `(verified_user_sub, target_resource, frozenset(scopes))`; **singleflight** via per-key `asyncio.Lock` to handle Gemini parallel tool calls.
- **§3.4 application-error codes**: catalog defined (-32001 invalid_audience, -32002 insufficient_scope, -32003 peer_not_trusted, -32004 session_terminated); JSON-RPC `data` field is a typed payload (no raw exception strings); batch requests rejected with -32600.
- **§3.4 N-tests**: added N14 (`requested_actor` policy denial), N15 (hallucinated skill returns structured error).
- **§3.6 fallback table**: added P1+P10 row.
- **§4.2 cache-bust**: dispatcher derived from agent-card discovery (not hardcoded URLs); cache-bust URL is `<a2a_origin>/internal/auth/cache-bust` by convention.
- **§4.4 cache-bust contract** specified end-to-end (HMAC + `key_id` header + ±30 s window + nonce cache).
- **§5.2 threat model**: added `auth.issuer` JWKS-redirection row (mitigated by hardcoded issuer + advisory-only treatment).
- **§5.3 single-replica**: `docker-compose.yml` declares `deploy.replicas: 1` for orchestrator; tested.
- Misc: stream cancellation propagates to `httpx` via cancel-aware client; agent-card 4xx re-fetch cooldown ≥30 s + jitter; surgical redactor strips `url`/`auth` from any agent-card-shaped object before LLM trace; STS negative cache keyed per-user for exchange calls (global only for actor-token mint).
- **§ Architecture symmetry fix:** added `it-server/` as a sibling to `hr-server/` so IT Asset Agent wraps a real backend (parallel to HR Agent → hr-server) instead of holding asset data in-memory. **Implementation must mirror `hr-server/` exactly** — same in-memory store pattern, same FastAPI+FastMCP wiring, no incidental complexity. Adds **Hop 5** (IT Agent → it-server re-mint, parallel to Hop 4), new spike probe **P14**, new API resource `it-server-api`, tests **N16/N17**. Symmetry tests Hop-4-style re-mint on two independent paths — proves the pattern composes.

## v3 POC review (2026-05-06) — applied trims
After v3 was reviewed by architect-reviewer + security-engineer with explicit POC framing, the following were trimmed (full production behavior captured in `docs/production-hardening.md`):
- **Cache-bust HMAC simplified:** dropped `key_id` overlap rotation + nonce cache. Kept HMAC + ±5 min timestamp window. Internal compose-network call; threat is "not anonymous," not "replay-from-network-capture."
- **STS negative cache simplified:** single global cache, no jitter. Per-user keying + jittered refresh deferred.
- **OIDC BCL §2.6 strict checklist trimmed:** dropped `iat` skew, `typ: logout+jwt` strict, alg pinning. Kept iss/aud/exp/sig/events/sid/jti single-use.
- **Spike probes trimmed:** P6 (BCL retry policy) and P9 (jti stability) deferred. 12 probes ship.
- **Fallback table reframed:** "fix tenant first" is the preferred response to probe failures; fallbacks are last-resort.
- Architectural decisions held against architect's "production-shaped" critique: kept `it-server/` + Hop 5 (security-engineer marked LOAD-BEARING; user-confirmed), kept two-layer revocation, kept agent-card non-trust-anchor proofs (N8/N8b), kept full N1/N7/N3 audience+chain test set.
- Added **R12** (joint-failure: introspect-down + cache-bust-failed → fail-closed 503) and **R13** (post-BCL chat session invalidation) per security-engineer's MISSING findings.
- Internal consistency fixes: §2.8 P1–P14, §3.5 "five hops," §3.3 Hop 2 `aud` hedged, Hop 3a chain-depth hedged pending P12, N6 rewritten to test missing-`act` rule (not signature failure), §1.3 review history updated, "v3 improvement" → "future" in §5.4, P95 → "max observed" in §4.5.
**Scope:** Sprint 0 (capability spike + shared refactor + service split), Sprint 1 (Orchestrator + A2A + agent cards), Sprint 2 (Secure Session Termination).
**Out of scope (confirmed):** UAE Pass federation, WSO2 API Manager, PII / prompt-injection guardrails, multi-replica deployment hardening, cross-domain orchestration.

---

## 1. Status snapshot

| Scenario | POC doc claim | Code reality | This plan |
|---|---|---|---|
| 1. Human → Agent OBO | Implemented | ✅ Implemented (Asgardeo + LangChain 1.2.11 + Gemini + MCP, single `agent/` service) | **Reshaped in Sprint 1** — split into orchestrator + HR specialist; user-login flow switches to `requested_actor` pattern. |
| 2. Agent-to-Agent | Implemented | ❌ Missing | **Sprint 1 deliverable** — orchestrator coordinates HR + IT specialists via A2A JSON-RPC + agent cards; specialist↔specialist call (HR→IT) deferred to "stretch" demo. |
| 3. Secure Session Termination | Implemented | ⚠️ Partial — in-memory logout only | **Sprint 2 deliverable** — BCL on orchestrator + introspection on specialists. |
| 4. Zero-Trust / Guardrails | Implemented | ⚠️ Partial — JWT scope enforcement works | Deferred. |

### 1.1 Architecture (target)

```
                        ┌────────────────────────┐
                        │      Asgardeo          │
                        │  (WSO2 IS SaaS / IdP)  │
                        └────────────┬───────────┘
                                     │ tokens, BCL, introspection
                                     │
            ┌────────────────────────┴───────────────────────┐
            │                                                │
            │            ┌──────────────────────┐            │
   user ────┼──PKCE──▶  │  Orchestrator Agent  │            │
   (browser)│           │  (LLM = Gemini,      │            │
            │           │   LangChain,         │            │
            │           │   discover_agents)   │            │
            │           └────────┬─────────────┘            │
            │           A2A JSON-RPC  /.well-known/agent-card.json
            │             "message/send"                    │
            │                    │                          │
            │   ┌────────────────┴────────────────┐         │
            │   ▼                                 ▼         │
            │ ┌──────────────────┐       ┌──────────────────┐
            │ │  HR Agent        │       │  IT Asset Agent  │
            │ │  (specialist,    │       │  (specialist,    │
            │ │   wraps hr-svr)  │       │   wraps it-svr)  │
            │ │  agent_card.json │       │  agent_card.json │
            │ └─────────┬────────┘       └─────────┬────────┘
            │           │ MCP (tool dispatch)      │ MCP (tool dispatch)
            │           ▼                          ▼
            │   ┌──────────────┐            ┌──────────────┐
            │   │  hr-server   │            │  it-server   │
            │   │  MCP + REST  │            │  MCP         │
            │   │  (existing)  │            │  (new, mirrors hr-server shape)
            │   └──────────────┘            └──────────────┘
            │
        SPA (client/) ──────► Orchestrator (HTTP/streaming chat)
```

**Services after Sprint 1:** `client/` (existing), `orchestrator/` (extracted from `agent/`), `hr-agent/` (extracted identity layer + new A2A surface; calls `hr-server` via MCP), `it-agent/` (new specialist; calls `it-server` via MCP), `hr-server/` (existing — unchanged), `it-server/` (new — mirrors hr-server shape with in-memory asset store).

### 1.2 Decided design parameters (confirmed)

- **Path B (hybrid):** A2A JSON-RPC for orchestrator ↔ specialists, MCP retained inside specialists for tool dispatch.
- **User → Orchestrator token:** OAuth 2.1 authorization-code + PKCE, with **Asgardeo `requested_actor=<orchestrator_client_id>` parameter** on the authorize endpoint (per the article's pattern; Asgardeo's "OBO for AI Agents" draft implementation). Resulting access token: `sub=user`, `act.sub=orchestrator-id`, `aud=orchestrator-resource`, `scope=<requested>`.
- **Orchestrator → Specialist token:** **RFC 8693 token-exchange** per target specialist. `subject_token`=user delegated token, `actor_token`=orchestrator's pre-minted client_credentials access token, `resource`=<specialist URI> (RFC 8707), `scope`=narrowed. Resulting token: `sub=user`, **`act.sub=orchestrator-id`**, with deeper nesting `act.act.sub` populated only if Asgardeo preserves the existing `act` from the user delegated token (see §3.7) — **shape verified by P12; Hop 3a flow diagram is illustrative pending that result**.
- **Specialist → Specialist token (HR→IT, *stretch goal*):** RFC 8693 chained re-mint with `subject_token`=incoming HR token, `actor_token`=HR-Agent client cred. Chain depth 2.
- **Discovery:** agent cards at `/.well-known/agent-card.json` on each specialist. Orchestrator fetches at startup, exposes `discover_agents` tool to LLM.
- **Revocation latency:** ≤ 5 s, via introspection on every A2A call (≤2-s positive cache, BCL-driven cache-bust) + Asgardeo back-channel logout to orchestrator.
- **Demo story:** orchestrator-coordinated. Orchestrator's LLM decides which specialists to query and composes the answer. Specialist-to-specialist (HR→IT) is a stretch demo, not a Sprint 1 DoD requirement.
- **Sprint cadence:** DoD-driven, no time-box.

### 1.3 Independent verification history
- v1 (direct HR↔IT MCP): reviewed by `architect-reviewer`, `security-engineer`, `ai-engineer`, `api-designer`. Resulted in v2.
- v2 (orchestrator + agent-card pivot): re-reviewed by all four. Resulted in v3.
- v3 (this version, with `it-server/` symmetry): reviewed by architect-reviewer + security-engineer with explicit POC framing. Their findings are integrated below.

---

## 2. Sprint 0 — Capability spike + shared refactor + service-split scaffolding

Sprint 0 is **complete and signed off** before any Sprint 1 code lands. It now covers (a) live-tenant Asgardeo capability verification, (b) shared `common/auth/` package, (c) scope policy, (d) library pins, (e) **agent-card schema definition**, (f) **service-split scaffolding**.

### 2.1 Asgardeo capability spike (BLOCKER)

| Probe | Question | Pass criterion |
|---|---|---|
| P1 | Does Asgardeo's RFC 8693 token-exchange grant accept `actor_token` (a pre-minted orchestrator client-credentials access token) and populate the `act` claim? | `act.sub == orchestrator client_id` present in returned JWT. |
| P2 | Does Asgardeo's token-exchange honor `resource` (RFC 8707) and reflect it as exact-match `aud`? | Token's `aud` exactly equals requested resource URI. |
| P3 | Does revoking the user's refresh token cascade-invalidate access tokens minted via token-exchange from it? | Exchanged token returns `active: false` from `/oauth2/introspect`. |
| P4 | Does Asgardeo deliver an OIDC back-channel logout token to a registered RP `backchannel_logout_uri` on user logout (user-initiated AND admin-terminated)? | Signed JWT with `events` and `sid` arrives within 30 s. |
| P5 | Does `/oauth2/introspect` reflect a manually-revoked token within 5 s (P95)? | `active: false` within budget. |
| ~~P6~~ | ~~What is Asgardeo's BCL retry policy / delivery semantics?~~ | **DEFERRED** (POC). Layer A (introspection cache) is the safety net; BCL retry behavior doesn't change the demo's "≤ 5 s revocation" claim. Re-add in production hardening. |
| P7 | Is `sid` present in **access tokens** and **exchanged access tokens**, or only ID tokens? | If access tokens lack `sid`, Layer-B termination correlates by stored `sid` from ID token at login. |
| P8 | Does Asgardeo preserve nested `act` chains when `subject_token` is itself an exchanged token? | `act.act.sub` populated correctly across two exchanges — only required if the stretch HR→IT specialist-to-specialist demo is in scope; otherwise can be deferred. |
| ~~P9~~ | ~~Does `/oauth2/introspect` always return a stable `jti`?~~ | **DEFERRED** (POC). Implementation falls back to `hash(token)` as cache key unconditionally; jti stability is a production performance optimization, not a correctness gate. |
| **P10** | Does Asgardeo's authorize endpoint accept `requested_actor=<client_id>` and produce a delegated token with `act.sub` populated? Is it ID token, access token, or both? | `act.sub == orchestrator client_id` in the access token returned to the orchestrator after code exchange. |
| **P11** | Does Asgardeo render a user-consent screen for `requested_actor` ("delegate to <agent name>?")? | Consent prompt shown; user can deny. Required for the governance story. |
| **P12** | Does `requested_actor`-derived token compose with subsequent RFC 8693 token-exchange (the orchestrator re-minting per specialist)? Does the resulting token preserve the actor chain? | Re-minted token has chain `act.act.sub=orchestrator, act.sub=orchestrator` OR documented Asgardeo flattening behavior. **Validator must require `act` non-empty regardless** — if Asgardeo flattens, lock validator to flattened shape and drop the stretch HR→IT chained-delegation demo. |
| **P13** | Does `hr-server` accept tokens whose `aud` is the canonical hr-agent A2A URI? Or does hr-server's existing JWT validation reject them? | If reject (expected), hr-agent must re-mint via RFC 8693 for `hr-server`'s configured audience (Hop 4 in §3.3). Document hr-server's actual `EXPECTED_AUD`. |
| **P14** | What is `it-server`'s expected `aud` (defined at registration; e.g., `https://it-server.local/mcp`)? Does the chosen Asgardeo API resource for it-server reflect `aud` exactly? | Document `EXPECTED_AUD` for it-server; confirm RFC 8693 token-exchange targeting that resource yields the right `aud`. Mirrors P13 for the IT path. |

**Spike memo template** (`docs/spikes/asgardeo-capability-memo.md`):
- Tenant name and date.
- Per probe: probe ID, curl invocation, raw response excerpt (redact bearers — preserve only enough to identify the claim), pass/fail verdict, Asgardeo doc link, decision impact.
- Decisions section: which §3.6 / §4.6 fallbacks are activated, if any.
- Sign-off block (lead engineer + security-engineer).

### 2.2 Shared `common/auth/` + `common/a2a/` packages

Extract before any new specialist code.

`common/auth/`:
- `jwt_validator.py` — JWKS signature, `iss`, **exact** `aud`, `exp`/`nbf`, scope extraction, full `act` chain extraction.
- `introspector.py` — RFC 7662 client. Default 2-s positive cache. BCL-driven invalidation hook. `jti` keying with hash-of-token fallback.
- `peer_trust.py` — **nested** `act` chain validation against an allowlist of peer-agent client_ids. Walks every level; rejects on first mismatch; rejects on `act` absent for resources that require delegation.
- `errors.py` — uniform error envelope: `{"error": "<code>", "required_scope"?: str, "available_scopes"?: list[str], "message": str}`.

`common/a2a/`:
- `agent_card.py` — Pydantic model for `/.well-known/agent-card.json` (see §2.5). Server helper for publishing; client helper for fetching + caching.
- `jsonrpc.py` — JSON-RPC 2.0 helpers: request/response envelopes, error codes, validators.
- `a2a_client.py` — orchestrator-side client: GET agent card → cache → POST `message/send` with `Authorization: Bearer <token>`.

### 2.3 Scope naming policy (`docs/scope-policy.md`)

Existing tenant uses `<resource>_<action>_mcp`. v2 keeps this:
- `agent_access` — umbrella user-scope gate (existing).
- `hr_basic_mcp`, `hr_self_mcp`, `hr_read_mcp`, `hr_approve_mcp` — existing HR scopes.
- `it_assets_read_mcp` — new (Sprint 1).
- `it_assets_write_mcp` — reserved (not used in this POC).

**A2A note:** the Agent Card advertises *capabilities* (skills) for LLM routing; the *enforcement* surface is OAuth scopes registered in Asgardeo. This separation is documented in `docs/scope-policy.md`.

### 2.4 Library version pins

- **`langchain-mcp-adapters >= 0.1.18`** — for per-request header injection. Verified by Sprint 0 smoke test.
- **`a2a-python` SDK** — only if used. If we hand-roll the JSON-RPC server (it's small), no SDK dependency needed; document the choice in `common/a2a/`. **Recommendation:** hand-roll; reduces dependency surface and forces uniform error envelope.

### 2.5 Agent-card schema (committed before Sprint 1)

Saved as `docs/agent-card-schema.md` and codified in `common/a2a/agent_card.py`:

```jsonc
{
  "schema_version": "v3-custom",                       // distinguishes from a future A2A SDK card
  "name": "HR Agent",                                  // human-readable
  "description": "Handles HR queries: leave, time-off, employee info.",
  "url": "https://hr.smart-employee.local/a2a",        // canonical specialist URI; ALSO the token-exchange `resource` parameter — derived from the allowlisted fetch URL, not from the card body
  "api_version": "1.0.0",                              // semver of the specialist's A2A surface; major bump = breaking
  "skills": [
    { "id": "hr.approve_leave",                        // namespaced; cross-agent collisions impossible
      "name": "Approve leave request",
      "description": "Approve or reject a leave request by id.",
      "required_scopes": ["hr_approve_mcp"] },        // documentation only; enforcement is at JWT validation
    { "id": "hr.get_leave_balance",
      "name": "Get leave balance",
      "description": "Return remaining leave days for an employee.",
      "required_scopes": ["hr_read_mcp"] }
  ],
  "capabilities": { "streaming": false, "pushNotifications": false },
  "auth": {
    "scheme": "oauth2",
    "issuer": "<asgardeo_issuer>",                     // ADVISORY ONLY — never used at runtime
    "audience": "https://hr.smart-employee.local/a2a"  // ADVISORY ONLY — orchestrator uses the allowlisted URL
  }
}
```

**Security-critical semantics (BLOCK fixed):**
- `auth.issuer` and `auth.audience` are **advisory metadata only**. They MUST NEVER be used at runtime to set up JWKS or as the `resource` parameter for token-exchange. The orchestrator hardcodes the Asgardeo issuer in `common/auth/jwt_validator.py`; if a card's `auth.issuer` differs, log and refuse to load the card. The token-exchange `resource` parameter is taken from the **allowlisted fetch URL** (which is verified against `ORCHESTRATOR_AGENT_CARD_URLS` before the card body is parsed).
- The card is NOT a trust anchor. Tokens are the trust anchor; cards are routing metadata.
- `schema_version` is checked at parse time; unknown values cause graceful fallback (the orchestrator logs and ignores the card).
- `api_version` major drift triggers a warning + skill list re-fetch.
- Skill IDs are **namespaced** (`<agent>.<verb>`) to make collisions impossible across multi-specialist deployments.

### 2.6 Service-split scaffolding (new in v2)

**Before Sprint 1 starts**, create the directory layout (empty stubs are fine):
- `orchestrator/` — to receive code from `agent/` (chat endpoint, LLM, session, BCL).
- `hr-agent/` — new specialist that wraps `hr-server` via MCP. Owns A2A endpoint + agent card.
- `it-agent/` — new specialist that wraps `it-server` via MCP. Owns A2A endpoint + agent card.
- `it-server/` — **new backend** mirroring `hr-server/` shape (FastAPI + FastMCP). In-memory asset fixture (5–10 sample rows). Validates `aud=<it-server canonical URI>`, scope `it_assets_read_mcp`, nested `act` allowlist (Sprint 1 allowlists `it-agent` AND `orchestrator-agent` so the chain user→orchestrator→it-agent→it-server validates).
- `agent/` — copied to `_archive/agent.before-v3/` and tagged; deleted on M1 sign-off.
- `hr-server/` — unchanged.
- `client/` — minor changes for the new login URL parameters (Sprint 1 task).

`docker-compose.yml` updated with the new services. Ports: orchestrator 8080, hr-agent 8001, it-agent 8002, hr-server 8003, **it-server 8004**.

### 2.7 Migration / rollback

Sprint 0 **copies** `agent/` to `_archive/agent.before-v3/` and tags the last-good commit as `pre-v3-orchestrator`. Sprint 1 implements the new orchestrator + specialists. The original `agent/` directory becomes a frozen reference; it is **deleted** from the working tree on M1 sign-off (the tag remains for git-history rollback). This eliminates the v2 move-vs-copy ambiguity flagged by architect-reviewer — there is no period during which the codebase has half-moved code.

### 2.8 Sprint 0 sign-off contract

Sprint 0 is complete when:
1. **`docs/asgardeo-setup.md` is committed** — see §2.9 for required content. This is the prerequisite for the spike (you cannot probe a tenant that hasn't been configured).
2. `docs/spikes/asgardeo-capability-memo.md` is committed with all 14 probes (P1–P14) recorded — signed off by lead + security-engineer.
3. `common/auth/` and `common/a2a/` packages are committed and consumed by `hr-server/` (introspection feature-flagged, default OFF; agent-card unused yet but importable).
4. `docs/scope-policy.md`, `docs/agent-card-schema.md`, and `docs/user-experience.md` are committed.
5. `requirements.txt` files are updated; per-request MCP header injection smoke test passes.
6. Empty service stubs (`orchestrator/`, `hr-agent/`, `it-agent/`) exist and `docker compose up` builds (won't yet do anything useful).
7. `agent/` is copied to `_archive/agent.before-v3/` and tagged `pre-v3-orchestrator`.

If P1+P2+P10 fail, Sprint 1 enters fallback mode (§3.6) before Sprint 0 closes.

### 2.9 Asgardeo configuration guide (`docs/asgardeo-setup.md`)

A step-by-step instruction file the lead engineer follows to bring an Asgardeo tenant from "freshly created" to "ready for the POC spike and Sprints 1–2." Owner: lead engineer. Reviewer: security-engineer.

**Why this is its own task:** the four Asgardeo gotchas (App-Native Auth disabled by default, Role Audience set to Application instead of Organization, agent not assigned to roles, Token Exchange grant not enabled) bite every new user, and the v3 architecture adds new configuration that the existing README doesn't cover (`requested_actor` policy, back-channel logout URL, three new agent identities, two new API resources). Without a single canonical setup doc, every team member rediscovers the same gotchas.

**Required content for the Sprint 0 baseline:**
1. **Prerequisites** — which Asgardeo tenant to use; admin credentials needed; any required Asgardeo plan/feature flags.
2. **Application registration**
    - `orchestrator-app` — Single-page application (PKCE), redirect URI `http://localhost:5001/callback`, configured to permit `requested_actor=orchestrator-agent`.
    - **Enable RFC 8693 token-exchange grant** on `orchestrator-app` with permitted resources `hr-agent-api`, `it-agent-api`, `hr-server-api` (the canonical resource URIs are listed in §3.3 of the milestone plan).
    - Document the **App-Native Authentication** toggle (existing gotcha #A from `project_readme_pending_improvements.md`).
3. **Agent identity registration**
    - `orchestrator-agent`, `hr-agent`, `it-agent` — three Agent identities. Capture client_id / client_secret for each.
    - Each agent must be **assigned to roles** (gotcha #D) so it can mint tokens with appropriate scopes.
4. **API resource registration**
    - `hr-agent-api` (audience = `https://hr.smart-employee.local/a2a`), scopes `hr_basic_mcp`, `hr_self_mcp`, `hr_read_mcp`, `hr_approve_mcp`.
    - `it-agent-api` (audience = `https://it.smart-employee.local/a2a`), scope `it_assets_read_mcp` (and reserved `it_assets_write_mcp`).
    - `hr-server-api` (existing — audience = MCP Client app ID; verify exact value in P13).
    - `it-server-api` (new — audience = `https://it-server.local/mcp`), scope `it_assets_read_mcp` (Hop 5 target; verify exact value in P14).
    - **Set Role Audience to Organization** on every app (gotcha #B and #C).
5. **Role configuration**
    - `employee` and `hr_admin` roles, with the scope assignments per `docs/scope-policy.md`.
    - Assign the appropriate agent identities to each role (gotcha #D).
6. **Back-channel logout configuration** (Sprint 2 prep — can be staged)
    - Set `backchannel_logout_uri = http://localhost:5001/auth/backchannel-logout` on `orchestrator-app`.
    - Confirm the application's `id_token_signed_response_alg`.
7. **Verification curl scripts** — one curl per probe (P1–P13) the lead engineer runs to confirm each capability before declaring spike-ready. These reduce the spike (§2.1) to "run scripts, paste output, sign off."
8. **Troubleshooting** — copy the four gotcha symptoms from `project_readme_pending_improvements.md` and add new ones for v3 (e.g., "consent screen not shown" → check `requested_actor` policy; "token-exchange returns no `act` claim" → P1 fail, see plan §3.6 fallback).

**Living document:** Sprint 1 adds tasks 1–5 of §3.4-B as updates to this doc (in addition to performing them). Sprint 2 adds task 1 of §4.4-A (back-channel logout activation). The doc is the single source of truth for "how do I set up Asgardeo for this POC."

**Acceptance:** a teammate who has not been involved in the architecture work can clone the repo, follow `docs/asgardeo-setup.md` end to end, and run the spike successfully without asking questions.

---

## 3. Sprint 1 — Orchestrator + Agent Cards + A2A

### 3.1 Goal

User logs into the orchestrator with a delegated session (`requested_actor=orchestrator-id`). The orchestrator's LLM, given a leave-approval request that depends on equipment status, **discovers** the HR Agent and IT Asset Agent via their agent cards, **independently** calls each via A2A JSON-RPC, **mints a per-target audience-narrowed token** (RFC 8693) for each call, and composes the final answer for the user.

Negative tests prove (a) the agent cards are not the trust anchor — tokens are; (b) audience-narrowed tokens cannot be replayed cross-specialist; (c) the act chain is preserved and validated; (d) the orchestrator cannot escalate beyond the user's consented scopes.

### 3.2 Components introduced

| Component | Purpose | Asgardeo identity |
|---|---|---|
| `orchestrator/` (extracted from `agent/`) | User-facing chat. LLM-driven (Gemini). Discovers agents, routes via A2A, performs token-exchange. Handles BCL (Sprint 2). | New **Application** registration: `orchestrator-app` (PKCE public/confidential client) + new **Agent identity**: `orchestrator-agent` (for `requested_actor` and as actor in token-exchange). |
| `hr-agent/` | Specialist. Owns `/a2a/message:send`, `/.well-known/agent-card.json`. Wraps `hr-server` MCP for tool dispatch. | New **Agent identity**: `hr-agent` + API resource `hr-agent-api` with existing `hr_*_mcp` scopes. |
| `it-agent/` | Specialist. Same pattern. Wraps `it-server/` via MCP for tool dispatch (parallel to HR Agent → hr-server). | New **Agent identity**: `it-agent` + API resource `it-agent-api` with `it_assets_read_mcp` scope. |
| `it-server/` | New backend. FastAPI + FastMCP, in-memory asset store (5–10 sample rows). Mirrors `hr-server/` shape. | New API resource `it-server-api` with `it_assets_read_mcp` scope. |

### 3.3 Token flow

```
─── Hop 1: User → Orchestrator (login) ─────────────────────────────────
  Browser → Asgardeo /authorize?
              client_id=orchestrator-app&
              requested_actor=orchestrator-agent&
              scope=openid+agent_access+hr_read_mcp+hr_approve_mcp+it_assets_read_mcp&
              code_challenge=...&audience=orchestrator-resource
            (consent screen: "Delegate to Orchestrator Agent?")
  Browser → Orchestrator /callback?code=...
  Orchestrator → Asgardeo /token (code, code_verifier)
            ⇒ user_delegated_token: {sub: user, act.sub: orchestrator-agent,
                                      aud: orchestrator-resource, scope: <requested>}

─── Hop 2: Orchestrator → Asgardeo (mint actor token; cached) ───────────
  Orchestrator → Asgardeo /token  (client_credentials, scope=internal)
            ⇒ orchestrator_actor_token: {sub: orchestrator-agent, aud: <as configured>}
                                          (Asgardeo's actual aud may not be "self";
                                           verify in spike — used only as actor_token in Hop 3)

─── Hop 3a: Orchestrator → HR Agent (per-call exchange) ─────────────────
  Orchestrator → Asgardeo /token  (RFC 8693 token-exchange,
              subject_token=user_delegated_token,
              actor_token=orchestrator_actor_token,
              resource=https://hr.smart-employee.local/a2a,
              scope=hr_read_mcp+hr_approve_mcp)
            ⇒ hr_call_token: {sub: user, act.sub: orchestrator-agent,
                              aud: https://hr.smart-employee.local/a2a, scope: ...}
  Orchestrator → HR Agent /a2a/message:send
              Authorization: Bearer <hr_call_token>
              Body: {"jsonrpc":"2.0","method":"message/send", ...}
  HR Agent: validate sig+iss+aud(exact)+exp+scope+act.sub(allowlist)
            → dispatch to hr-server MCP via existing tools

─── Hop 3b: Orchestrator → IT Agent (per-call exchange) ─────────────────
  Symmetric: aud=https://it.smart-employee.local/a2a, scope=it_assets_read_mcp

─── Hop 4: HR Agent → hr-server (specialist's internal tool dispatch) ───
  HR Agent → Asgardeo /token  (RFC 8693 token-exchange,
              subject_token=hr_call_token (the one HR Agent received),
              actor_token=hr-agent client_credentials access token,
              resource=<hr-server EXPECTED_AUD verified by P13>,
              scope=hr_read_mcp+hr_approve_mcp)
            ⇒ hr_server_token: {sub: user, act.sub: hr-agent,
                                  act.act.sub: orchestrator-agent,
                                  aud: <hr-server canonical URI>}
  HR Agent → hr-server MCP  Authorization: Bearer <hr_server_token>
  hr-server: validate sig+iss+aud(exact, =EXPECTED_AUD)+exp+scope+nested act
            → existing tool dispatch

─── Hop 5: IT Agent → it-server (parallel to Hop 4) ─────────────────────
  IT Agent → Asgardeo /token  (RFC 8693 token-exchange,
              subject_token=it_call_token (the one IT Agent received),
              actor_token=it-agent client_credentials access token,
              resource=https://it-server.local/mcp  (verified by P14),
              scope=it_assets_read_mcp)
            ⇒ it_server_token: {sub: user, act.sub: it-agent,
                                  act.act.sub: orchestrator-agent,
                                  aud: https://it-server.local/mcp}
  IT Agent → it-server MCP  Authorization: Bearer <it_server_token>
  it-server: validate sig+iss+aud(exact)+exp+scope+nested act
            → in-memory asset store dispatch
```

**Hop 4 and Hop 5 demonstrate the same security property on two paths:** the agent → backend re-mint pattern with full 3-name `act` chain (`user`/`orchestrator`/`specialist-agent`). v2 had hr-agent forwarding the incoming token to hr-server, which would either fail (token's `aud` doesn't match hr-server's `EXPECTED_AUD`) or pass permissively (worse). v3 has each specialist re-mint per RFC 8693 for its backend's exact resource URI. Spikes P13 (hr-server) and P14 (it-server) verify each backend's `EXPECTED_AUD` so the resource URI in the exchange call is correct.

### 3.4 Task list (sequenced)

**A — Sprint 0 sign-off prerequisite** (no Sprint 1 code starts otherwise).

**B — Asgardeo configuration**
1. Register `orchestrator-app` (Application). Configure callback, PKCE required.
2. Register `orchestrator-agent`, `hr-agent`, `it-agent` as Agent identities. Issue client credentials.
3. Register API resources `hr-agent-api`, `it-agent-api` with appropriate scopes.
4. Configure `requested_actor` permitted-actors policy: `orchestrator-app` may use `requested_actor=orchestrator-agent`.
5. Configure RFC 8693 token-exchange grant on `orchestrator-app` with permitted resources `hr-agent-api`, `it-agent-api`.

**C — Orchestrator extraction (`orchestrator/`)**
6. Move chat endpoint, session store, LangChain integration, Gemini wiring from `agent/` to `orchestrator/`. **Drop direct `hr-server` MCP wiring** — orchestrator no longer talks to `hr-server`; it talks to `hr-agent` via A2A.
7. Update `client/app.js`: login URL builder appends `requested_actor=<orchestrator_agent_id>`. Add a small UI hint on the login button: "(delegating chat to Orchestrator Agent)".
8. Implement actor-token cache (`orchestrator/actor_token.py`): client_credentials → access token. Refresh at 80% of TTL with jitter. Negative cache 30 s on STS errors.
9. Implement RFC 8693 client (`orchestrator/token_exchange.py`). Cache key = `(verified_user_sub, target_resource, frozenset(requested_scopes))` — scope MUST be in the key to prevent silent over-grant when concurrent calls request different scopes. `verified_user_sub` is taken from the **signature-validated** incoming user token, never from request headers. TTL = `min(token_exp, 5 min)`. **Singleflight via per-key `asyncio.Lock`**: first caller mints, subsequent callers await the in-flight mint (handles Gemini parallel tool calls cleanly). Counter `token_exchange_singleflight_waits_total`. **STS negative cache: single global cache, 30 s, no jitter** (POC simplification; per-user keying + jittered refresh deferred to `production-hardening.md`).
10. Implement A2A client (`common/a2a/a2a_client.py`):
    - GET agent card from an URL in `ORCHESTRATOR_AGENT_CARD_URLS` allowlist (BEFORE the body is parsed). Schema-version check; reject unknown.
    - Cache cards 5 min (jittered). Re-fetch cooldown ≥30 s between forced refetches (prevents misbehaving specialist DoSing the discovery cache).
    - On 3 consecutive `method_not_found`-style errors for a given skill, force card refresh and prune the missing skill from the LLM's view next turn.
    - POST `message/send` to `<allowlisted_url>/a2a` (single endpoint per specialist; method only in JSON-RPC body). Authorization header set via header-callable (per-request from `RunnableConfig`).
    - **The token-exchange `resource` parameter is the allowlisted URL** — NEVER the card body's `auth.audience` (advisory only).
11. Implement **opaque `agent_id` LLM routing** (BLOCK fix): LLM-facing `discover_agents` tool returns a list of `{agent_id: str, name: str, description: str, skills: [{id, name, description}]}` — **no `url` and no `auth` fields**. The LLM's per-skill tool calls use only the namespaced `skill_id` (e.g., `hr.approve_leave`); orchestrator maps `skill_id` → `(agent_id, url)` from a private server-side registry. `agent_id` is a Pydantic enum constrained to known agents so Gemini's function schema cannot fabricate an unknown one.
12. **Hallucinated/unknown skill** path: orchestrator's tool dispatcher returns a structured error envelope (`{"error": "unknown_skill", "available": [...]}`) the LLM can reason about, never an exception that bubbles up.
13. **Tool-schema hygiene:** the `employee_id` parameter exposed to specialist tools is **server-derived** from the verified user's `sub` (or a session-store mapping). Not LLM-controlled. Pydantic validators reject mismatch.
13a. **Trace leak hardening for cards:** the surgical LangSmith redactor strips `url` and `auth` keys from any agent-card-shaped object before it enters the trace, in addition to the bearer/JWT regex masks (§F).

**D — HR Agent specialist (`hr-agent/`)**
14. Hand-rolled FastAPI A2A endpoint: `POST /a2a` (single endpoint; JSON-RPC 2.0). Accepts `application/json`. Method is `message/send` (in JSON-RPC body, not URL). Body parameters validated against per-skill Pydantic schemas. **Reject JSON-RPC batch requests** (`[...]` body) with `-32600` until batch semantics are reviewed (avoids amplification + per-item auth confusion).
15. Agent card at `GET /.well-known/agent-card.json` (per §2.5).
16. **Re-mints token via RFC 8693 to call hr-server (Hop 4 in §3.3)** — does NOT forward the incoming token. `subject_token` = incoming `hr_call_token`, `actor_token` = hr-agent client_credentials access token (cached, refreshed at 80% TTL with jitter), `resource` = hr-server's verified `EXPECTED_AUD`, `scope` = subset needed for the skill.
17. JWT validation per `common/auth.jwt_validator`:
    - Required `aud`: **exact match** `https://hr.smart-employee.local/a2a` (no substring; multi-`aud` arrays must have every entry in an allowlist).
    - Required scope: per skill (e.g., `hr_approve_mcp` for `hr.approve_leave`).
    - **Nested `act` chain validation**: walk every level; every `sub` must be in `HR_TRUSTED_PEER_AGENTS` (env var; Sprint 1 allowlists `orchestrator-agent`). Reject if `act` is absent or empty (delegation chain required for this resource).
    - **Issuer is hardcoded** in the validator from configuration; the agent card's `auth.issuer` is never consulted at runtime.
18. Introspection feature flag `HR_INTROSPECT_ENABLED` (default `false`; flipped in Sprint 2).
19. **Application JSON-RPC error code catalog** (consistent across hr-agent and it-agent):
    - `-32600` invalid_request (incl. batch rejection)
    - `-32601` method_not_found
    - `-32602` invalid_params
    - `-32603` internal_error (no stack-trace leakage)
    - `-32001` invalid_audience
    - `-32002` insufficient_scope
    - `-32003` peer_not_trusted
    - `-32004` session_terminated
    - `-32005` token_expired
    - `-32006` token_revoked
    The `data` field is a **typed payload** restricted to `{required_scope?: str, available_scopes?: [str], code: str, correlation_id: str}` — NEVER raw exception strings or internal paths.
19a. Inbound requests carry an `X-Correlation-Id` header (UUIDv4). hr-agent forwards it into the downstream MCP call as metadata; logs it; echoes it back in JSON-RPC `data.correlation_id`. New histogram `a2a_to_mcp_dispatch_latency_seconds`.

**E — IT Asset Agent specialist (`it-agent/`)**
20. Scaffold mirroring `hr-agent/`. **Wraps `it-server/` via MCP (`langchain-mcp-adapters`)** for tool dispatch — does NOT hold asset data itself. Architecture is symmetric with HR Agent → hr-server.
21. Agent card published. Skills: `it.get_employee_assets`, `it.get_asset_by_id`. JWT validation requires exact `aud=https://it.smart-employee.local/a2a`, scope `it_assets_read_mcp`, nested `act` allowlist (`IT_TRUSTED_PEER_AGENTS=orchestrator-agent`).
22. **Hop 5 re-mint** before calling it-server: RFC 8693 token-exchange with `subject_token`=incoming `it_call_token`, `actor_token`=it-agent client_credentials access token, `resource`=it-server `EXPECTED_AUD` (verified by P14), `scope=it_assets_read_mcp`. Same caching + singleflight semantics as Hop 4.
23. Introspection feature flag `IT_INTROSPECT_ENABLED` (default `true` from day 1 — IT is greenfield so we adopt the Sprint 2 shape immediately).

**E2 — IT backend (`it-server/`)** — new in v3
23a. **Scaffold IDENTICAL to `hr-server/`** in shape and conventions — directory layout, FastAPI + FastMCP wiring, in-memory store pattern (mirror `hr-server/service/store.py`), config loading, error envelopes. The point of `it-server/` is symmetry with `hr-server/`; deviations would muddle the demo. In-memory data: 5–10 sample rows `{asset_id, employee_id, type, model, status}`. MCP tools: `get_employee_assets(employee_id, asset_category=None, limit=50, cursor=None)` returning `{"assets": [...], "total": int, "next_cursor": str|null}`; `get_asset_by_id(asset_id)`.
23b. JWT validation per `common/auth.jwt_validator`:
    - Required `aud`: exact `https://it-server.local/mcp` (or whatever the configured `EXPECTED_AUD` resolves to from P14).
    - Required scope: `it_assets_read_mcp`.
    - **Nested `act` chain check**: walk every level; allowlist for it-server is `IT_SERVER_TRUSTED_PEER_AGENTS=it-agent,orchestrator-agent` (chain depth 2: it-agent on top, orchestrator below).
    - Reject if `act` absent.
23c. `employee_id` is **server-derived** from the verified `sub` claim (or session-store mapping), not LLM-controlled. Pydantic validators reject mismatch.
23d. Introspection enabled from day 1 (Sprint 2 shape) behind flag `IT_SERVER_INTROSPECT_ENABLED=true`.
23e. Add to `docker-compose.yml` on port 8004; `/health` endpoint without auth (no version/build leak per security NIT).
23f. Cache-bust receiver `POST /internal/auth/cache-bust` per the §4.4 task 7 contract (HMAC + key_id + ±30s window).

**F — Trace / leak hygiene**
24. **Surgical** LangSmith redaction (not the nuclear `LANGSMITH_HIDE_INPUTS=true` switch): a `hide_inputs`/`hide_outputs` callable masks `Authorization`, `actor_token`, `subject_token`, `client_assertion`, anything matching `eyJ[A-Za-z0-9_-]{10,}\.`, any field whose key contains `token`. Tool args, messages, and outputs remain visible.
25. CI / pre-commit regex check on changed files for `Bearer\s` and `eyJ[A-Za-z0-9_-]{10,}\.`.
26. Identity context (non-secret only: `sub`, `sid`, `act` chain summary, `aud`) stashed in `RunnableConfig["metadata"]` for trace filtering.

**G — Tests — happy path**
27. `docker compose up`. Log in (consent screen visible per P11). Ask "approve leave LR001 — does John have outstanding equipment?" → orchestrator (a) calls HR with audience-narrowed token to fetch leave context, (b) calls IT with audience-narrowed token to fetch asset info, (c) composes answer.
28. Verify on Asgardeo audit log (or via introspection of saved tokens): two distinct exchanged tokens issued, one per resource, each with correct `aud` and scope.

**H — Tests — negative (DoD bar)**

Token-level (carry-over from v1, adapted to orchestrator → specialist hop):

29. **N1 — Bearer-forward rejected.** Orchestrator forwards the user delegated token directly (no exchange) to HR. Expected: 401, `error: invalid_audience` (delegated token has `aud=orchestrator-resource`, not `hr-agent-api`).
30. **N2 — Scope denial.** Mint exchanged token without `hr_approve_mcp`, attempt `approve_leave`. Expected: 403, `error: insufficient_scope`.
31. **N3 — Unknown actor.** Tamper exchanged token's `act.sub` to a non-allowlisted client_id. Expected: 403, `error: peer_not_trusted`.
32. **N4 — Expired exchanged token.** Mint with very short TTL, sleep past `exp`, replay. Expected: 401, `error: token_expired`.
33. **N5 — User scope absent.** User lacks `agent_access` (or specific specialist scope). Orchestrator must refuse without making any token-exchange call. Verified by Asgardeo log absence + intercept counter.
34. **N6 — Act-removal / chain-loss enforcement.** Test that the validator's primary defense is the **missing-`act` rule**, not signature failure. Mint a signature-valid token (e.g., via a misconfigured fallback path or a probe-test client_credentials grant) whose `act` claim is absent. Expected: 401, validator rejects with `error: invalid_token` citing missing actor chain — proving the validator enforces the chain even when the signature passes.
35. **N7 — Cross-aud replay.** HR-audience exchanged token replayed against IT specialist. Expected: 401, `error: invalid_audience`.

A2A-level (new in v2/v3):

36. **N8 — Spoofed agent card.** Stand up a malicious server claiming to be `it-agent` (different URL). Orchestrator's discovery is locked to a configured allowlist of agent-card URLs (env var `ORCHESTRATOR_AGENT_CARD_URLS`); cards from other origins are ignored before body parsing. Expected: malicious card not loaded.
37. **N8b — Card with mismatched `auth.issuer`.** A card from an allowlisted URL returns body with `auth.issuer` ≠ configured Asgardeo issuer. Expected: card rejected with logged warning; orchestrator does NOT use the body's issuer for any runtime decision.
38. **N9 — Missing agent card.** Specialist is down; card returns 404. Orchestrator's `discover_agents` reports the unavailable agent gracefully and the LLM cannot route to it. Expected: user-friendly fallback.
39. **N9b — Specialist runtime outage.** Card serves 200 but `message/send` returns 503 mid-conversation. Expected: orchestrator retries with exponential backoff (max 3 attempts), then returns structured error to the LLM (`{"error": "specialist_unavailable", "agent_id": ...}`).
40. **N10 — JSON-RPC malformed request.** Send `message/send` with missing/wrong-typed parameters. Expected: error code `-32602`; HTTP 200; `data.code = "invalid_params"`; no exception strings in `data`.
41. **N11 — Wrong JSON-RPC method.** Send `method: "tools/call"`. Expected: `-32601`.
42. **N11b — Batch JSON-RPC request.** Send `[{...}, {...}]` body. Expected: `-32600` (batch rejected for POC scope).
43. **N12 — User-consent denial.** User clicks "Deny" on Asgardeo's `requested_actor` consent screen. Expected: orchestrator surfaces a clean error to the SPA; no session created; no actor token issued.
44. **N14 — `requested_actor` policy denial.** Orchestrator login URL with `requested_actor=<actor not permitted by Asgardeo's application policy>`. Expected: Asgardeo's `/authorize` returns `invalid_request` (or similar); orchestrator surfaces clean error. Distinct from N12 (user denial).
45. **N15 — Hallucinated skill from LLM.** LLM emits a tool call for `skill_id="bogus.fly_to_moon"`. Expected: orchestrator's dispatcher returns `{"error": "unknown_skill", "available": [...]}`; LLM recovers; no specialist contacted.

Hop-4 / Hop-5 specialist-to-backend tests (new in v3 for symmetry):

45a. **N16 — Specialist forwards instead of re-mints.** HR Agent forwards the incoming `hr_call_token` (aud=hr-agent-api) directly to hr-server's MCP without re-minting. Expected: hr-server rejects with `invalid_audience` (-32001). Repeat the test for IT Agent → it-server path: same rejection at it-server. Both paths must fail; this is what proves the agent→backend re-mint is enforced, not optional.
45b. **N17 — Wrong backend audience.** Take a token correctly minted for hr-server (aud=`https://hr-server.local/mcp`) and replay it against it-server. Expected: it-server rejects with `invalid_audience`. Cross-backend replay must fail just as cross-specialist replay does (N7).

Identity-propagation:

46. **N13 — Token leakage check.** Inspect the full LangSmith run tree (root + all child runs, all messages, all error fields, agent-card content) for any string matching `Bearer\s` or `eyJ[A-Za-z0-9_-]{10,}\.`. **Also assert agent-card `url` and `auth` blocks are absent from trace** (surgical redactor strips them). Expected: zero matches.

### 3.5 Sprint 1 Definition of Done

- N1–N17 all pass (renumbering is intentional — v3 added N8b, N9b, N11b, N14, N15, N16, N17).
- Happy-path demo (§3.4-G) runs on `docker compose up`; consent screen shown at login.
- Sequence diagram checked in to `docs/diagrams/orchestrator-a2a-flow.md` (mermaid) — must show **all five hops** including hr-agent → hr-server (Hop 4) and it-agent → it-server (Hop 5) re-mints.
- LangSmith trace clean (N13).
- Asgardeo capability memo (Sprint 0, P1–P13) referenced by every task that depends on a verified probe.
- `docker-compose.yml` declares `deploy.replicas: 1` for orchestrator; assertion test in CI.

### 3.6 Sprint 1 fallbacks (only if Sprint 0 spike fails the relevant probe)

**Preferred POC posture:** if a probe fails, the first response is **fix the Asgardeo tenant configuration**, not implement a fallback. Fallbacks below exist for cases where the tenant *cannot* be reconfigured (Asgardeo limitation, not user error).

| Failure | Fallback |
|---|---|
| **P1 fail** (no `act` from RFC 8693) | Asgardeo claim-mapping policy adds `x_actor_client_id` custom claim. Validators check this instead of `act.sub`. |
| **P2 fail** (no `aud` narrowing on token-exchange) | Asgardeo policy emits `aud` as a custom claim per requested resource. Do NOT fall back to scope-only. |
| **P10 fail** (no `requested_actor` support) | Use RFC 8693 token-exchange for the **first hop** as well (orchestrator post-login mints a delegated-with-act token from the user's plain auth code's token). Loses the user-consent-at-login UX but preserves the chain. Document the deviation. |
| **P11 fail** (no consent screen for `requested_actor`) | Display an in-orchestrator consent step before any specialist call ("you are about to delegate to <orchestrator>"). Weaker than IdP-rendered consent; document the tradeoff. |
| **P12 fail** (no chained-actor preservation across exchanges) | Skip the stretch HR→IT specialist-to-specialist demo. Hop 4 (hr-agent → hr-server) still works because chain depth there is 2 — verify in P12 specifically that depth-2 is supported even if depth-3 isn't. |
| **P13 fail** (hr-server rejects the hr-agent-aud token AND hr-server cannot be reconfigured) | Reconfigure hr-server's `EXPECTED_AUD` to accept the hr-agent-aud (still permissive only to the exact value), OR add a thin shim to hr-server that re-introspects each token. Re-mint (Hop 4) is the preferred path; fallbacks documented in spike memo. |
| **P1 + P2 both fail** | Use **RFC 7523 JWT-bearer**: orchestrator signs an assertion `{sub: user, act: {sub: orchestrator}, aud: <specialist>}` and presents as `client_assertion` to mint a target-aud access token. **Client-credentials-only fallback is rejected** — loses delegation chain. |
| **P1 + P10 both fail** | Cascade into the RFC 7523 path described above for ALL hops (no `requested_actor` UX, no RFC 8693). Heaviest fallback; document explicitly. Likely indicates the Asgardeo tenant is not configured for agent identities — may justify pausing for support engagement. |
| **P3 fail** (no revocation cascade) | Acceptable. Sprint 2's Layer A (introspection) is the primary mechanism. |
| **P4 fail** (no BCL) | Sprint 2 leans entirely on Layer A. Document limitation. |
| **P7 fail** (no `sid` in access tokens) | Layer-B correlator is `sid` stored in session record at login (read from ID token). |

### 3.7 Sprint 1 risks

- **Resource URI versioning.** Canonical URIs `https://{hr,it}.smart-employee.local/a2a` are baked into every exchanged token's `aud`. **Decision: unversioned for POC.** Move/rename = breaking change. Production reserves a `/v1/` prefix.
- **`HR_TRUSTED_PEER_AGENTS` env var allowlist scales O(N²)** with agent count. POC accepts this; production must move trust to centralized Asgardeo policy. Documented as scaling cliff.
- **A2A protocol version drift.** v0.3 of the spec is current; we hand-roll. If we later adopt the SDK, agent-card schema must align — see §2.5 for the v2 schema we ship today.
- **Specialist→specialist chain (HR→IT)** is *stretch*, not DoD. If P12 fails, drop the stretch entirely.

---

## 4. Sprint 2 — Secure Session Termination

### 4.1 Goal

When a user logs out (or their Asgardeo session is admin-terminated), every downstream agent token derived from that session is rejected within **≤ 5 s** at every specialist. Negative tests prove (a) stale exchanged tokens fail, (b) admin termination triggers BCL, (c) the BCL endpoint cannot be weaponized, (d) streaming chats terminate cleanly mid-flight.

### 4.2 Mechanism — two layers

**Layer A — Per-request introspection with BCL-driven cache-bust** (specialists: hr-agent, it-agent).
- Each specialist calls Asgardeo `/oauth2/introspect` for every A2A `message/send` after JWKS signature/`exp`/`aud` checks pass.
- Cache TTL = **2 s** positive. On BCL receipt at the orchestrator, the orchestrator's cache-bust dispatcher posts to each known specialist's `POST /internal/auth/cache-bust`.
- **Cache-bust targets are derived from the orchestrator's loaded agent-card set** (URL convention: `<a2a_origin>/internal/auth/cache-bust`). Adding a new specialist = add to `ORCHESTRATOR_AGENT_CARD_URLS`; cache-bust fan-out updates automatically. No hardcoded specialist URLs in the dispatcher.
- Worst-case stale window without BCL = 2 s; with BCL = sub-second after delivery.

**Layer B — Back-channel logout at orchestrator** (the user-facing service).
- Orchestrator registers `POST /auth/backchannel-logout` with Asgardeo as the `backchannel_logout_uri`.
- Content-type `application/x-www-form-urlencoded`; single field `logout_token` (per OIDC BCL 1.0 §2.8).
- On valid logout token: `sessions.terminate_by_sid(sid)` → cache-bust dispatcher fires to hr-agent + it-agent → best-effort `/oauth2/revoke` for the user's OBO refresh token.
- Endpoint terminates **only the `sid` in the validated token** — never any session inferred from caller's network/cookie identity.

### 4.3 Components touched

| File | Change |
|---|---|
| `orchestrator/main.py` | Add `POST /auth/backchannel-logout` (form-urlencoded, OIDC BCL 1.0 compliant). Modify existing `POST /api/logout` to call `/oauth2/revoke` (fire-and-forget, 1-s timeout, response unchanged). Add cache-bust dispatcher to known specialists. |
| `orchestrator/session.py` | Add `terminated_sids: set[str]` and `terminate_by_sid()`. Store `sid` from ID token at login. |
| `orchestrator/asgardeo_logout_token.py` (new) | OIDC BCL 1.0 logout-token validator. |
| `hr-agent/auth/`, `it-agent/auth/` | Switch validator to JWKS-then-introspect chain. Add `POST /internal/auth/cache-bust` endpoint (HMAC-protected). HR feature-flagged `HR_INTROSPECT_ENABLED=true` in this sprint; IT was already on. |

### 4.4 Task list

**A — Asgardeo configuration**
1. Configure orchestrator's `backchannel_logout_uri`. Confirm BCL fires on user-initiated AND admin-terminated session.

**B — Logout-token validator**
2. Implement `orchestrator/asgardeo_logout_token.py`. **POC-trimmed OIDC BCL 1.0 §2.6 checklist** (full strict checklist deferred to production-hardening):
   - `iss` = expected Asgardeo issuer.
   - `aud` = orchestrator's `client_id`.
   - `exp` not yet passed.
   - **`sub` OR `sid` present** (at least one — required by spec).
   - `events` claim contains the exact key `http://schemas.openid.net/event/backchannel-logout` with empty object value.
   - `nonce` MUST NOT be present.
   - Signature verified against Asgardeo JWKS.
   - Single-use: `jti` cache; reject if seen; cache for `exp`.
   - **POC tolerances** (warn-not-reject; tighten in production):
     - `iat` skew check (redundant with `exp` for POC).
     - `typ: logout+jwt` header (SHOULD per spec; Asgardeo may emit without it).
     - Algorithm pinning to registered `id_token_signed_response_alg` (Asgardeo signs with one alg per app; mismatch is unlikely outside attack scenarios).

**C — Orchestrator endpoints**
3. `POST /auth/backchannel-logout`:
   - Accept `application/x-www-form-urlencoded`, parse `logout_token`.
   - Validate via #2 above. On valid: `terminate_by_sid` → cache-bust dispatcher → best-effort `/oauth2/revoke`.
   - Response: 200 empty body on success; 400 with `{"error": "invalid_request"|"invalid_logout_token"}` per spec; 501 if BCL is disabled.
4. Modify `POST /api/logout` to also call `/oauth2/revoke`. Response contract preserved (`{"success": true, "message": "Session cleared."}`) regardless of revoke outcome. 1-s timeout.
5. Modify chat endpoint to short-circuit if session's `sid` ∈ `terminated_sids` (return 401, `error: session_terminated`).
6. **Streaming + BCL race handling:** for each active streaming chat, register an `asyncio.Event` keyed on `sid`. `terminate_by_sid()` calls `event.set()`. The streaming handler races `astream_events` against `event.wait()` via `asyncio.wait(..., return_when=FIRST_COMPLETED)`. On event-set, the generation task is cancelled before any further A2A dispatch.
7. **Cache-bust dispatcher** (`orchestrator/cache_bust.py`) — **simplified for POC** (per v3 review):
    - **Targets:** derived from loaded agent-card URLs (`<a2a_origin>/internal/auth/cache-bust` by convention). NOT hardcoded.
    - **Request:** `POST /internal/auth/cache-bust`, `Content-Type: application/json`, body `{"sid": str, "sub": str, "ts": int (Unix epoch)}`. Header: `X-Cache-Bust-Sig: <HMAC-SHA256-hex>` (over canonicalized body).
    - **Auth:** shared HMAC secret in env var; `±5 min` timestamp window. Internal call between containers on the compose network — POC threat model is "don't accept anonymous POSTs," not "resist replay-from-network-capture."
    - **Response:** `204 No Content` on eviction; `400` on expired timestamp; `401` on bad/missing HMAC; `404` if no matching cached entries (treated by dispatcher as success — idempotent).
    - **Best-effort** with 1-s timeout from dispatcher; specialists' 2-s introspection cache TTL is the safety net.
    - Counter `cache_bust_dispatched_total{target=<agent_id>, outcome=...}`; failure mode never blocks BCL response.
    - **Production hardening (deferred to `docs/production-hardening.md`):** HMAC key-id with two-key overlap rotation; nonce cache with smaller window for replay protection; mTLS as a stronger alternative.

**D — Specialist introspection**
8. Implement `common/auth/introspector.py` maturity: 2-s positive cache, BCL cache-bust hook (`/internal/auth/cache-bust`).
9. Wire HR + IT validator chains: JWKS check → introspection (cache-respecting) → reject on `active: false`.
10. Behavior under introspect outage (decision: **fail-closed for unverified tokens**): if no cached `active: true` and introspect unreachable, return 503 `error: introspection_unavailable`. Cached entries continue to serve until TTL expires. Documented + tested.

**E — Observability**
11. Counters: `token_exchange_calls_total`, `token_exchange_cache_hits_total`, `introspect_calls_total`, `introspect_cache_hits_total`, `bcl_tokens_received_total`, `bcl_tokens_rejected_total{reason=...}`, `cache_bust_dispatched_total`, `cache_bust_failed_total`.
12. Histograms: `token_exchange_latency_seconds`, `introspect_latency_seconds`, `bcl_to_eviction_latency_seconds` (e2e revocation latency from BCL receipt to specialist cache eviction).

**F — Tests — happy path**
13. Logged-in user logs out → next chat call gets 401 → previously-issued HR + IT tokens replayed against their respective specialists also get 401 within 5 s.

**G — Tests — negative (DoD bar)**

14. **R1 — Stale HR-call token replayed at HR agent.** Save valid token pre-logout; replay post-logout. Expected: 401 within 5 s.
15. **R2 — Stale IT-call token replayed at IT agent.** Same. Expected: 401 within 5 s.
16. **R3 — Admin-terminated session.** Operator terminates session in Asgardeo console; within 5 s, orchestrator's `/auth/backchannel-logout` fires; subsequent chat gets 401; subsequent specialist replays get 401.
17. **R4 — Logout-token replay.** Replay a previously-seen valid logout token. Expected: 400 `invalid_logout_token` (jti cache hit).
18. **R5 — Forged logout token.** POST a logout token signed by an unrelated key. Expected: 400 `invalid_logout_token`.
19. **R6 — Streaming + logout race.** Chat stream in flight when user logs out. Expected: stream cancelled; no A2A `message/send` call after `terminate_by_sid`; no specialist `ToolMessage` produced post-cancellation.
20. **R7 — DoS / hijack via BCL endpoint.** While authenticated as User-B, POST a valid logout token for User-A's `sid`. Expected: User-A's `sid` is terminated (legitimate per spec); User-B's session unaffected (caller's network/cookie identity must NOT influence which sid gets terminated).
21. **R8 — Wrong-aud logout token.** Asgardeo-signed logout token whose `aud` is a different application. Expected: 400 `invalid_logout_token`.
22. **R9 — Concurrent logout + non-streaming chat.** User submits chat, simultaneously logs out. Expected: chat completes before terminate, OR returns 401 mid-flight. Never partial tool execution after revoke.
23. **R10 — Cache-bust replay (timestamp-window).** Replay a valid cache-bust message after the ±5 min window. Expected: 400, timestamp outside window.
24. **R11 — Cache-bust forgery.** Attacker without the HMAC secret sends cache-bust. Expected: 401, missing/invalid `X-Cache-Bust-Sig`.
25. **R6 enhancement — stream cancellation propagates to HTTP.** Verify in R6 that no `POST /a2a` HTTP request reaches the specialist after `event.set()` fires. Use a specialist-side request log to assert zero post-cancellation calls (not just zero `ToolMessage` results). Requires the orchestrator's A2A client to be cancel-aware (httpx `AsyncClient` with explicit `task.cancel()`).
26. **R12 — Joint failure: introspect down + cache-bust failed.** Kill introspect endpoint. Send a BCL token (cache-bust dispatch fails because target unreachable). Replay a token within the 2-s pre-eviction window. Expected: 503 `introspection_unavailable` (fail-closed per §4.4 task 10) — NOT 200 with stale token. Proves the fail-closed policy holds when both Layer A's external dependency and Layer B's push channel both degrade.
27. **R13 — Post-BCL chat session invalidation.** After BCL fires for User-A, send a new `/api/chat` request from User-A's still-valid orchestrator cookie. Expected: 401 `session_terminated` — proves the orchestrator-side session is invalidated by BCL, not just downstream tokens (otherwise the chat UI looks alive while specialists fail).

### 4.5 Sprint 2 Definition of Done

- R1–R13 all pass (incl. R12 joint-failure and R13 orchestrator-session invalidation).
- Maximum observed end-to-end revocation latency (BCL receipt → specialist cache eviction → next request rejected) < 5 s. Recorded in test output via `bcl_to_eviction_latency_seconds`. (Single-user POC; "P95" would be meaningless without load.)
- Both Layer A and Layer B demonstrably trigger.
- HR introspection feature flag flipped to `true`; existing scenarios still pass with introspect endpoint reachable AND a regression test with introspect endpoint unreachable (fail-closed verified).
- Logout flow updated in `docs/diagrams/logout-flow.md`.

### 4.6 Sprint 2 risks

- **`sid` not in access tokens (P7).** Mitigated by storing `sid` from ID token in session record at login.
- **BCL retry budget.** Mitigated by Layer A always running.
- **Cache-bust dispatcher reliability.** If cache-bust fails, 2-s introspection cache TTL closes the gap — worst case 2 s stale window, still inside 5-s budget.
- **HMAC key distribution.** Key shared via env var across orchestrator and specialists. Rotation unscoped for POC; production must use a secret manager.

---

## 5. Cross-cutting concerns

### 5.1 Identity propagation (LangChain runtime)
- Bearer tokens flow only through `RunnableConfig["configurable"]`. Use `Annotated[..., InjectedToolArg]` for any parameter that must be hidden from the model.
- Identity context in `RunnableConfig["metadata"]` (non-secret only): `sub`, `sid`, `act` chain summary, `aud`. Used for trace filtering by user/actor without exposing bearers.
- LangSmith redaction is **surgical** (callable `hide_inputs`/`hide_outputs`). Tool args, messages, outputs remain visible.
- N13 covers full-tree leak verification (root + all child runs, inputs/outputs/error/extra.metadata).
- CI / pre-commit regex check on changed files for `Bearer\s` and JWT body regex.

### 5.2 Threat model

| Threat | Sprint 1 mitigation | Sprint 2 mitigation |
|---|---|---|
| Confused deputy at orchestrator → specialist | Per-target audience-narrowed exchange + nested `act` allowlist | n/a |
| Bearer forwarding (no exchange) | N1 explicitly tests rejection | n/a |
| Cross-aud replay | N7 tests rejection | n/a |
| Token replay after revocation | n/a | Introspection cache ≤ 2 s + BCL cache-bust |
| Cross-agent privilege escalation | Per-specialist scopes + audience-narrowing (RFC 8707) | n/a |
| Spoofed agent card (rogue specialist) | N8 — orchestrator URL allowlist; agent card is *not* a trust anchor | n/a |
| **Card `auth.issuer` JWKS redirection** | N8b — issuer hardcoded in validator; card body's `auth.issuer` is advisory; mismatch logged + card refused | n/a |
| **Token-exchange `resource` redirection via card body** | `resource` derived from allowlisted URL, not card body; verified in §3.4 task 10 | n/a |
| Missing / unreachable specialist | N9 / N9b — graceful fallback + retry+backoff | n/a |
| User-consent bypass (user denial) | N12 — IdP-rendered consent for `requested_actor` (P11 verified) | n/a |
| `requested_actor` policy abuse (asking for non-permitted actor) | N14 — Asgardeo's app policy rejects at `/authorize` | n/a |
| Hallucinated skill from LLM | N15 — opaque `agent_id` enum + structured error response | continued |
| Cross-agent skill-id collision | Namespaced skill IDs (`hr.*`, `it.*`) — collisions impossible by construction | continued |
| JSON-RPC info disclosure via error data | N10/N11 — `data` field is typed payload; no exception strings | continued |
| JSON-RPC batch amplification | N11b — batch requests rejected with `-32600` | continued |
| Token leakage to LLM | `InjectedToolArg` + `RunnableConfig` | continued |
| Token leakage to traces / logs | Surgical redaction + run-tree scan + CI regex | continued |
| BCL-token forgery | n/a | Sig + iss + aud + exp + iat + events + alg |
| BCL-token replay | n/a | `jti` single-use cache (TTL = `exp`) |
| BCL-driven session hijack | n/a | Endpoint terminates only the `sid` in the validated token (R7) |
| Wrong-aud BCL token | n/a | `aud` exact-match (R8) |
| Streaming/logout race | n/a | `asyncio.Event` per stream, cancellation before A2A dispatch |
| Cache-bust forgery / replay | n/a | HMAC + timestamp window (R10, R11) |

### 5.3 Single-replica POC constraint (explicit + enforced)
- `docker-compose.yml` declares `deploy.replicas: 1` for orchestrator (and for now, hr-agent / it-agent). CI assertion verifies the constraint so R7 and BCL tests are deterministic.
- `terminated_sids` is per-replica → BCL hitting one orchestrator replica doesn't terminate sessions on another.
- Introspection cache is per-replica → 2× introspect QPS at 2 replicas (acceptable, but not in POC scope).
- `jti` single-use cache for logout-token replay is per-replica → replay against another replica succeeds.
- Cache-bust nonce cache is per-specialist-replica → replay against another specialist replica succeeds.
- SPA → orchestrator traffic must be sticky-session-routed under HA, OR session state moved to Redis.
- Cache-bust dispatcher is one-to-one with specialist replicas; HA needs a fan-out broadcaster (or shared cache state).

A `docs/production-hardening.md` checklist is created at end of Sprint 2 enumerating these.

### 5.4 Configuration-driven agent registration
With agent cards in v2, adding a third specialist becomes:
- Register agent in Asgardeo + create API resource.
- Deploy new specialist with agent card.
- Add the specialist's URL to orchestrator's `ORCHESTRATOR_AGENT_CARD_URLS` env var.
- *No code change in orchestrator.*

System-prompt assembly remains hardcoded for Sprint 1; making it card-derived (e.g., LLM is told skill descriptions from cards) is deferred to a future iteration.

---

## 6. Out of scope (confirmed)

| Item | Why deferred | Re-entry trigger |
|---|---|---|
| UAE Pass federation | Requires Asgardeo connection setup; transparent to code. | When demo audience requires UAE Pass. |
| WSO2 API Manager / AI Gateway | Heavy infra; not on critical path. | When PII / PI guardrails or rate limiting are scoped. |
| PII masking | Needs guardrail framework. | Future "guardrails" sprint. |
| Prompt-injection detection | Same. | Future "guardrails" sprint. |
| Multi-replica deployment | Single-replica POC. | Production hardening sprint. |
| Cryptographically-verifiable actor chains (`draft-mw-spice-actor-chain`) | Spec is early-draft. | Watch IETF status. |
| Distributed introspection / revocation cache | Single-replica POC. | Production hardening sprint. |
| Cross-domain / cross-org orchestration | Article calls this out as "single domain limitation"; same applies here. | Multi-org sprint. |
| Specialist→Specialist HR→IT call (full chained delegation) | Stretch in Sprint 1; depends on P12. | If P12 passes and time allows; otherwise v3. |

---

## 7. Milestones & gates

| Milestone | Gate | Owner |
|---|---|---|
| **M0 — Sprint 0 done** | Spike memo (**P1, P2, P3, P4, P5, P7, P8, P10, P11, P12, P13, P14** — 12 probes; P6 and P9 deferred per v3 POC review) committed; `common/auth/` + `common/a2a/` lands; scope policy + agent-card schema + UX validation spec + Asgardeo setup guide committed; library pins verified; service stubs build (incl. it-server); `agent/` archived to `_archive/agent.before-v3/` and tagged `pre-v3-orchestrator`. | Lead engineer + security-engineer sign-off. |
| **M1 — Sprint 1 done** | **N1–N17** pass (incl. N8b, N9b, N11b, N14, N15, N16, N17); orchestrator-coordinated demo runs; consent screen visible at login; sequence diagram (5 hops, both specialist→backend re-mints) committed; LangSmith trace clean (incl. agent-card `url`/`auth` stripped); `replicas: 1` constraint asserted in CI. | Lead engineer + architect-reviewer + api-designer sign-off. |
| **M2 — Sprint 2 done** | **R1–R13 (incl. R6 enhancement, R12 joint-failure, R13 session invalidation)** pass; max observed revocation latency recorded; both layers proven; HR introspection flag ON; production-hardening checklist drafted; cache-bust contract documented in `docs/jsonrpc-contract.md`. | Lead engineer + security-engineer sign-off. |
| **M3 — POC doc rewrite** | Original POC doc updated to match implementation. Architecture diagram updated to show orchestrator + specialists. | Technical writer + product manager. |

---

## 8. Tracked NITs (defer-and-revisit)

- Centralize peer trust in Asgardeo policy instead of `*_TRUSTED_PEER_AGENTS` env var. (architect)
- Add SLO/alert on introspection error rate. (architect)
- `/health` no-auth must not leak version/build info. (security)
- Resource URI versioning: POC unversioned; production reserves `/v1/`. (api)
- Agent-card-derived system prompt assembly (config-driven). (architect)
- **Card text sanitization for prompt assembly** — once card-derived prompts are introduced, rogue card text becomes a prompt-injection vector. (architect)
- Signed agent cards (defense beyond URL allowlisting). (security)
- A2A SDK adoption (vs hand-rolled JSON-RPC) once schema stabilizes — `schema_version` field eases migration. (api)
- `message/stream`, `tasks/get`, `tasks/cancel` JSON-RPC methods explicitly **out of scope for the POC**; LLM composes synchronously. (api)
- mTLS for `/internal/auth/cache-bust` in production (HMAC + key_id is the POC shape). (security)
- HMAC key rotation procedure documented (overlap two keys via `X-Cache-Bust-Key-Id`). (security)
- JSON Schema for `message/send` body in `docs/jsonrpc-contract.md`. (api)
- Strict-mode flag for `typ: logout+jwt` header (default warn for Asgardeo compatibility). (security)

---

## 9. References

### Standards
- OAuth 2.1 (current draft): https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-15
- RFC 8693 — Token Exchange: https://datatracker.ietf.org/doc/html/rfc8693
- RFC 8707 — Resource Indicators: https://datatracker.ietf.org/doc/html/rfc8707
- RFC 7009 — Token Revocation: https://datatracker.ietf.org/doc/html/rfc7009
- RFC 7662 — Token Introspection: https://datatracker.ietf.org/doc/html/rfc7662
- RFC 7523 — JWT-bearer assertions: https://datatracker.ietf.org/doc/html/rfc7523
- OIDC Back-Channel Logout 1.0: https://openid.net/specs/openid-connect-backchannel-1_0.html
- A2A Protocol Specification: https://a2a-protocol.org/latest/specification/

### Asgardeo
- Configure Token Exchange: https://wso2.com/asgardeo/docs/guides/authentication/configure-token-exchange/
- Back-Channel Logout: https://wso2.com/asgardeo/docs/guides/authentication/oidc/add-back-channel-logout/
- Revoke Tokens: https://wso2.com/asgardeo/docs/guides/authentication/oidc/revoke-tokens/
- Agent Quickstart (Python): https://wso2.com/asgardeo/docs/quick-starts/agent-auth-py/
- Secure Agentic AI Tutorial: https://wso2.com/asgardeo/docs/tutorials/secure-agentic-ai-systems/

### LangChain / MCP
- `langchain-mcp-adapters` header-callable PR: https://github.com/langchain-ai/langchain-mcp-adapters/pull/313
- `InjectedToolArg`: https://python.langchain.com/api_reference/core/tools/langchain_core.tools.base.InjectedToolArg.html
- Custom auth in LangGraph: https://blog.langchain.com/custom-authentication-and-access-control-in-langgraph/

### Article that informed v2
- Binula Dimantha T., *From Delegation to Action: Building Secure Multi-Agent Systems with A2A Protocol and Asgardeo*, 2025-12-16: https://www.linkedin.com/pulse/from-delegation-action-building-secure-multi-agent-a2a-thilakasiri-zckhc/
- Companion repo: https://github.com/Bin4yi/Secure-Multi-Agent-Systems-with-A2A-Protocol-and-Asgardeo

### POC artefacts
- POC document: [Proof of Concept (POC)_ Identity-First AI Agent Governance.md](Proof%20of%20Concept%20(POC)_%20Identity-First%20AI%20Agent%20Governance.md)
- **User-experience validation spec:** [user-experience.md](user-experience.md) — what the user sees + acceptance criteria, mapped to N/R-tests. Source of truth for "the UX works."
- **Asgardeo configuration guide** (Sprint 0 deliverable, §2.9): `docs/asgardeo-setup.md`
- Scope policy (Sprint 0 output): `docs/scope-policy.md`
- Agent-card schema (Sprint 0 output): `docs/agent-card-schema.md`
- Spike memo (Sprint 0 output): `docs/spikes/asgardeo-capability-memo.md`
- Production hardening checklist (Sprint 2 output): `docs/production-hardening.md`

---

**v2 supersedes v1. Changes require re-review by all four verifying agents and re-sign-off at the affected milestone gate.**
