# Sprint 5 — Stage 8: Code Review

**Reviewer:** code-reviewer agent
**Date:** 2026-05-11
**Scope:** Stage 3/5/6/7 plan docs, verified against `orchestrator/chat/routes.py`, `keyword_fallback.py`, `config.py`, `main.py`, `agent_registry/cards.py`, `common/a2a/agent_card.py`, `hr_agent/ciba/orchestrator.py`, `hr_agent/mcp/client.py`, `hr_server/mcp/tools.py`, `it_agent/ciba/orchestrator.py`.

---

## 1. Verdict — **GO-WITH-CHANGES**

The plan is minimal, well-sequenced, and the security invariant survives. Two blocking findings (both code-hygiene, not architecture); the rest is polish folded into the slices. No Stage-6.5 reconciliation pass needed.

## 2. Blocking findings

### F-01 — LLM response post-processing escapes the `except` net → can leave the SPA hung
- **What:** Tech-arch §3's `GeminiLLMClient.route/compose` wrap only `await asyncio.wait_for(self._llm.ainvoke(...))` in `try/except → raise LLMError`. The next lines — `parse_router_output(resp.content)` / `(resp.content or "").strip()` — run *after* the `except`. `ChatGoogleGenerativeAI.ainvoke` does not always return `resp.content` as `str` (newer LangChain returns a list of content blocks for some responses) → `AttributeError`, not `LLMError`. `resolve_tool_calls` / `compose_reply` only catch `LLMError`, so it escapes. `resolve_tool_calls` is `await`ed in `post_chat` → 500 on `POST /api/chat` (not graceful keyword fallback — defeats exit-criterion §6.5). `compose_reply` is called at the *end* of `_run_serial_fan_out`, which has **no outer `try/except`** → an escape there → no terminal `ChatMessageEvent` → SPA hangs on the spinner.
- **Fix:** (1) Pull the post-processing *inside* the `try` in both methods (or coerce: `text = resp.content if isinstance(resp.content, str) else str(resp.content)`). (2) In `resolve_tool_calls` / `compose_reply` catch `(LLMError, Exception)  # noqa: BLE001` — deliberate broad net at the chat boundary, mirroring the existing `except (A2AError, Exception)  # noqa: BLE001` in `chat/routes.py` — log + fall back. (3) Wrap the final `compose_reply` + `channel.publish(ChatMessageEvent(...))` in `_run_serial_fan_out` in its own `try/except` so "always emits a terminal `chat_message`" is structurally guaranteed.
- **Lands in:** S5.2 (GeminiLLMClient.route, resolve_tool_calls), S5.3 (compose, compose_reply, the `_run_serial_fan_out` outer guard).

### F-02 — card `args[]` ⇔ dispatcher `kwargs_builder` arg-name contract under-specified
- **What:** `_validate` (tech-arch §5) keeps only arg keys present in the card skill's `args[]`. But the dispatcher `kwargs_builder` lambdas read specific keys: `hr.lookup_employee` → `args.get("name")`; `hr.cubicle_assign` → `cubicle_id`/`employee_username`/`employee_email`; `hr.cubicle_list_floor` → `floor`; `it.issue_asset` → `asset_id`/`employee_id`; `hr.approve_leave`/`hr.reject_leave` → `leave_id`(/`reason`); `hr.apply_leave` → `leave_type`/`start_date`/`end_date`/`reason`. The Stage-10 consistency test as planned only checks `{s.tool_id for s in card.skills} == set(_TOOL_REGISTRY)` — a card listing the wrong arg names passes, passes `_validate`, then silently drops the LLM's argument → `ERR-AGENT-002` for a correctly-routed tool.
- **Fix:** Enumerate the exact `args[]` per skill in the card-expansion table (1:1 with the lambda keys). Strengthen the Stage-10 test to also assert, per tool, that the card's `args` set ⊇ the keys the `kwargs_builder` reads. Trap watch: `hr.lookup_employee` (`name` vs `username_or_email`), `it.issue_asset` (`employee_id` — recall the Sprint-4 `employee_id`-rename churn).
- **Lands in:** S5.2 (card expansion, the consistency test).

## 3. Non-blocking findings (folded into slices)

- **F-03 — `/api/chat` latency profile changes.** `post_chat` now does a Gemini round-trip (≈0.4–1.5 s typical, up to `LLM_TIMEOUT_S`=8 s on a stall) *before* returning `ChatAck`/spawning the fan-out. Handled by the S5.4 "Thinking…" affordance; update the `post_chat` docstring (steps 3–5) + note `LLM_TIMEOUT_S` is the worst-case added latency. → S5.2 (docstring), S5.4 (UX).
- **F-04 — `render_outcomes` can leak `sub` into the composer prompt.** `hr.lookup_employee` returns `{found, username, email, sub}` (IS UUID, F-12-forbidden on chat/UI); if carded it's LLM-routable and its `data` lands in the composer prompt. Fix: don't list `hr.lookup_employee` as an LLM-routable skill (it's an agent-internal helper for `assign_cubicle`, never a direct chat intent — keep it out of the card's skills, document the exception in the consistency test); plus a defensive key-denylist in `render_outcomes`. (Overlaps Security F-1 — same fix.) → S5.2/S5.3.
- **F-05 — `(asyncio.TimeoutError, Exception)` is redundant** (in 3.11+ `asyncio.TimeoutError is builtins.TimeoutError ⊂ Exception`). The deliberate broad `except Exception → raise LLMError` is correct here (langchain raises a zoo). Cosmetic: `except Exception as exc:  # noqa: BLE001 — langchain raises a zoo; any failure → LLMError → fallback`. → S5.2.
- **F-06 — lazy import is correct; add a guard test.** `gemini.py` must be the *only* module importing `langchain_google_genai`; `routes.py`/`router.py`/`composer.py`/`client.py`/`prompts.py` import only from `orchestrator.llm.client` + stdlib. Add Stage-10 `test_llm_router_importable_without_langchain` (monkeypatch `sys.modules['langchain_google_genai']=None`, import `orchestrator.llm.router`). (`langchain-google-genai` *is* in `orchestrator/requirements.txt` already, so the prod image has it; the test venv may not.) → S5.2 + Stage-10.
- **F-07 — `apply_leave` MCP handler matches the existing pattern; two nits.** It's a faithful clone of `assign_cubicle`/`approve_leave`; `hr_service.apply_leave` returns exactly `{success, request_id}` / `{error, message}` — 1:1 map to `ApplyLeaveResult`. Nits: (1) the *older* `hr_server/mcp_server/server.py:233` `apply_leave` tool uses scope `hr_self_mcp` + arg name `type` — that's a **different surface** (not the live `build_hr_mcp_router`); the new route in `hr_server/mcp/tools.py` uses `hr_self_rest` + `leave_type` and does not touch `mcp_server/server.py`. (2) Don't forget `ApplyLeaveArgs`, `ApplyLeaveResult` in `__all__`. → S5.1.
- **F-08 — `HRMcpClient.apply_leave` signature is consistent** with `assign_cubicle`/`reject_leave`. No change. → S5.1.
- **F-09 — `FakeLLMClient` is sufficient.** Covers all routing/composition/fallback branches without network. The one branch not exercisable through it ("router OK but `parse_router_output` raises `LLMError` on unparseable" — internal to `GeminiLLMClient`) is unit-tested by calling `parse_router_output` directly (already in the Stage-7 test list). Put `FakeLLMClient` in a shared location (`tests/orchestrator/llm/conftest.py` or `tests/fakes.py`) so the end-to-end chat-route tests reuse it. → Stage-10.
- **F-10 — config field append + `_parse_float` parity.** New fields go *after* all required `OrchestratorConfig` fields (all current defaulted fields are already at the end — fine to append). `llm_timeout_s = float(env.get("LLM_TIMEOUT_S", "8") or "8")` `ValueError`s on a non-numeric env value — either add a `_parse_float(value, name, default)` helper for parity with the existing `_parse_*` helpers or accept the loud-crash-at-boot. Update the `OrchestratorConfig` docstring Attributes block. → S5.2.
- **F-11 — parallel `outcomes` list is safe** — adding it alongside `per_tool_outputs` doesn't disturb the SSE events / `_friendly_error` mapping / CIBA polling / `pending_ciba` cleanup / `cancelled_ack` barrier / `terminating` fence / `last_logout_reason` one-shot, and the "no tool calls → I don't know" path is in `post_chat` *before* the fan-out (so `resolve_tool_calls` → `[]` hits exactly that branch). One thing to cover: the two early-`continue` branches (agent not in registry / no A2A client) must also emit a `ToolOutcome(ok=False, error_id="ERR-AGENT-002", reason="agent_not_registered")` so the composer mentions the dropped tool. → S5.3.

## 4. Blocking-vs-non-blocking summary

| ID | Blocking? | Lands in |
|---|---|---|
| F-01 LLM post-processing escapes `except` + no outer guard on `_run_serial_fan_out` | **BLOCKING** | S5.2 + S5.3 |
| F-02 card `args[]` ⇔ `kwargs_builder` key contract under-specified | **BLOCKING** | S5.2 |
| F-03 `/api/chat` latency doc | non-blocking | S5.2/S5.4 |
| F-04 `render_outcomes` `sub` leak via `hr.lookup_employee` | non-blocking (also Security F-1) | S5.2/S5.3 |
| F-05 cosmetic `except` redundancy | non-blocking | S5.2 |
| F-06 importable-without-langchain test | non-blocking | S5.2 + Stage-10 |
| F-07 `apply_leave` `__all__` + don't confuse with `mcp_server/server.py` | non-blocking | S5.1 |
| F-08 client signature consistent | none | — |
| F-09 `FakeLLMClient` location | non-blocking | Stage-10 |
| F-10 config field append + `_parse_float` | non-blocking | S5.2 |
| F-11 cover the two early-`continue` `outcomes` branches | non-blocking | S5.3 |

No over-engineering — two Gemini round-trips, token caps, 8 s timeout→fallback, Protocol-behind-a-fake, keyword floor unchanged: all demo-appropriate. Deferral list (no multi-turn FSM, no token streaming, no LangChain agent executor, no `langchain-mcp-adapters`) correctly drawn.
