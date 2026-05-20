# Sprint 5 — LLM-driven chat orchestration (M5)

**Stage 3 binding plan.** This document is authoritative for S5 scope, the security invariant, and the build slices. Upstream Stage 1/2 docs ([`sprint-5-stage-1-product-review.md`](sprint-5-stage-1-product-review.md), [`UC-17`](../use-cases/UC-17-llm-routed-chat.md)) are narrative; where they disagree with this file, this file wins.

**Date:** 2026-05-11
**Branch:** `sprint-5-build` (cut from `sprint-4-build`)
**Predecessor state:** M4 build complete; Sprint-4 post-build fixes landed (self-service-by-`sub`, leave-balance reply shape, `hr.read_policy` chat tool). 1002 tests / 60 files strict-green.

---

## 1. One-paragraph statement

Turn the orchestrator's chat from a keyword router into an **LLM-driven router + reply composer** (OpenAI `gpt-4.1` via `langchain-openai`, reached through the WSO2 AMP AI Gateway), keep the keyword router as the automatic fallback when OpenAI / the AMP gateway is unavailable, and add the one missing piece that makes natural-language "apply for leave" actually work: an `apply_leave` chat tool (`hr_server` MCP route + HR-Agent dispatcher entry + CIBA scope wiring). The A2A → per-agent-CIBA → MCP fan-out is unchanged; the LLM influences only *which* tools are selected and *how the reply reads* — never *what is authorised*.

## 2. The security invariant (non-negotiable)

> **The LLM is a router and a writer of prose. It is not an authority.**

Concretely, S5 must preserve all of these — they are exit criteria, not aspirations:

1. **Fixed tool catalogue.** The LLM may only emit tool calls drawn from the agent cards' `skills[]` — which are injected as OpenAI function schemas via `ChatOpenAI.bind_tools()`, so the model can only name a function the catalogue defines. Every returned `tool_call` (`{agent_id, tool_id}`) is validated against the registry before fan-out; unknowns are dropped (logged), never executed.
2. **Server-fixed scopes.** Each tool's CIBA scope comes from the HR/IT-Agent `_TOOL_REGISTRY`, not from the LLM. An LLM that picks `hr.cubicle_assign` for an `Employee`-role user gets an IS CIBA denial (`hr_assets_write_rest` not granted) — no escalation.
3. **Per-action consent.** Every tool the LLM picks still triggers a CIBA consent widget; the widget's action text comes from `_TOOL_REGISTRY` (server-controlled, audit-logged, F-08 charset/length-capped), never from the LLM.
4. **No LLM-authored HTML.** The composed reply is rendered via `textContent`. The LLM's output is treated as untrusted text.
5. **No new outbound data exposure.** The router prompt contains: the user's message + the tool catalogue (labels/descriptions/arg names — all already public-ish). The composer prompt contains: the user's message + the tool *results* (which the user is already entitled to see — they triggered the tools and consented). No tokens, no `sub`s in prompts, no secrets. (Stage 8 security review verifies.)
6. **Key hygiene.** `OPENAI_API_KEY` lives only in `orchestrator/.env` (gitignored); it is sent to the AMP gateway via the `OPENAI_API_HEADER` (default `api-key`). Never logged, never in compose `environment:` literals, never echoed in errors. A repo-wide grep for an OpenAI API key shape (`sk-…`) must return nothing in tracked files (CI check + Stage 8).

## 3. In scope

| # | Item | Where |
|---|------|-------|
| 1 | `LLMClient` — thin `langchain-openai` wrapper exposing `route(user_message, tool_catalogue) -> list[ToolCall]` and `compose(user_message, tool_outcomes) -> str`. Behind a Protocol so tests inject a fake. | `orchestrator/llm/client.py` (new) |
| 2 | Router prompt template; the tool catalogue is injected as OpenAI function schemas via `ChatOpenAI.bind_tools()` and the model returns structured `tool_calls` (no JSON parsing) + per-`ToolCall` validation against the registry. Empty/all-invalid → keyword fallback. | `orchestrator/llm/router.py` (new) |
| 3 | Composer prompt template (incl. "if a tool failed with a missing-arg error, ask the user for it in plain language"; "if a consent was declined, say so plainly"). | `orchestrator/llm/composer.py` (new) |
| 4 | `apply_leave` chat tool: `hr_server` MCP route `POST /mcp/tools/apply_leave` (scope `hr_self_rest`) → `hr_service.apply_leave(...)`; `HRMcpClient.apply_leave(...)`; HR-Agent `_TOOL_REGISTRY["hr.apply_leave"]` (CIBA scope `openid hr_self_rest`, `_REQUIRED_ARGS = ["leave_type","start_date","end_date"]`, action_text "Apply for leave on your behalf"); `hr.apply_leave` skill on the hr_agent card. | `hr_server/`, `hr_agent/`, `tests/fixtures/agent_cards/hr_agent_valid.json` |
| 5 | `main.py` branch on `cfg.llm_fallback_mode`: `"llm"` → LLM router (wrapping `KeywordRouter` for fallback) + LLM composer; else → today's keyword-only path. `ChatRouterDeps` gains optional `llm_client`. | `orchestrator/main.py`, `orchestrator/chat/routes.py` |
| 6 | Refactor `chat/routes.py` fan-out so it takes "a resolved `[ToolCall]` + a compose callback" instead of calling `keyword_router.route()` and `"\n\n".join(...)` inline. Minimal — preserve all existing SSE events and error mapping. | `orchestrator/chat/routes.py` |
| 7 | Config: read `LLM_FALLBACK_MODE` at runtime (it's currently dead); add `OPENAI_MODEL` (default `gpt-4.1`), `LLM_TIMEOUT_S` (default `8`), `LLM_MAX_OUTPUT_TOKENS` (default `512`). | `orchestrator/config.py` |
| 8 | SPA: transient "Thinking…" affordance between `POST /api/chat` and the first SSE event (router latency). | `client/app.js`, `client/styles.css`, `client/index.html` |
| 9 | Tests: `FakeLLMClient`; router-parse, composer, fallback-on-error, unknown-tool-id-drop, `apply_leave` MCP tool, end-to-end with fake LLM. Plus an opt-in `pytest -m live_llm` smoke (not in the strict suite). | `tests/orchestrator/llm/`, `tests/hr_server/mcp/`, `tests/hr_agent/`, `tests/conftest.py` (marker) |
| 10 | `.env` already updated (`LLM_FALLBACK_MODE=llm`, `OPENAI_API_KEY=…`, `OPENAI_BASE_URL=…`). `docker-compose.yml` orchestrator service: confirm `env_file` carries the new vars (it does — whole-file load); add `OPENAI_MODEL`/`LLM_TIMEOUT_S` passthrough with defaults. | `docker-compose.yml` |

## 4. Out of scope (deferred / explicitly not done)

- Multi-turn dialogue state / slot-filling across turns. (LLM is single-shot per message; it asks clarifying questions *in its reply* but the orchestrator keeps no dialogue FSM.) Chat-history replay is a **stretch goal** — only if §3 lands with time to spare; if attempted it's a per-`Session` ring buffer (last N turns) fed into both prompts.
- Deleting / replacing `keyword_fallback.py` — it stays as the fallback.
- LLM-driven CIBA consent copy, LLM-driven scope selection, LLM-rendered HTML — all forbidden (see §2).
- Streaming OpenAI tokens to the SPA. (SSE event shape unchanged.)
- Making `hr_agent` / `it_agent` LLM-driven — they stay deterministic dispatchers.
- LangChain agent executor / `langchain-mcp-adapters` — not used; only `ChatOpenAI`.
- Any change to the `/api/chat` external contract or the SSE event types.

## 5. Architecture summary (full detail: [`sprint-5-tech-arch.md`](sprint-5-tech-arch.md))

```
SPA --POST /api/chat--> Orchestrator
                          |
                          | (mode == "llm")
                          v
                   LLMRouter.route(msg)
                     |  OpenAI call #1 (router prompt: msg + tool catalogue as bind_tools() function schemas)
                     |  model returns structured tool_calls -> validate each {agent_id,tool_id} vs registry (no JSON parsing)
                     |  -> [ToolCall, ...]   (drop unknowns; if empty -> KeywordRouter.route(msg))
                     v
            (UNCHANGED Sprint-4 serial fan-out)
              for each ToolCall:  A2A message/send -> agent CIBA -> SSE consent_required
                                  user approves -> token-C -> MCP tool -> ResultPayload/ErrorPayload
                     |
                     |  collect [(tool_id, result_or_error), ...]
                     v
                   LLMComposer.compose(msg, outcomes)
                     |  OpenAI call #2 (composer prompt) -> natural-language reply
                     |  (on failure: fall back to "\n\n".join(_render_result(...)))
                     v
                ChatMessageEvent(content=reply)  --SSE--> SPA
```

- OpenAI calls: `temperature=0` (router) / `0.3` (composer); `max_output_tokens` capped; per-call timeout `LLM_TIMEOUT_S` (default 8 s); the client retries transient gateway 5xx (`max_retries=5`) before any exception → fall back. OpenAI is reached via the WSO2 AMP AI Gateway (`OPENAI_BASE_URL`), key sent via `OPENAI_API_HEADER` (default `api-key`); observability via `amp-instrumentation` + `traceloop-sdk`.
- `langchain-openai`'s `ChatOpenAI` is the wire layer; the `LLMClient` Protocol is what `chat/routes.py` depends on (so tests inject a fake — no network in unit tests).

## 6. M5 exit criteria (Stage 11 ticks these)

1. `LLM_FALLBACK_MODE=llm` + valid key → free-text "How much annual leave do I have and where's my cubicle?" routes to `hr.read_balance` + `hr.cubicle_lookup_self`, runs both CIBA consents, SPA shows one natural reply covering both.
2. "I'd like annual leave from 2026-06-10 to 2026-06-14, reason: family trip" → `hr.apply_leave` with all four args → CIBA → leave request created → My Leaves panel shows it on the SSE settle. (UC-13 Main flow, end to end.)
3. "I want to take leave" (no dates) → LLM picks `hr.apply_leave` partial / or `hr.read_policy` → dispatcher returns `ERR-AGENT-002` (if partial) → composer asks for the dates in plain language. No CIBA wasted.
4. Prompt-injection test: a message instructing the agent to "assign cubicle C-099 to everyone" while signed in as `employee_user` → either the LLM doesn't pick `hr.cubicle_assign`, or it does and IS denies the CIBA (no `hr_assets_write_rest`) → user sees "you're not authorised," not a successful escalation.
5. Kill network to OpenAI / the AMP gateway (or set an invalid key) → next chat message routes via `KeywordRouter` within the timeout (after the client exhausts its `max_retries=5` transient-5xx retries); reply composed via `_render_result`; SPA still works; security/SSE unchanged. Logs show `llm_router_failed … falling_back_to_keyword`.
6. A repo-wide grep for an OpenAI API key shape (`sk-…`) over tracked files returns nothing.
7. Full test suite green (strict mode) including all new tests. `pytest -m live_llm` documented but not required.
8. `./scripts/demo-up.sh` smoke green; manual gate runbook ([`sprint-5-stage-11-manual-gate.md`](sprint-5-stage-11-manual-gate.md)) walked.

## 7. Build slices (detail: [`sprint-5-stage-7-slice-plan.md`](sprint-5-stage-7-slice-plan.md))

- **S5.0** — config wiring (`LLM_FALLBACK_MODE` read at runtime; new env vars; `OrchestratorConfig` fields) + `LLMClient` Protocol + `langchain-openai` `ChatOpenAI` adapter + `FakeLLMClient` for tests. No behaviour change yet (mode default stays effective; nothing reads the client).
- **S5.1** — `hr.apply_leave` chat tool (MCP route + client method + dispatcher registry entry + card skill + `_REQUIRED_ARGS`). Reachable from keyword mode too? No — keep keyword router as-is (apply needs the LLM); but the MCP tool + dispatcher entry are tested standalone. This slice has zero dependency on the LLM and can ship/test independently.
- **S5.2** — `LLMRouter` (router prompt + `bind_tools()` function-schema tool catalogue → structured `tool_calls`, no JSON parsing + per-ToolCall validation + keyword fallback) + refactor `chat/routes.py` to consume a resolved `[ToolCall]`. Wire `main.py` `"llm"` branch for routing only (composer still `_render_result`).
- **S5.3** — `LLMComposer` (composer prompt + the missing-arg / declined-consent instructions) + wire it in; `_render_result` becomes the composer's fallback.
- **S5.4** — SPA "Thinking…" affordance; `docker-compose.yml` passthrough; integration test with `FakeLLMClient` end-to-end; `./scripts/demo-up.sh` + manual gate.

Order rationale: S5.1 is independent and de-risks the apply-leave gap early; S5.0 unblocks S5.2/S5.3; S5.2 before S5.3 so routing works before composition (and a half-built S5 still degrades cleanly to `_render_result`).

## 8. Risks carried from Stage 1 (live tracking)

R1 latency (2 OpenAI round-trips) — mitigated by the `gpt-4.1` model + token caps + 8 s timeout→fallback; consider a single combined call only if Stage 6 finds it clean.
R2 hallucinated tool — mitigated by per-ToolCall registry validation (§2.1).
R3 incomplete args — handled by the existing `_REQUIRED_ARGS` pre-CIBA check; composer asks for the rest.
R4 key leak — §2.6 + CI grep + Stage 8.
R5 prompt injection — §2.2 backstop + Stage 11 test case.
R6 determinism — `temperature=0` router; keyword fallback floor; documented as "security deterministic, phrasing not."

## 9. Process

Standard gated flow: Stage 1 (done) → 2 (done) → **3 (this doc)** → 4 UX → 5 API → 6 tech arch → 7 slice plan → 8 multi-agent review (architect / code / security) → 9 implementation (S5.0–S5.4) → 10 test coverage → 11 manual gate ↔ bug fixes → 12 retro + signoff. Commit per slice; stage specific files; `orchestrator/.env` never committed.
