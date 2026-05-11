# Sprint 5 — Stage 8 Reconciliation

**Date:** 2026-05-11
**Status:** all three Stage-8 reviews returned **GO-WITH-CHANGES** (no NO-GO) → no full Stage-6.5 reconciliation pass; this doc folds the findings into the slices and **amends** `sprint-5.md` §2/§6/§8, `sprint-5-tech-arch.md`, and `sprint-5-stage-7-slice-plan.md`. Where this doc disagrees with those, this doc wins.

Reviews: [`sprint-5-stage-8-architect-review.md`](sprint-5-stage-8-architect-review.md) · [`sprint-5-stage-8-code-review.md`](sprint-5-stage-8-code-review.md) · [`sprint-5-stage-8-security-audit.md`](sprint-5-stage-8-security-audit.md).

---

## 1. Verdicts

| Reviewer | Verdict | Blocking findings |
|---|---|---|
| architect-reviewer | GO-WITH-CHANGES | F-1 (`_REQUIRED_ARGS` checks presence not validity — doc + Stage-11 test), F-2 (`OrchestratorConfig` frozen+slots — explicit 3-place edit + no-key-no-crash test) |
| code-reviewer | GO-WITH-CHANGES | F-01 (LLM post-processing escapes `except`; `_run_serial_fan_out` no outer guard), F-02 (card `args[]` ⇔ `kwargs_builder` keys 1:1 + strengthen Stage-10 test) |
| security-auditor | GO-WITH-CHANGES | F-1 (`hr.lookup_employee` `sub` → composer prompt), F-2 (`it.issue_asset` two `sub`s → composer prompt). F-3 (`.env` gitignore) → **already resolved** (`.gitignore:75 **/.env`). |

## 2. Amendments to `sprint-5.md` §2 (the security invariant)

Add clause **2.7 — Tool results that reach a prompt or a reply must be display-safe.** Before a tool's result becomes a `ToolOutcome.data` (fed to the LLM composer prompt) or a `_render_result` fragment, the orchestrator strips a key denylist: `sub`, any `*_sub` (`assigned_to_sub`, `reviewed_by_sub`, `user_sub`, …), `employee_id`/`reviewer_sub`/`issued_by` when the value is UUID-shaped, and anything containing `token` or `secret`. Reason: several MCP tool results carry the IS subject UUID for internal joins (`LookupEmployeeResult.sub`, `IssueAssetResult.{employee_id, issued_by}`) which F-12 forbids on the chat/UI surface — and the composer path additionally crosses the trust boundary to Google. Plus: **`hr.lookup_employee` is NOT an LLM-routable card skill** (it's an agent-internal helper for `assign_cubicle`'s `login_hint` resolution, never a direct chat intent) — it stays in the `hr_agent` dispatcher `_TOOL_REGISTRY` but is *omitted from `hr_agent_valid.json`'s `skills[]`*; the Stage-10 card⇔registry consistency test treats it as a documented exception (registry ⊇ card-skills, with `hr.lookup_employee` on the allowed-extra list).

Add clause **2.8 — The Gemini API key never reaches a log line.** `GeminiLLMClient` logs `type(exc).__name__` + `str(exc)[:200]` on failure, never `repr(exc)` or the request object. `common/logging/redaction.py` gains a pattern `AIza[0-9A-Za-z\-_]{35}` → `<REDACTED_API_KEY>`. The repo-wide leak check is `git grep -nP 'AIza[0-9A-Za-z_-]{35}'` (the full 39-char key shape — bare `AIza` has false positives: this doc and the others legitimately mention the prefix in prose).

Strengthen **2.x — the consent widget's named target is the last line of defence for write tools with fabricated args.** `_REQUIRED_ARGS` only checks arg *presence*, not that the named target exists — so the LLM (or an injection) can route `hr.cubicle_assign` / `hr.approve_leave` / `it.issue_asset` with a fabricated cubicle/leave-id/recipient and it reaches CIBA. That's not a scope escalation (IS still gates by role), and the MCP tool's business validation rejects a non-existent target — but the operator's defence is reading the consent widget's `action_text` ("Assign cubicle C-099 to everyone") before clicking Approve. Documented invariant; Stage-11 adds a "fabricated target → operator declines → no write" case and a "markup/control chars in the recipient arg → consent widget + audit line stay clean" case (the F-08 sanitiser now sees an LLM-sourced arg).

## 3. Amendments to `sprint-5.md` §6 (exit criteria) — add:

- **§6.9** — No tool result reaching a Gemini prompt or a chat reply contains a `sub`/UUID/token value (Stage-10: `test_render_outcomes_strips_sensitive_keys` over every tool's representative result; Stage-11: `hr.lookup_employee`-via-injection → no `sub`-shaped value in the SSE `chat_message` content or any captured Gemini request).
- **§6.10** — A forced `ChatGoogleGenerativeAI` auth/transport error never writes the API key to a log record (Stage-10 test with a sentinel fake key); `common/logging/redaction.py` redacts `AIza…`.
- **§6.11** — `orchestrator/llm/router.py` (and `composer.py`, `client.py`, `prompts.py`, `chat/routes.py`) import successfully with `langchain_google_genai` *uninstalled* (lazy-import discipline — Stage-10 `test_llm_router_importable_without_langchain`).
- **§6.12** — Card `skills[].args` ⊇ the keys each tool's dispatcher `kwargs_builder` reads, for every tool, for both agents (Stage-10 `test_agent_card_args_match_dispatcher` — strengthens the planned tool-id-set consistency test).
- **§6.13** — `OrchestratorConfig.from_env` with `LLM_FALLBACK_MODE=llm` and no `GEMINI_API_KEY` *warns and runs keyword-only* — does not crash (Stage-10 `test_config_llm_mode_without_key_does_not_crash`).
- Refine **§6.6**: `git grep -nP 'AIza[0-9A-Za-z_-]{35}'` over tracked files returns nothing (full key shape).

## 4. Amendments to `sprint-5.md` §8 (risks) — add:

- **R7 — cost / DoS via chat spam.** No per-`/api/chat` rate limit; each `llm`-mode turn = 2 Gemini calls. Mitigated by: login-gated, serial demo, `gemini-2.5-flash`, `max_output_tokens=512`, 8 s timeout, and the keyword-fallback floor (a Gemini 429 degrades routing to keyword — quota exhaustion is self-limiting, just costly). Accepted for the controlled demo walkthrough. Optional cheap hardening: a per-`Session` minimum chat interval (≈2 s) in `post_chat`. Documented limitation, not a blocker.
- **R8 — `langchain` / `langchain-google-genai` floor-pinned.** Pin both to exact (or tight `~=`) versions for the S5 build; record the versions here once chosen; drop `langchain-mcp-adapters` from `orchestrator/requirements.txt` (S5 confirms it's unused). Done in S5.2.
- **R9 — composed reply rendered as text only.** The SPA must never `innerHTML`/markdown-render the composed reply (it's `textContent` today). Stage-10/11: a reply payload with `</script>`, ` `, `{` round-trips intact. Documented limitation.

## 5. Amendments to `sprint-5-tech-arch.md`:

- §1: `Skill.args` is added to **`common/a2a/agent_card.py`** (where the `Skill` Pydantic model actually lives), not `orchestrator/agent_registry/cards.py`; `cards.py`'s `llm_tool_list()` surfaces it.
- §3 (`GeminiLLMClient`): the response post-processing (`parse_router_output(resp.content)` / `(resp.content or "").strip()`) goes **inside** the `try` block (or coerce `resp.content` to `str` first: `content = resp.content if isinstance(resp.content, str) else str(resp.content)`). The `except` is `except Exception as exc:  # noqa: BLE001` (drop the redundant `asyncio.TimeoutError`; in 3.11+ it's a subclass). Log `type(exc).__name__` + `str(exc)[:200]`, never `repr(exc)`/the request object.
- §5 (`resolve_tool_calls`) / new §5b (`compose_reply`): both catch `(LLMError, Exception)  # noqa: BLE001` (broad chat-boundary net, mirroring `chat/routes.py`'s existing `except (A2AError, Exception)`), log, fall back. `_validate` also drops `hr.lookup_employee` if it ever appears (defence-in-depth — it shouldn't, since it's off the card).
- §4d (`render_outcomes`): strip the §2.7 key denylist from each `data` dict before `json.dumps`; recursive (nested dicts/lists). Same strip applied in `orchestrator/chat/routes.py:_render_result`'s generic-fallback branch.
- §6 (composition wiring): `_run_serial_fan_out` wraps the final `final_content = await compose_reply(...)` **and** the `channel.publish(ChatMessageEvent(...))` in its own `try/except (Exception)  # noqa: BLE001` → on any escape, publish `ChatMessageEvent(content=fallback_text)` so a terminal `chat_message` is *structurally guaranteed*. Also: the two early-`continue` branches (agent not in registry / no A2A client) emit a `ToolOutcome(ok=False, error_id="ERR-AGENT-002", reason="agent_not_registered")` so the composer mentions the dropped tool.
- §7 (config): be explicit — `OrchestratorConfig` is `@dataclass(frozen=True, slots=True)`, so each new field touches three places (the class-body declaration, the `from_env` parse, and the `cls(...)` kwargs at the bottom of `from_env`) or `from_env` raises `TypeError` — that contradicts the "warn don't crash" promise. New fields go *after* all current fields. `llm_fallback_mode = env.get("LLM_FALLBACK_MODE","keyword").strip() or "keyword"`. Use a `_parse_float` helper for `LLM_TIMEOUT_S` (parity with the existing `_parse_*` helpers), or accept a loud boot-time crash on a non-numeric value. Update the `OrchestratorConfig` docstring Attributes block.
- §8 (`apply_leave`): note explicitly — the *older* `hr_server/mcp_server/server.py` `apply_leave` tool (scope `hr_self_mcp`, arg `type`) is a **different surface** and stays untouched; the new route is in `hr_server/mcp/tools.py` (the live `build_hr_mcp_router`), scope `hr_self_rest`, arg `leave_type`. `ApplyLeaveArgs`, `ApplyLeaveResult` added to `__all__`.

## 6. Amendments to `sprint-5-stage-7-slice-plan.md`:

- **S5.1** — also add an `hr.apply_leave` skill to `hr_agent_valid.json` *with explicit `args: ["leave_type","start_date","end_date","reason"]`*; the `Skill.args` field is wired in S5.2 but the JSON can carry it now (loader ignores unknown keys).
- **S5.2** — additionally: (a) add `Skill.args: tuple[str,...] = ()` to `common/a2a/agent_card.py`; expand both cards so `skills[]` covers every dispatcher `_TOOL_REGISTRY` key *except* `hr.lookup_employee`, each with `args` matching its `kwargs_builder` keys 1:1; (b) `OrchestratorConfig` 3-place edit + `_parse_float` + `or "keyword"`; named test `test_config_llm_mode_without_key_does_not_crash`; (c) `GeminiLLMClient` exception hygiene per §5 above + the `AIza…` redaction pattern in `common/logging/redaction.py`; (d) `resolve_tool_calls` catches `(LLMError, Exception)`; (e) `post_chat` docstring updated for the inline router call (+ `LLM_TIMEOUT_S` worst-case latency note); (f) `test_llm_router_importable_without_langchain`; (g) pin `langchain`/`langchain-google-genai` exactly + drop `langchain-mcp-adapters` from `orchestrator/requirements.txt`; (h) `test_agent_card_args_match_dispatcher` (strengthened consistency test).
- **S5.3** — additionally: (a) `render_outcomes` + `_render_result` generic fallback strip the §2.7 key denylist; `test_render_outcomes_strips_sensitive_keys`; (b) `_run_serial_fan_out` outer `try/except` around final compose+publish; (c) the two early-`continue` branches emit `ToolOutcome`s; (d) `compose_reply` catches `(LLMError, Exception)`.
- **S5.4** — additionally: (a) optional per-`Session` chat min-interval in `post_chat` (or document R7 only); (b) Stage-11 runbook items: prompt-injection (4 strengthenings from the security audit §4), fabricated-target-decline, markup-in-recipient-arg, kill-Gemini-fallback, reply-with-markup round-trip; (c) `git grep -nP 'AIza[0-9A-Za-z_-]{35}'` cross-cutting check (full key shape).

## 7. Net effect on the build

No new slices; no design rethink. ~12 small additions distributed across S5.1–S5.4 + Stage-10/11, all enumerated above. The headline security claim ("LLM never escalates privilege") was confirmed sound by all three reviewers; the fixes harden the *data-exposure* leg (don't leak `sub`s to Gemini), the *resilience* leg (no escaped LLM exception can hang the SPA), and the *operability* leg (config doesn't crash on a missing key; the key never hits a log). Proceed to Stage 9 (implementation), S5.1 first.
