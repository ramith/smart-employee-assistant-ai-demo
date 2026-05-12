# Sprint 5 — Stage 7: Build Slice Plan

**Date:** 2026-05-11
**Refs:** [`sprint-5.md`](sprint-5.md) (binding), [`sprint-5-tech-arch.md`](sprint-5-tech-arch.md) (reference), [`sprint-5-stage-5-api-design.md`](sprint-5-stage-5-api-design.md) (contracts).

Five slices. Each ends green (strict test suite) and is committed separately on `sprint-5-build`. The order de-risks the independent `apply_leave` work early and keeps a half-built S5 degrading cleanly to Sprint-4 behaviour at every checkpoint.

---

## S5.1 — `apply_leave` chat tool *(independent of the LLM; do first)*

**Why first:** zero LLM dependency, closes the long-open UC-13 gap, fully testable on its own. If S5 stalled after this slice, keyword mode would still be unchanged and we'd have a usable `apply_leave` MCP tool.

- `hr_server/mcp/tools.py`: `ApplyLeaveArgs`, `ApplyLeaveResult` Pydantic models; `POST /mcp/tools/apply_leave` handler (scope `hr_self_rest`); add the three to `__all__`; map `hr_service.apply_leave(...)` return → `ApplyLeaveResult`.
- `hr_agent/mcp/client.py`: `apply_leave(self, *, token_b, leave_type, start_date, end_date, reason="", request_id=None)`.
- `hr_agent/ciba/orchestrator.py`: `_REQUIRED_ARGS["hr.apply_leave"] = ["leave_type","start_date","end_date"]`; `_TOOL_REGISTRY["hr.apply_leave"]` entry (action_text "Apply for leave on your behalf", method `apply_leave`, kwargs builder, CIBA scope `openid hr_self_rest`).
- `tests/fixtures/agent_cards/hr_agent_valid.json`: add `hr.apply_leave` skill (`scope`/`required_scopes`/`args` — `args` field is added to the `Skill` model in S5.2; for now include it in the JSON, the loader ignores unknown keys until S5.2 wires it).
- `orchestrator/chat/routes.py` `_render_result`: add an `hr.apply_leave` case (keyword-mode fallback reply).
- Tests: `apply_leave` MCP tool — 200 success (store has the new `LRxxx`), 200 each business rejection (`invalid_leave_type`, `invalid_dates`, `insufficient_notice`, `insufficient_balance`), 401 missing scope (`ERR-MCP-003`), 401 no bearer, 422 malformed body; `HRMcpClient.apply_leave` posts the right body/path/header; HR dispatcher `hr.apply_leave` with full args → CIBA initiated (scope `openid hr_self_rest`) / with a missing arg → `ERR-AGENT-002`, no CIBA call; `_render_result` `hr.apply_leave` renders both branches.

Commit: `S5.1: apply_leave chat tool (MCP route + HR-Agent dispatcher entry + card skill)`.

## S5.2 — `LLMClient` + `GeminiLLMClient` + config + `LLMRouter` + card expansion + routing wired

**Why next:** unblocks LLM routing; the composer still uses `_render_result` after this slice (clean partial state).

- `orchestrator/config.py`: `from_env` reads `LLM_FALLBACK_MODE` at runtime; parses `GEMINI_MODEL`/`LLM_TIMEOUT_S`/`LLM_MAX_OUTPUT_TOKENS`; new `OrchestratorConfig` fields; warn-not-crash if `llm` + no key.
- `orchestrator/agent_registry/cards.py`: `Skill` gains `args: list[str] = []` (or `tuple`); `llm_tool_list()` surfaces it. `tests/fixtures/agent_cards/*.json`: expand both cards so every `_TOOL_REGISTRY` tool is a listed skill, each with `args`.
- `orchestrator/llm/__init__.py`, `client.py` (Protocol + dataclasses + `LLMError`), `gemini.py` (`GeminiLLMClient`, lazy `langchain_google_genai` import), `prompts.py` (router system prompt + `parse_router_output`; composer prompt + `render_outcomes` — composer prompt lands now even though the composer call is wired in S5.3), `router.py` (`resolve_tool_calls` + `_validate` + `_build_catalogue`).
- `orchestrator/main.py`: build `GeminiLLMClient` iff `llm` mode + key; pass into `ChatRouterDeps(llm_client=...)`.
- `orchestrator/chat/routes.py`: `ChatRouterDeps.llm_client: LLMClient | None`; `post_chat` calls `await resolve_tool_calls(body.message, deps)`; `_run_serial_fan_out` gains `user_message: str` (threaded through, not yet used for composition).
- `tests/orchestrator/llm/`: `FakeLLMClient`; `parse_router_output` (valid array, empty array, markdown-fenced, malformed-all → `LLMError`, mixed valid/invalid → keeps valid); `resolve_tool_calls` (LLM hit → those ToolCalls; LLM `LLMError` → keyword fallback; LLM `[]` → keyword fallback; unknown agent/tool dropped; hallucinated args filtered); `_build_catalogue` from a fixture registry; config `from_env` (mode/key/new vars); `test_agent_card_matches_dispatcher_registry` (card skills ⇔ `_TOOL_REGISTRY` for hr + it).

Commit: `S5.2: LLM router + GeminiLLMClient + config wiring + agent-card expansion`.

## S5.3 — `LLMComposer` wired

- `orchestrator/llm/composer.py`: `compose_reply(user_message, outcomes, fallback_text, deps) -> str` — `if llm mode + client + outcomes: try deps.llm_client.compose(...) except LLMError: return fallback_text; else return fallback_text`.
- `orchestrator/chat/routes.py` `_run_serial_fan_out`: build `outcomes: list[ToolOutcome]` alongside `per_tool_outputs`; at the end compute `fallback_text` then `final_content = await compose_reply(user_message, outcomes, fallback_text, deps)`.
- Tests: composer happy path (fake returns prose → that's the `ChatMessageEvent` content); composer `LLMError` → `fallback_text` used; composer with a mix of ok/declined/missing-arg outcomes still calls the fake with the right `ToolOutcome` list; end-to-end with `FakeLLMClient` (route → fan-out → compose) via the existing chat-route test harness.

Commit: `S5.3: LLM reply composer wired (with _render_result fallback)`.

## S5.4 — SPA "Thinking…" + compose passthrough + integration + demo-up

- `client/index.html`: thinking-placeholder bubble template (muted text + dots).
- `client/app.js`: on `/api/chat` submit, append the placeholder; on the first SSE event for that request (`routing`/`consent_required`/`chat_message`) or on request error, remove it.
- `client/styles.css`: `.chat-thinking` (uses `--text-muted`; reuse existing dot animation if present, else 3 CSS `·`).
- `docker-compose.yml`: orchestrator service — `GEMINI_MODEL=${GEMINI_MODEL:-gemini-2.5-flash}`, `LLM_TIMEOUT_S=${LLM_TIMEOUT_S:-8}` passthrough; **do not** add `GEMINI_API_KEY` to the `environment:` block (it stays in the gitignored `.env` only, loaded via `env_file`).
- Update `scripts/demo-up.sh` banner (mention LLM mode is on; keyword fallback active).
- `./scripts/demo-up.sh --clean` → 6/6 healthy → spot-check a free-text chat in the browser → run Stage 11.
- Tests: SPA changes are not unit-tested (vanilla JS, no harness) — covered by the Stage 11 manual gate; add a note. Final `tools/run-tests.sh` strict-green.

Commit: `S5.4: SPA thinking indicator + compose passthrough + compose docker env`.

## S5.0 — (folded into S5.2)

The original Stage-3 plan listed an S5.0 "config + client Protocol + adapter, no behaviour change." In practice that's inseparable from S5.2 (the router needs the config + client to do anything) — so S5.0's contents are the first half of S5.2. No standalone S5.0 commit.

---

## Cross-cutting checks (every slice)

- `grep -rn "AIza" --` over tracked files → empty (the key lives only in gitignored `orchestrator/.env`).
- `tools/run-tests.sh` strict-green.
- No change to `/api/chat` contract or SSE event types.
- `orchestrator/.env` never staged.

## Stage-8 review hand-off

Before S5.1 starts, Stage 8 runs three parallel reviews on the Stage 1–7 docs:
- **architect-reviewer** — is the "LLM = router/composer only, never authority" boundary actually airtight given the v4 topology? Is the fallback truth table complete? Is the card⇔registry consistency the right contract?
- **code-reviewer** — does the `chat/routes.py` refactor stay minimal? Any spot where an LLM exception could escape? Is `langchain_google_genai` imported lazily so keyword-only deployments don't need it?
- **security-auditor** — what exactly is in each prompt? Could anything sensitive (sub, token, secret) reach Gemini? Is the API key handling tight (no log lines, no compose literals, gitignored)? Is the prompt-injection backstop (scope-policy + CIBA) sufficient, and is it test-covered?

Findings → a Stage 6.5-style reconciliation pass only if a NO-GO; otherwise fold minor notes into the slices.
