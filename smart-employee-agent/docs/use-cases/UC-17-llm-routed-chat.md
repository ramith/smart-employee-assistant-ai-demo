# UC-17 — LLM-routed natural-language chat

> **Build status:** NEW in Sprint 5 (M5). The orchestrator's chat routing + reply composition become LLM-driven (Gemini `gemini-2.5-flash` via `langchain-google-genai`). The keyword router (`orchestrator/chat/keyword_fallback.py`) is retained as the automatic fallback. See [`docs/architecture/sprint-5.md`](../architecture/sprint-5.md).

**Sprint:** 5
**Priority:** High
**Maps to N-tests:** N1, N4, N7 (chat round-trip — reused), R-LLM-1 (router parse), R-LLM-2 (composer), R-LLM-3 (fallback-on-error), R-LLM-4 (unknown-tool-id guard)
**Maps to scenarios:** Sprint 5 demo storyboard — free-text chat that "just works"

## Actors
- **Primary:** Any signed-in user (`employee_user`, `hr_admin_user`)
- **Secondary:** SPA, Orchestrator (now hosting an LLM router + composer), Gemini API, HR Agent, IT Agent, HR Server / IT Server (MCP), WSO2 IS

## Preconditions
- UC-01 succeeded (user signed in, session cookie + token-A held).
- `LLM_FALLBACK_MODE=llm` and a valid `GEMINI_API_KEY` are set in `orchestrator/.env` (gitignored).
- The orchestrator's `AgentRegistry` is populated from the agent cards; `registry.llm_tool_list()` yields the tool catalogue.
- Gemini is reachable. (If not — see EX-3.)

## Trigger
User types a free-text message in the SPA chat, e.g. *"How much annual leave do I have, and where's my cubicle?"*

## Main flow
1. SPA `POST <orch>/api/chat` with `{session_id, user_message}` (unchanged contract).
2. Orchestrator builds the **router prompt**: a system instruction describing each available tool (`tool_id`, `agent_id`, `label`, `description`, the args it accepts) from `registry.llm_tool_list()`, plus the user message. It calls Gemini (`temperature=0`, `max_output_tokens` capped, 8 s timeout) requesting a JSON array of `{agent_id, tool_id, args}`.
3. Orchestrator parses the JSON. For each entry it validates: `agent_id` is a known card, `tool_id` is in that agent's skills *and* in the agent's dispatcher `_TOOL_REGISTRY` (the orchestrator can't see the dispatcher's registry directly, but the agent card's `skills[].id` is the contract). Unknown entries are dropped with a warning. The surviving list is an ordered `[ToolCall, ...]` — identical shape to `KeywordRouter.route()`'s output, so the **rest of the fan-out is unchanged**.
4. If the surviving list is empty (LLM returned no tools, or all were unknown), the orchestrator falls back to `KeywordRouter.route(user_message)`. If *that* is also empty, the orchestrator replies with the LLM's natural-language clarification text (step 8) or a generic "I'm not sure what you'd like me to do."
5. Orchestrator emits SSE `routing` events and runs the **existing serial CIBA fan-out** (per UC-02/03): for each `ToolCall`, the specialist agent validates token-A, initiates per-agent CIBA, the SPA renders the consent widget (action text from `_TOOL_REGISTRY`, **not** from the LLM), the user approves, token-C is minted, the MCP tool runs, the agent returns an A2A `ResultPayload` or `ErrorPayload`.
6. Orchestrator collects the raw tool outputs (the `data` dicts on success; `error_id`/`reason` on failure).
7. Orchestrator builds the **composer prompt**: the user message + a structured dump of each tool's outcome (tool_id + result data, or tool_id + error). It calls Gemini (`temperature ≈ 0.3`, capped tokens, 8 s timeout) for a single natural-language reply.
8. Orchestrator publishes `ChatMessageEvent(content=<llm reply>)`. SPA renders it.

## Exception flows

### EX-1 — LLM emits an unknown / hallucinated tool
1. Step 3: `tool_id` not in any card's skills (or `agent_id` unknown). The entry is dropped; a `WARNING llm_router_dropped_unknown_tool tool=… agent=…` is logged.
2. If at least one valid `ToolCall` remains, the fan-out proceeds with just the valid ones.
3. If none remain → fall back to keyword router (step 4).
4. **No privilege escalation is possible even if a valid-but-wrong tool is picked:** the tool's required scope is fixed server-side; `hr.cubicle_assign` needs `hr_assets_write_rest`, which the `Employee` role doesn't hold, so IS denies the CIBA → the user sees `ERR-CIBA-007`-style "you're not authorised for that," not a successful escalation. This is a deliberate Stage 11 test (prompt-injection scenario).

### EX-2 — LLM emits valid tool but incomplete args
1. e.g. user said "I want to apply for leave" with no dates → LLM emits `{agent_id: hr_agent, tool_id: hr.apply_leave, args: {leave_type: "Annual Leave"}}` (missing `start_date`, `end_date`).
2. The HR dispatcher's pre-CIBA arg check (`_REQUIRED_ARGS["hr.apply_leave"] = ["leave_type", "start_date", "end_date"]`) returns `ErrorPayload(error_id="ERR-AGENT-002", reason="Missing required arguments for hr.apply_leave: ['end_date', 'start_date']")` — **no CIBA round-trip wasted**.
3. The composer receives that error and replies with a clarification: *"To apply for annual leave I need the start and end dates — e.g. 'from June 10 to June 14'."* (The composer prompt explicitly instructs: "if a tool failed with a missing-argument error, ask the user for the missing info in plain language.")

### EX-3 — Gemini unreachable / rate-limited / invalid key (the fallback path — R-LLM-3)
1. The router call raises (timeout, HTTP 4xx/5xx, network error, malformed JSON that won't parse).
2. Orchestrator logs `WARNING llm_router_failed reason=… falling_back_to_keyword` and routes via `KeywordRouter.route(user_message)`.
3. Fan-out proceeds as in Sprint 4.
4. The composer call: if it *also* fails, the orchestrator composes the reply via the Sprint-4 `_render_result` concatenation. So a total Gemini outage degrades the chat to exactly the Sprint-4 behaviour — never a hard error.
5. SSE / consent / security behaviour is identical to the LLM path.

### EX-4 — User denies CIBA consent on an LLM-routed tool
- Identical to UC-04: IS returns `access_denied`, the agent emits `ERR-CIBA-005`, the composer (or `_render_result` on fallback) surfaces *"That action wasn't authorised — you declined."* No data written.

### EX-5 — LLM picks two tools; the first's CIBA is denied, the second is approved
- The fan-out is serial and independent (per UC-03). The composer receives `[hr.cubicle_lookup_self → ERR-CIBA-005, it.get_my_assets → {assets:[…]}]` and replies covering both: *"I couldn't look up your cubicle (you declined that one), but here are your assigned IT assets: …"*

## Postconditions
- **Success:** the SPA shows a coherent natural-language reply reflecting every tool that ran (and any that were declined/failed). For write tools, the underlying store reflects the change; the relevant sidebar panel re-fetches on the SSE settle.
- **Failure (LLM):** chat degrades to Sprint-4 keyword behaviour; no user-visible error attributable to the LLM.
- **Security invariant (always):** the set of *possible* actions is the fixed tool catalogue; each action's *scope* is server-fixed; each action's *consent* is per-action via CIBA. The LLM influences only which tools are tried and how the reply reads — never what's authorised.

## Design notes for downstream stages

### UX (Stage 4)
- Add a transient "Thinking…" affordance between `POST /api/chat` and the first `routing`/`consent_required`/`chat_message` SSE event (the router LLM call adds latency). Reuse the existing progress element pattern. Empty/idle states unchanged.
- The composed reply is rendered with `textContent` (no HTML) — same as today. The LLM must not be relied on to produce safe HTML; we don't render its output as markup.

### API (Stage 5)
- `/api/chat` request/response contract unchanged. SSE event types unchanged. New internal interface: `LLMClient` (router + composer methods) behind a Protocol so tests inject a fake.
- New env: `LLM_FALLBACK_MODE` (now read at runtime), `GEMINI_API_KEY`, optional `GEMINI_MODEL` (default `gemini-2.5-flash`), `LLM_TIMEOUT_S` (default 8).

### Architecture (Stage 6)
- Router and composer are two prompt templates + one thin `langchain-google-genai` wrapper. No LangChain agent executor, no `MultiServerMCPClient`.
- `main.py` branches on `cfg.llm_fallback_mode`: `"llm"` → `LLMRouter` (wrapping the keyword router for fallback) + LLM composer; anything else → today's `KeywordRouter`-only path.
- The `ChatRouterDeps` gains an optional `llm_client` + `keyword_router` (already there). The fan-out code is refactored only enough to take "a list of ToolCalls + a compose callback" rather than calling `keyword_router.route()` inline.

### Testing (Stages 10–11)
- All LLM calls are mocked in unit tests (a `FakeLLMClient` returning canned JSON / canned prose). One opt-in live smoke (`pytest -m live_llm`) hits the real API — not part of the strict suite.
- Stage 11 manual gate: ≥5 free-text scenarios + the prompt-injection scenario + the kill-the-network fallback scenario.
