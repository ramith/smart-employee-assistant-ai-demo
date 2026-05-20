# Sprint 1 — Technical Architecture (integrating doc)

**Stage:** 4 close-out
**Status:** ✅ **Stage 5 council review complete (4 agents, all GO-with-conditions).** All blockers resolved in [`sprint-1-fixes.md`](sprint-1-fixes.md) — F-01 through F-16. Implementing agents MUST read `sprint-1-fixes.md` before writing code; it overrides the source docs where conflicts exist.
**Read order:** `sprint-1.md` (this) → [`sprint-1-fixes.md`](sprint-1-fixes.md) (binding addendum) → [`api-contracts.md`](api-contracts.md) + [`module-layout.md`](module-layout.md) + [`sequence-diagrams.md`](sequence-diagrams.md) (detail).

---

## 1. What got produced

| Artifact | Lines | Purpose |
|---|---|---|
| `api-contracts.md` | ~880 | Pydantic v2 / TypedDict / dataclass definitions for: orchestrator REST, SSE events, A2A JSON-RPC, MCP tools, IS consumer endpoints, internal data models |
| `module-layout.md` | 1201 | 34 modules across 5 services + shared `common/`, full public-surface signatures, reuse pointers to `_archive/agent.before-v3/`, dependency graph |
| `sequence-diagrams.md` | 492 | 8 Mermaid sequence diagrams (UC-01..06 + actor-token sub-flow + UC-04 denial variant) |

Combined, these three docs define everything an implementing agent needs to build a module without coordinating with other agents — the basis for parallel Stage 6.

---

## 2. Naming alignment (resolved inconsistencies)

The three docs were written by three independent agents in parallel. They agree on most names; below are the divergences and the **locked canonical choice**:

### SSE event vocabulary

| Canonical name | Alternate seen | Resolution |
|---|---|---|
| `routing` | (consistent) | ✓ |
| `ciba_url` | (consistent) | ✓ |
| `ciba_state_change` | `widget_state` (sequence-diagrams.md) | **Use `ciba_state_change`** — matches typed Pydantic model in api-contracts.md. Patch sequence-diagrams.md to follow. |
| `chat_message` | (consistent) | ✓ |
| `error` | (consistent) | ✓ |
| `partial_result` | only in sequence-diagrams.md | **DROPPED** — partial results are delivered as `chat_message` with the explanation copy from `copy-deck.md` §7 (e.g., "I have leave info but couldn't reach IT"). No new event type needed. |
| `session_ready` | only in sequence-diagrams.md | **ADOPTED** — emitted once on `/events/{session_id}` connect; carries user display name, connection status. Add to api-contracts.md. |

**Final SSE event type set (6):** `session_ready`, `routing`, `ciba_url`, `ciba_state_change`, `chat_message`, `error`.

### Endpoint paths
All consistent across the three docs. No conflicts.

### Class names
All consistent. The `module-layout.md` paths shown in the consistency check (e.g., `/auth/actor_token_provider`) are file paths within `common/auth/`, not URL routes — no conflict.

---

## 3. The 8-wave implementation plan (locked for Stage 6)

From `module-layout.md`'s dependency graph. Each wave is a parallel-safe batch; the next wave starts after the previous merges.

### Wave 1 — no deps (6 modules, all parallel)
- `common/auth/models.py` — OAuthToken, OBOToken, JWTClaims dataclasses
- `common/auth/errors.py` — exception hierarchy
- `common/a2a/jsonrpc.py` — JSON-RPC 2.0 envelope helpers
- `common/a2a/models.py` — Pydantic shapes
- `common/logging/correlation.py` — `X-Request-ID` middleware
- `common/logging/redaction.py` — JWT/token regex stripper

### Wave 2 — depends on W1 (4 modules, all parallel)
- `common/auth/peer_trust.py` — depth-1 act validation (already exists; extend per Sprint 1 changes)
- `common/auth/jwt_validator.py` — JWKS + signature + claim validator (port from `_archive/agent.before-v3/main.py:106-180`)
- `common/auth/wso2_is_client.py` — async helpers for /authorize, /authn, /token
- `common/agent_card.py` — agent card schema + URL allowlist

### Wave 3 — depends on W1+W2 (4 modules, all parallel)
- `common/auth/actor_token_provider.py` — port from `_archive/.../agent_auth.py`
- `common/auth/ciba_client.py` — initiate + poll + acquire_obo, with full error class hierarchy
- `common/a2a/client.py` — async A2A client for orchestrator → specialist
- `common/a2a/server.py` — FastAPI router factory for specialists

### Wave 4 — configs (5 modules, all parallel)
- `orchestrator/config.py`, `hr_agent/config.py`, `it_agent/config.py`, `hr_server/config.py`, `it_server/config.py`

### Wave 5 — service internals (8 modules, all parallel — the widest fan-out)
- `orchestrator/auth/session_store.py`
- `orchestrator/auth/pattern_c.py`
- `orchestrator/events/sse.py`
- `orchestrator/chat/keyword_fallback.py`
- `hr_server/auth/validators.py`
- `it_server/auth/validators.py`
- (placeholder for additional internal modules per `module-layout.md`)

### Wave 6 — service orchestration (4 modules, all parallel)
- `hr_agent/ciba/orchestrator.py`
- `it_agent/ciba/orchestrator.py`
- `hr_server/mcp/tools.py`
- `it_server/mcp/tools.py`

### Wave 7 — route handlers (5 modules, all parallel)
- `orchestrator/auth/routes.py`
- `orchestrator/chat/routes.py`
- `orchestrator/events/sse_router.py`
- `hr_agent/a2a/handler.py`
- `it_agent/a2a/handler.py`

### Wave 8 — wiring (5 main.py files, all parallel)
- `orchestrator/main.py`, `hr_agent/main.py`, `it_agent/main.py`, `hr_server/main.py`, `it_server/main.py`

### + Cross-cutting (independent, can run anytime after W4)
- SPA Consent Widget (`client/`) — JS, can be built in parallel with the Python waves
- End-to-end demo script (`scripts/demo.sh` or similar)

**Estimated parallelism savings vs sequential:** ~50% wall-clock for Stage 6.

---

## 4. Implementation contracts that MUST hold (or Stage 6 falls apart)

These are the highest-risk integration points. The agents writing modules around these contracts must NOT diverge:

1. **`OBOToken` shape** (`common/auth/models.py`) — wraps `OAuthToken` + decoded claims. Used by every specialist's CIBA acquisition + every MCP server's validator. Single canonical class.
2. **A2A response discriminated union** — `consent_required | result | error` per `api-contracts.md` §3. The orchestrator's chat loop and the specialist's CIBA orchestrator MUST agree byte-for-byte.
3. **`Session.cached_obo` cache key** — `tuple[agent_id: str, scope_frozenset: frozenset[str]]`. Both the cache writer (specialist returning a token) and the cache reader (next request to same agent) must use identical key construction.
4. **`X-Request-ID` propagation** — every HTTP boundary (SPA→orch, orch→specialist, specialist→IS, specialist→MCP) preserves and logs the same `X-Request-ID`. Wave 1's `correlation.py` middleware enforces this.
5. **MCP token validation policy** — exact 6-step check (sig, iss, exp, aud, act.sub, scope) per `api-contracts.md` §4. hr_server and it_server SHARE this validator (in `common/auth/`); they don't write their own.

If Stage 5 council finds anything ambiguous in 1–5, it's a blocker.

---

## 5. Open items for Stage 5 review

These are the questions I'd raise myself; the council will surface more.

1. **Token-B handoff to orchestrator's session map** — UC-02 design notes proposed three options (callback, in-process map, embed-in-A2A-response). `module-layout.md` settled on **A2A response carries `IssuedTokenRecord`** which orchestrator's `chat/routes.py` writes to `Session.cached_obo` after success. Confirm this is the right place to write — alternative: specialist writes via a shared in-process map (only works because of single-process Q5).

2. **Where does the LLM tool-routing run?** `module-layout.md` puts `chat/llm.py` in the orchestrator. But the LLM's TOOL CALLS are A2A invocations to specialists — does the LLM see the tool *as the A2A method* or as a higher-level "ask HR for leave balance"? Pick one and lock; the keyword-fallback must mirror.

3. **Two-specialist serial coordination (UC-03)** — who decides "wait for HR's reply before starting IT's CIBA"? Probably the LLM (it sees the tool-call sequence). If keyword fallback is in play, who orchestrates the sequence? Lock in `chat/routes.py`.

4. **Cookie security for dev** — `Secure HttpOnly SameSite=Lax` is right for prod. For dev with self-signed cert, `Secure` cookies get rejected unless using HTTPS. Add a `DEV_COOKIE_SECURE=false` env override for the demo.

5. **State-diagram naming patch** — `sequence-diagrams.md` needs `widget_state` → `ciba_state_change` find-and-replace (1 edit). Tracked.

---

## 6. What Stage 5 reviewers should check

- Is every UC's main flow buildable directly from these docs without ambiguity?
- Are interfaces concrete enough that 6 implementing agents working in parallel won't diverge on the contracts in §4?
- Are there any threats from `milestone-plan.md` §4 not addressed in the architecture?
- Is the wave plan's parallelism realistic, or are there hidden synchronization needs?
- Will the SPA's `EventSource` handle the SSE event vocabulary cleanly?

Stage 5 is the last gate before code is written.
