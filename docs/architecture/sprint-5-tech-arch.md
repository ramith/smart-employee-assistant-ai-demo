# Sprint 5 — Stage 6: Technical Architecture

**Date:** 2026-05-11
**Binding plan:** [`sprint-5.md`](sprint-5.md). This doc is the implementation reference for S5.0–S5.4.

---

## 1. Module layout (new)

```
orchestrator/llm/
  __init__.py
  client.py      # LLMClient Protocol, ToolCatalogueEntry, RoutedToolCall, ToolOutcome, LLMError
  amp_client.py  # OpenAILLMClient(LLMClient) — wraps langchain_openai.ChatOpenAI (via the WSO2 AMP AI Gateway)
  prompts.py     # router/composer system prompts + the catalogue/outcome renderers
  router.py      # resolve_tool_calls(msg, deps) -> list[ToolCall]  (LLM call -> validate -> keyword fallback)
  composer.py    # compose_reply(msg, outcomes, fallback_text, deps) -> str  (LLM call -> _render_result fallback)
```
`tests/orchestrator/llm/` mirrors this; `FakeLLMClient` lives in `tests/orchestrator/llm/conftest.py` (or a shared `tests/fakes.py`).

Touch-points in existing code (kept minimal):
- `orchestrator/config.py` — `from_env` reads `LLM_FALLBACK_MODE` at runtime; adds `openai_model`, `llm_timeout_s`, `llm_max_output_tokens` fields + env parsing; warns (doesn't crash) if mode=`llm` but no key.
- `orchestrator/main.py` — builds an `OpenAILLMClient` iff `cfg.llm_fallback_mode == "llm"` and `cfg.openai_api_key`; passes it (or `None`) into `ChatRouterDeps`.
- `orchestrator/chat/routes.py` — `ChatRouterDeps` gains `llm_client: LLMClient | None`; `post_chat` calls `resolve_tool_calls(...)` instead of `deps.keyword_router.route(...)` directly; `_run_serial_fan_out` gains a `user_message: str` param and, at the end, calls `compose_reply(...)` instead of `"\n\n".join(...)` directly. **No other changes** to the fan-out loop, the SSE events, the error mapping, the CIBA polling.
- `orchestrator/agent_registry/cards.py` — `Skill` model gains optional `args: list[str] = []`; `llm_tool_list()` includes it. (Card loader change only; no behaviour change for existing consumers.)
- `tests/fixtures/agent_cards/hr_agent_valid.json`, `it_agent_valid.json` — expanded so **every dispatcher `_TOOL_REGISTRY` tool is listed as a skill** with its `args`. (Stage 10 adds a consistency test: card-skills ⇔ `_TOOL_REGISTRY` keys.)
- `hr_server/mcp/tools.py`, `hr_agent/mcp/client.py`, `hr_agent/ciba/orchestrator.py` — the `apply_leave` tool (S5.1).
- `client/index.html|app.js|styles.css` — the "Thinking…" affordance (S5.4).
- `docker-compose.yml` — `OPENAI_MODEL` / `LLM_TIMEOUT_S` passthrough (with defaults); **not** `OPENAI_API_KEY`.

## 2. Data types (`orchestrator/llm/client.py`)

```python
@dataclass(frozen=True)
class ToolCatalogueEntry:
    agent_id: str        # "hr_agent"
    tool_id: str         # "hr.apply_leave"
    label: str           # card skill name
    description: str     # card skill description
    args: tuple[str, ...]  # arg names the tool accepts (from the card skill's `args`)

@dataclass(frozen=True)
class RoutedToolCall:     # raw LLM output, pre-validation
    agent_id: str
    tool_id: str
    args: dict

@dataclass(frozen=True)
class ToolOutcome:        # one tool's result, fed to the composer
    agent_id: str
    tool_id: str
    ok: bool
    data: dict | None = None       # set when ok
    error_id: str | None = None    # e.g. "ERR-CIBA-005", "ERR-AGENT-002" when not ok
    reason: str | None = None      # human reason when not ok

class LLMError(Exception):
    """Any LLM transport/parse/timeout failure. Caller falls back."""

class LLMClient(Protocol):
    async def route(self, user_message: str, catalogue: list[ToolCatalogueEntry]) -> list[RoutedToolCall]: ...
    async def compose(self, user_message: str, outcomes: list[ToolOutcome]) -> str: ...
```
The chat router type (`orchestrator/chat/keyword_fallback.ToolCall`) stays the canonical "thing the fan-out consumes" — `RoutedToolCall` is converted to `ToolCall` by `router.py` after validation.

## 3. `OpenAILLMClient` (`orchestrator/llm/amp_client.py`)

OpenAI is reached via the **WSO2 AMP AI Gateway** (`OPENAI_BASE_URL`), not the public OpenAI endpoint. The API key is sent in the header named by `OPENAI_API_HEADER` (default `api-key`). Observability is wired through `amp-instrumentation` + `traceloop-sdk` so every router/composer call is traced at the gateway.

```python
class OpenAILLMClient:
    def __init__(self, *, api_key: str, model: str, base_url: str, api_header: str,
                 timeout_s: float, max_output_tokens: int):
        # The tool catalogue is bound as OpenAI function schemas via .bind_tools(); the model
        # then returns structured tool_calls — there is no JSON parsing of the text content.
        self._router_llm   = ChatOpenAI(model=model, api_key=api_key, base_url=base_url,
                                        default_headers={api_header: api_key},
                                        temperature=0.0, max_tokens=max_output_tokens,
                                        max_retries=5)
        self._composer_llm = ChatOpenAI(model=model, api_key=api_key, base_url=base_url,
                                        default_headers={api_header: api_key},
                                        temperature=0.3, max_tokens=max_output_tokens,
                                        max_retries=5)
        self._timeout_s = timeout_s

    async def route(self, user_message, catalogue) -> list[RoutedToolCall]:
        sys = prompts.router_system(catalogue, today=date.today().isoformat())
        tools = prompts.tool_schemas(catalogue)            # function schemas, one per catalogue entry
        router = self._router_llm.bind_tools(tools)
        try:
            resp = await asyncio.wait_for(
                router.ainvoke([SystemMessage(sys), HumanMessage(user_message)]),
                timeout=self._timeout_s)
        except (asyncio.TimeoutError, Exception) as exc:   # langchain raises a zoo of exceptions
            raise LLMError(f"router call failed: {exc!r}") from exc
        # Read the structured tool_calls off the response — NO JSON parsing of resp.content.
        return [RoutedToolCall(agent_id=_agent_for(tc["name"]), tool_id=tc["name"], args=tc["args"])
                for tc in (resp.tool_calls or [])]

    async def compose(self, user_message, outcomes) -> str:
        sys = prompts.composer_system()
        body = prompts.render_outcomes(outcomes)
        try:
            resp = await asyncio.wait_for(
                self._composer_llm.ainvoke([SystemMessage(sys), HumanMessage(f"{body}\n\nUser said: {user_message}")]),
                timeout=self._timeout_s)
        except (asyncio.TimeoutError, Exception) as exc:
            raise LLMError(f"composer call failed: {exc!r}") from exc
        text = (resp.content or "").strip()
        if not text:
            raise LLMError("composer returned empty text")
        return text
```
- `ChatOpenAI.ainvoke` is async. We wrap with `asyncio.wait_for` for a hard timeout regardless of the client's internal timeout.
- The router uses OpenAI **function-calling**: `bind_tools()` injects the tool catalogue as function schemas and the model returns structured `tool_calls` (read straight off `resp.tool_calls`). There is **no JSON parsing** of the response content; the old `parse_router_output` function was removed.
- The OpenAI client **retries transient gateway 5xx** internally (`max_retries=5`) before any call is considered failed; only after the retries are exhausted does it fall back to the keyword router.
- `except Exception` is deliberate here (then re-raised as `LLMError`) — `langchain_openai` surfaces transport/quota/auth errors as various exception classes; we don't want any of them to escape into the chat handler. (Stage 8 security review: confirm we never log the exception with the API key in it — `repr(exc)` from langchain doesn't include the key, but we double-check.)

## 4. Prompts (`orchestrator/llm/prompts.py`)

### 4a. Router system prompt
The tool catalogue is **not** described in free text and the model is **not** asked to emit a JSON array. Instead the catalogue is injected as OpenAI **function schemas** via `ChatOpenAI.bind_tools()` (see `prompts.tool_schemas(catalogue)`), and the model returns structured `tool_calls`. The system prompt only carries the policy the schemas can't express:
```
You are the routing layer of an internal HR/IT employee assistant. Call the
tool(s) needed to fully satisfy the user's message, extracting each tool's
arguments from the message. Use only the tools provided to you (as functions) —
never invent a tool name. Call zero tools if the message maps to none (chit-chat,
off-topic, or a question no tool can answer).
Rules:
- Include only the argument names a tool's function schema declares. Omit args you
  can't determine from the message.
- Dates must be ISO format YYYY-MM-DD. Today is {today}.
- leave_type must be exactly one of: Annual Leave, Sick Leave, Personal Leave.
- If the message maps to more than one tool, call them in the order they should run.
```
### 4b. Reading the router output
There is **no** `parse_router_output` (it was removed). The model's structured `tool_calls` are read directly off the response's `tool_calls` attribute — each entry already carries the `name` (tool_id) and a validated `args` dict produced by OpenAI function-calling. The router maps each `tool_call` to a `RoutedToolCall` (resolving its `agent_id` from the tool_id), then hands the list to `_validate`. Zero `tool_calls` → `[]` (the model legitimately found nothing → keyword fallback). No JSON string parsing, no markdown-fence stripping, no `LLMError` on unparseable text — those failure modes no longer exist.

### 4c. Composer system prompt
```
You are the reply layer of an internal HR/IT employee assistant. Given the user's
message and the outcome of each tool that ran, write ONE short, friendly reply in
plain text — no markdown, no HTML, no code fences, no headings. Address the user as
"you", first person for the assistant.
Rules:
- Mention the outcome of every tool listed. Don't drop any.
- If a tool failed with error_id ERR-CIBA-005, say plainly that the user declined
  that action (and it wasn't done).
- If a tool failed with error_id ERR-AGENT-002, ask the user for the specific
  missing information (the reason text names it). Don't say "something went wrong".
- For any other failure, give a brief, non-technical apology and (if useful) what
  the user could try.
- State only facts present in the tool outputs. Never invent request IDs, balances,
  cubicle numbers, asset IDs, dates.
- Use a bullet list ("- ") only when listing 3 or more items; otherwise prose.
- Keep it under ~5 sentences for simple cases.
```
### 4d. `render_outcomes(outcomes) -> str`
One line per outcome:
- ok:  `Tool {tool_id} (success): {json.dumps(data, default=str)}`
- not ok: `Tool {tool_id} (failed): error_id={error_id} reason={reason!r}`
Capped: if `json.dumps(data)` exceeds ~2 KB, truncate with `… (truncated)` — keeps the composer prompt bounded (R1 latency, and avoids leaking a huge accidental blob).

## 5. `resolve_tool_calls(user_message, deps) -> list[ToolCall]` (`orchestrator/llm/router.py`)

```python
async def resolve_tool_calls(user_message: str, deps: ChatRouterDeps) -> list[ToolCall]:
    use_llm = deps.config.llm_fallback_mode == "llm" and deps.llm_client is not None
    if use_llm:
        catalogue = _build_catalogue(deps.agent_registry)        # from registry.llm_tool_list()
        try:
            routed = await deps.llm_client.route(user_message, catalogue)
        except LLMError as exc:
            logger.warning("llm_router_failed reason=%s falling_back_to_keyword", exc)
            routed = None
        if routed is not None:
            tool_calls = _validate(routed, deps)                 # drop unknowns; filter args
            if tool_calls:
                logger.info("llm_router_ok tools=%s", [tc.tool_id for tc in tool_calls])
                return tool_calls
            logger.info("llm_router_empty_or_all_invalid falling_back_to_keyword")
    return deps.keyword_router.route(user_message)
```
`_validate(routed, deps)`:
- For each `RoutedToolCall`:
  - `agent_id` must be in `deps.a2a_clients` → else drop + `logger.warning("llm_router_dropped_unknown_agent agent=%s tool=%s", ...)`.
  - look up the card for `agent_id`; `tool_id` must be in `{s.tool_id for s in card.skills}` → else drop + `logger.warning("llm_router_dropped_unknown_tool agent=%s tool=%s", ...)`.
  - `args`: keep only keys present in that skill's `args` list (defensive: strip hallucinated keys); ensure every value is a JSON scalar (str/int/float/bool) — drop non-scalars. (Floor/leave_id may be int or str; the dispatcher's `kwargs_builder` coerces.)
  - emit `ToolCall(agent_id=agent_id, tool_id=tool_id, args=filtered_args)`.
- No per-agent dedup (the LLM may legitimately pick two tools from one agent — the fan-out handles a list in order, one CIBA each).
- Return the list in the LLM's order.

`_build_catalogue(registry)`:
- `registry.llm_tool_list()` already flattens skills with `agent_id`/`tool_id`/`label`/`description`; map each to `ToolCatalogueEntry` adding `args` (the new `Skill.args`).

## 6. Composition wiring (`orchestrator/chat/routes.py`)

`_run_serial_fan_out` gains `user_message: str`. As it loops it builds **both**:
- `per_tool_outputs: list[str]` — exactly as today (`_render_result` / `_friendly_error` fragments) — this is the **fallback** reply.
- `outcomes: list[ToolOutcome]` — for the LLM composer. On a `ResultPayload`: `ToolOutcome(agent_id, tool_id, ok=True, data=result.data)`. On an `ErrorPayload` / CIBA-error / consent-denied / expired / unexpected: `ToolOutcome(agent_id, tool_id, ok=False, error_id=..., reason=...)`.

At the end:
```python
if per_tool_outputs:
    fallback_text = "\n\n".join(per_tool_outputs)
else:
    fallback_text = "I was unable to retrieve any results. Please try again."

use_llm = deps.config.llm_fallback_mode == "llm" and deps.llm_client is not None
if use_llm and outcomes:
    try:
        final_content = await deps.llm_client.compose(user_message, outcomes)
        logger.info("llm_composer_ok request_id=%s", request_id)
    except LLMError as exc:
        logger.warning("llm_composer_failed reason=%s falling_back request_id=%s", exc, request_id)
        final_content = fallback_text
else:
    final_content = fallback_text

await channel.publish(ChatMessageEvent(content=final_content, request_id=request_id))
```
(If `outcomes` is empty — e.g. the keyword/LLM router produced no tool calls — `post_chat` already handles that earlier with the "I don't know" `ChatMessageEvent`; `_run_serial_fan_out` is only called with a non-empty list. The `if not per_tool_outputs` branch is the existing belt-and-braces.)

`post_chat`:
```python
tool_calls = await resolve_tool_calls(body.message, deps)   # was: deps.keyword_router.route(body.message)
if not tool_calls:
    ... existing "I don't know" path ...
asyncio.create_task(_run_serial_fan_out(session, tool_calls, request_id, deps, user_message=body.message))
```

## 7. Config (`orchestrator/config.py`)

`from_env`:
```python
llm_fallback_mode = env.get("LLM_FALLBACK_MODE", "keyword").strip() or "keyword"
openai_api_key    = env.get("OPENAI_API_KEY", "").strip() or None
openai_model      = env.get("OPENAI_MODEL", "gpt-4.1").strip() or "gpt-4.1"
openai_base_url   = env.get("OPENAI_BASE_URL", "").strip() or None
openai_api_header = env.get("OPENAI_API_HEADER", "api-key").strip() or "api-key"
llm_timeout_s     = float(env.get("LLM_TIMEOUT_S", "8") or "8")
llm_max_output_tokens = int(env.get("LLM_MAX_OUTPUT_TOKENS", "512") or "512")
if llm_fallback_mode == "llm" and not openai_api_key:
    logger.warning("LLM_FALLBACK_MODE=llm but OPENAI_API_KEY is empty — running keyword-only.")
    # main.py will then build llm_client=None; resolve_tool_calls/compose_reply both no-op to keyword.
```
New `OrchestratorConfig` fields: `openai_model: str = "gpt-4.1"`, `openai_base_url`, `openai_api_header: str = "api-key"`, `llm_timeout_s: float = 8.0`, `llm_max_output_tokens: int = 512`. Existing: `llm_fallback_mode`, `openai_api_key`.

`main.py`:
```python
llm_client = None
if cfg.llm_fallback_mode == "llm" and cfg.openai_api_key:
    from orchestrator.llm.amp_client import OpenAILLMClient
    llm_client = OpenAILLMClient(api_key=cfg.openai_api_key, model=cfg.openai_model,
                                 base_url=cfg.openai_base_url, api_header=cfg.openai_api_header,
                                 timeout_s=cfg.llm_timeout_s, max_output_tokens=cfg.llm_max_output_tokens)
    logger.info("llm_client_enabled model=%s timeout_s=%.1f", cfg.openai_model, cfg.llm_timeout_s)
# ... ChatRouterDeps(..., llm_client=llm_client)
```
The `ChatOpenAI` import is **lazy** (inside the `if`) so the orchestrator still imports/starts fine if `langchain-openai` isn't installed and mode is `keyword` — useful for the test venv if it lacks the package. (Stage 10: confirm `.venv` has it; if not, `pip install` it.) OpenAI is reached via the WSO2 AMP AI Gateway (`OPENAI_BASE_URL`), with the key sent under `OPENAI_API_HEADER` (default `api-key`); observability uses `amp-instrumentation` + `traceloop-sdk`.

## 8. `apply_leave` chat tool (S5.1) — see [`sprint-5-stage-5-api-design.md`](sprint-5-stage-5-api-design.md) §2–§4 for the exact shapes. Summary:

- `hr_server/mcp/tools.py`: `ApplyLeaveArgs` / `ApplyLeaveResult` models; `POST /mcp/tools/apply_leave` handler (scope `hr_self_rest`) → `hr_service.apply_leave(claims.sub, _username_for(claims), "", body.leave_type, body.start_date, body.end_date, body.reason)`; map the service return: `{success:true, request_id}` → `ApplyLeaveResult(success=True, request_id=...)`; `{error, message}` → `ApplyLeaveResult(success=False, error=..., message=...)`. Add to `__all__`.
- `hr_agent/mcp/client.py`: `apply_leave(self, *, token_b, leave_type, start_date, end_date, reason="", request_id=None)`.
- `hr_agent/ciba/orchestrator.py`: `_REQUIRED_ARGS["hr.apply_leave"] = ["leave_type","start_date","end_date"]`; `_TOOL_REGISTRY["hr.apply_leave"] = ("Apply for leave on your behalf", "apply_leave", lambda a: {...four args...}, "openid hr_self_rest")`.
- Card: `hr.apply_leave` skill in `hr_agent_valid.json` (`scope: "hr_self_rest"`, `required_scopes: ["hr_self_rest"]`, `args: ["leave_type","start_date","end_date","reason"]`).
- `_render_result` (orchestrator): add an `hr.apply_leave` case for the keyword-fallback reply — `{success:true, request_id}` → "Your {leave_type? from?}… leave request has been submitted (ref {request_id}) and is pending approval."; `{success:false}` → `data.get("message")`. (The LLM composer handles the LLM-mode reply; this is the fallback.)

## 9. Agent-card / dispatcher consistency (S5.2)

Expand `hr_agent_valid.json` skills to cover every `_TOOL_REGISTRY` key: `hr.read_policy`, `hr.read_balance`, `hr.read_history`, `hr.approve_leave`, `hr.reject_leave`, `hr.apply_leave`, `hr.cubicle_summary`, `hr.cubicle_list_floor`, `hr.cubicle_assign`, `hr.lookup_employee`, `hr.cubicle_lookup_self` — each with `scope`, `required_scopes`, and `args`. Same for `it_agent_valid.json` (`it.list_available_assets`, `it.get_my_assets`, `it.issue_asset`, plus whatever else its dispatcher serves). Stage 10 test `test_agent_card_matches_dispatcher_registry`: for each agent, `{s.tool_id for s in card.skills} == set(_TOOL_REGISTRY)` — fails loudly on drift.

## 10. Failure modes & the fallback truth table

| Situation | Routing | Composition | User sees |
|---|---|---|---|
| mode=keyword (any reason incl. no key) | keyword router | `_render_result` join | Sprint-4 behaviour exactly |
| mode=llm, OpenAI OK, valid tools | LLM tools | LLM reply | natural NL chat |
| mode=llm, OpenAI OK, LLM returns no tool_calls/all-invalid | keyword router | LLM reply (composer still runs on whatever the keyword tools produced) | keyword tools, LLM-worded reply |
| mode=llm, router call fails (timeout/AMP gateway 5xx after retries/net) | keyword router | LLM reply *if composer still works*, else `_render_result` join | works; reply maybe degraded |
| mode=llm, router OK, composer call fails | LLM tools | `_render_result` join | right tools, Sprint-4-worded reply |
| mode=llm, both calls fail | keyword router | `_render_result` join | exactly Sprint-4 behaviour |
| LLM picks a tool the user's role can't scope (e.g. employee → `hr.cubicle_assign`) | tool runs to CIBA | — | IS denies CIBA → "you're not authorised for that" (no escalation) |

## 11. Latency budget (R1)

- Router call: ≈0.4–1.5 s (`gpt-4.1`, `temperature=0`, ≤512 out tokens, prompt ≈1–2 KB; function-calling tool schemas).
- Composer call: ≈0.4–1.5 s.
- Total added vs keyword mode: ≈1–3 s per chat turn. Acceptable for the demo. Mitigations already in: `max_output_tokens` cap, 8 s hard timeout each → fallback, transient-5xx retries (`max_retries=5`) absorbed inside the timeout. Not pursued in S5: a single combined router+compose call (the two have different shapes and the composer needs the *tool results* which only exist after the fan-out — can't merge without restructuring; revisit if latency bites).

## 12. What this design deliberately does NOT do

- It does not give the LLM the user's `sub`, any token, any secret, or the IS config — only the message + the tool catalogue (router) / the message + tool results (composer).
- It does not let the LLM choose scopes, write consent copy, or emit HTML.
- It does not change the A2A / CIBA / MCP wire protocols or the SSE events.
- It does not remove or weaken the keyword router, `_render_result`, or any Sprint 1–4 behaviour — all of it is the fallback floor.
- It does not add multi-turn dialogue state. (Chat-history replay is a stretch goal — if done, it's a `Session.chat_history: deque[(role, text)]` capped at N, rendered into both prompts; not in the S5 commitment.)
