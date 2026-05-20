# Sprint 5 — Stage 10: Automated Test Coverage

**Date:** 2026-05-11
**Suite status:** **1058 tests / 64 files — strict-green** (`./tools/run-tests.sh`; rejects "failed"/"error"/"xfailed" in the summary line). Up from 1002/60 at the start of S5 (+56 tests, +4 files).

This sprint added no live network in the strict suite — every LLM call is mocked (`FakeLLMClient` returns canned `RoutedToolCall`s / canned prose / an `LLMError` to raise). The `amp_client.py` adapter (the only langchain-touching module) is *not* imported by the suite; a dedicated test asserts the rest of the LLM layer imports without langchain installed (it isn't in the test venv).

---

## 1. New / changed test files

| File | What it covers |
|---|---|
| `tests/orchestrator/llm/conftest.py` | `FakeLLMClient` (structural `LLMClient`); `agent_registry` fixture (real demo cards); `make_deps(...)` minimal duck-typed `ChatRouterDeps`. |
| `tests/orchestrator/llm/test_router.py` (27) | router output handling: structured `tool_calls` → `RoutedToolCall`s, empty `tool_calls` → `[]` (valid), `args` carried through verbatim (no JSON parsing — `parse_router_output` was removed, so there is no markdown-fence/garbage/`LLMError`-on-unparseable path to test). `strip_sensitive`: drops `sub`/`*_sub`/`issued_by`/`*token*`/`*secret*` keys + UUID-shaped string values, recursive into dicts/lists, passes scalars/non-UUID strings. `render_outcomes`: success strips `sub`, failure shows `error_id`+`reason`, empty → `(no tools ran)`. `router_system`/`tool_schemas`: every catalogue tool becomes a function schema, `hr.lookup_employee` absent. `build_catalogue`: from registry, `args` populated, `hr.lookup_employee` absent. `resolve_tool_calls`: LLM hit → those tools; hallucinated/non-scalar args stripped, valid kept; unknown agent + unknown tool dropped, valid kept; internal-only `hr.lookup_employee` dropped; LLM `LLMError` → keyword fallback; LLM no tool_calls → keyword fallback; mode=keyword (with a client present) → never calls LLM; no llm_client → keyword. |
| `tests/orchestrator/llm/test_composer.py` (5) | `compose_reply`: LLM reply used; LLM `LLMError` → `fallback_text`; no outcomes → `fallback_text` (no LLM call); mode=keyword → `fallback_text` (no LLM call); no llm_client → `fallback_text`. |
| `tests/orchestrator/llm/test_card_dispatcher_consistency.py` (3) | `hr_agent` / `it_agent` cards' `skills[]` == dispatcher `_TOOL_REGISTRY` keys minus the off-card `hr.lookup_employee`; each carded skill's `args` matches the expected set 1:1 and ⊇ that tool's `_REQUIRED_ARGS`; `hr.lookup_employee` is in the dispatcher registry but not in the card. |
| `tests/orchestrator/llm/test_no_langchain.py` (1) | With `langchain` / `langchain_core` / `langchain_openai` imports blocked, `orchestrator.llm.client/prompts/router/composer` all import fine — and `orchestrator.llm.amp_client` raises `ModuleNotFoundError` (proving the langchain dep lives only there, behind the lazy import in `main.py`). |
| `tests/orchestrator/test_config.py` (+5) | `openai_model` default + override; `llm_timeout_s` / `llm_max_output_tokens` defaults + parse; **`LLM_FALLBACK_MODE=llm` + no key → warns, keeps mode, no exception** (exit-criterion §6.13); blank `LLM_FALLBACK_MODE` → `keyword`. |
| `tests/common/logging/test_redaction.py` (+4) | An OpenAI-API-key-shaped value (`sk-…`) is stripped from a URL in a log line, from a bare `key=…`, and from a `LogRecord` via `RedactionFilter`; a bare too-short `sk-` prefix is *not* over-redacted. |
| `tests/hr_server/mcp/test_tools.py` (+8, S5.1) | `apply_leave` MCP tool: 200 success (store has a new Pending `LRxxx`); 200 each business rejection (`invalid_leave_type`, `insufficient_notice`, `invalid_dates`, `insufficient_balance`); 401 missing scope (`ERR-MCP-003`); 401 no bearer (`ERR-AUTH-006`); 422 malformed body. |
| `tests/hr_agent/mcp/test_client.py` (+2, S5.1) | `HRMcpClient.apply_leave` POSTs `{leave_type,start_date,end_date,reason}` to `/mcp/tools/apply_leave` with the Bearer token-B header, returns the parsed body; `reason` defaults to `""`. |
| `tests/hr_agent/ciba/test_orchestrator.py` (+2, S5.1) | `hr.apply_leave` with all required args → `ConsentRequiredPayload` whose `action` ("Apply for leave on your behalf") and CIBA `scope` (`openid hr_self_rest`) come from the server-side `_TOOL_REGISTRY`; with only `leave_type` → `ErrorPayload` `ERR-AGENT-002`, **no CIBA `initiate` call**. |
| `tests/common/a2a/test_agent_card.py` (changed) | `llm_projection` skill dicts now have `{tool_id, label, description, scope, args}` and `args` is a list. |
| `tests/hr_server/service/test_cubicle_service.py` / `tests/it_server/mcp/test_tools.py` (changed earlier this branch) | self-service-by-`sub` resolution (S4.11 post-build fix). |

## 2. Mapping to the M5 exit criteria (`sprint-5.md` §6 + reconciliation §3)

| Exit criterion | Covered by |
|---|---|
| §6.1 free-text → right tools + args + CIBA + NL reply | unit: `resolve_tool_calls` (route+validate) + `compose_reply` + the unchanged fan-out tests; e2e: Stage 11 manual gate. |
| §6.2 "annual leave June 10–14" submits a real request → My Leaves panel | unit: `apply_leave` MCP tool success + `HRMcpClient.apply_leave` + dispatcher consent/scope; e2e: Stage 11. |
| §6.3 "I want leave" (no dates) → composer asks for them, no CIBA wasted | unit: dispatcher `ERR-AGENT-002`-no-CIBA + `_REQUIRED_ARGS["hr.apply_leave"]` + composer prompt has the "ask for missing args" rule; e2e: Stage 11. |
| §6.4 prompt-injection → unknown tools dropped / IS denies the CIBA, no escalation | unit: `resolve_tool_calls` drops unknown agent+tool + internal-only tool; the IS-scope-denial leg is exercised by the existing CIBA-denied tests; e2e: Stage 11 (the dedicated injection scenario). |
| §6.5 kill OpenAI / AMP gateway → keyword fallback within the timeout, demo still works | unit: `resolve_tool_calls` LLM-error → keyword + `compose_reply` LLM-error → fallback + `_run_serial_fan_out` outer guard publishes a terminal `chat_message`; e2e: Stage 11 (kill-the-network scenario). |
| §6.6 no OpenAI API key (`sk-…`) string in tracked files | `git grep -nP 'sk-[A-Za-z0-9_-]{20,}'` over tracked files → only the fake `sk-` sentinel in `test_redaction.py`; nothing in git history. Plus the redaction filter (`test_redaction.py` `TestOpenAiApiKeyRedaction`). |
| §6.7 full suite green incl. all new tests | ✅ 1058/64 strict-green. `pytest -m live_llm` documented (Stage 11), not in the strict suite. |
| §6.8 `./scripts/demo-up.sh` smoke green; manual gate walked | ✅ smoke green; orchestrator log shows `llm_client_enabled model=gpt-4.1`. Manual gate = Stage 11. |
| §6.9 no `sub`/UUID/token reaches a prompt or chat reply | unit: `strip_sensitive` + `render_outcomes` + `_render_result` now `strip_sensitive`s `result.data`; e2e: Stage 11 (`hr.lookup_employee`-via-injection — though it's off-card so it can't even be routed; the strip is belt-and-braces). |
| §6.10 forced OpenAI / AMP gateway auth error never logs the key | `test_redaction.py` `TestOpenAiApiKeyRedaction.test_filter_strips_api_key_from_record` + `OpenAILLMClient` logs `type(exc).__name__`+`str(exc)[:200]`, never `repr(exc)`. (A live forced-error test is `-m live_llm`, not in the strict suite.) |
| §6.11 LLM layer importable without langchain | `test_no_langchain.py`. |
| §6.12 card `args` ⊇ dispatcher `kwargs_builder` keys | `test_card_dispatcher_consistency.py`. |
| §6.13 `from_env` llm-mode + no key → warn, not crash | `test_config.py::test_llm_mode_without_key_does_not_crash`. |

## 3. Deliberately not covered by the strict suite (covered elsewhere)

- **The live OpenAI round-trip** (router prompt → real model → structured `tool_calls` via function-calling; composer prompt → real model → prose), reached through the WSO2 AMP AI Gateway. Exercised by the Stage-11 manual gate and an opt-in `pytest -m live_llm` smoke (requires `langchain-openai` installed + a valid `OPENAI_API_KEY` + `OPENAI_BASE_URL`; deliberately *not* part of `./tools/run-tests.sh`). Rationale: a strict CI suite must not depend on a paid external API / network.
- **The SPA "thinking" affordance** — `client/app.js` is vanilla JS with no test harness. Covered by the Stage-11 manual gate (and it's a one-line addition reusing the already-tested `#routing-line` machinery).
- **The orchestrator Docker image building with `langchain-openai`** — verified by running `./scripts/demo-up.sh` (orchestrator built; `llm_client_enabled` in the log; 6/6 healthchecks pass), not by a unit test.

## 4. Regression check

All Sprint 1–4 behaviour is unchanged when `LLM_FALLBACK_MODE != "llm"` or no key is set (`llm_client` is `None` → `resolve_tool_calls` / `compose_reply` no-op to the keyword router / `_render_result`). The existing 22 `tests/orchestrator/chat/test_routes.py` cases (which run with `llm_client=None`) pass unmodified; the CIBA / SSE / fan-out / `terminating`-fence / logout-cascade tests are untouched.
