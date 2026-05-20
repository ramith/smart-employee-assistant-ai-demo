# Sprint 5 — Retrospective (M5: LLM-driven chat orchestration)

**Date:** 2026-05-11
**Branch:** `sprint-5-build` (cut from `sprint-4-build` @ `05e1208`)
**Outcome:** build complete; automated gate green (1058 tests / 64 files strict-mode); manual gate runbook prepared ([`sprint-5-stage-11-manual-gate.md`](sprint-5-stage-11-manual-gate.md)); demo live (`llm_client_enabled model=gpt-4.1`).

---

## 1. What we set out to do

Flip the orchestrator's chat from a keyword router to an LLM (OpenAI `gpt-4.1`) for routing *and* reply composition, keep the keyword router as the automatic fallback, and add the missing `apply_leave` chat tool so natural-language "apply for leave" finally works end-to-end (UC-13 had promised it since Sprint 1 but the LLM that would parse `{leave_type, start_date, end_date}` was never built).

## 2. What went well

- **The "LLM = router/composer, never an authority" framing held up under independent review.** All three Stage-8 agents (architect / code / security) said GO-WITH-CHANGES with no NO-GO. The security agent walked the prompt-injection attack end to end and confirmed there's no escalation path: out-of-catalogue tools are dropped in `_validate`; surviving tools hit the *unchanged* Sprint-1..4 fan-out; each tool's CIBA scope is server-fixed in the dispatcher `_TOOL_REGISTRY`; WSO2 IS denies any scope the user's role lacks. Crucially this backstop was *already* in place (keyword-mode rules aren't role-gated either) — S5 just added one more caller of it.
- **Tiny blast radius in the chat hot path.** `chat/routes.py` changed in three coherent ways: `post_chat` calls `resolve_tool_calls` instead of `keyword_router.route`; `_run_serial_fan_out` builds a parallel `outcomes` list via a `_record()` helper and calls `compose_reply` at the end (wrapped so a terminal `chat_message` is structurally guaranteed); `_render_result` `strip_sensitive`s its input. The SSE events, CIBA polling, `terminating` fence, logout-cascade barrier — untouched. All 22 existing `test_routes.py` cases passed unmodified.
- **Lazy-import discipline paid off.** `langchain-openai` isn't in the test venv; `amp_client.py` is the only module that imports it, and it's imported lazily by `main.py` only in llm-mode-with-a-key. The whole strict suite (1058 tests) runs with no langchain installed, and `test_no_langchain.py` enforces this.
- **The agent-card ⇔ dispatcher consistency contract** (every routable tool carded, with `args` matching the `kwargs_builder` keys 1:1, `hr.lookup_employee` off-card on purpose) caught what would otherwise have been silent `ERR-AGENT-002` failures, and gives us a single failure point if the two ever drift again (`test_card_dispatcher_consistency.py`).
- **S5.1 (apply_leave) shipped independently first** — zero LLM dependency, fully testable on its own, closes the long-open UC-13 gap. If S5 had stalled after S5.1, keyword mode would have been unchanged and we'd still have a usable `apply_leave` MCP tool.
- **The fallback is real, not aspirational.** Every OpenAI / AMP gateway failure mode (unreachable, rate-limited, auth, malformed output, langchain not installed) degrades to exactly the Sprint-4 behaviour — keyword routing + `_render_result` concatenation — never a hard error; the client first retries transient gateway 5xx (`max_retries=5`). The fallback truth table in the tech arch enumerated all of them and the unit tests cover each branch.
- **The gated process surfaced the right things at the right time.** Stage 8's three findings that actually mattered — (a) tool results carry IS `sub`s that would have leaked to OpenAI, (b) LLM response post-processing could escape the `except` net and hang the SPA, (c) the card/dispatcher arg-name contract was under-specified — were all caught *before* a line of implementation was written, and folded into the slices via the reconciliation doc.

## 3. What was bumpy / what we'd do differently

- **I almost committed the real API key as a test sentinel.** `test_redaction.py` initially used the user's actual OpenAI key (`sk-…`) as the "syntactically-shaped" sentinel — caught by the OpenAI API-key leak grep before pushing, swapped to an obviously-fake repeated-char string. **Lesson: a "shaped sentinel" must be obviously fake** (repeated chars, not a plausible-looking random string), and run the leak grep *as part of the commit ritual*, not just at sign-off. Consider promoting it to a pre-commit hook (it's in the retro follow-ups).
- **`langchain-openai` / `langchain` are still `>=`-pinned** (inherited from the milestone plan). For a demo that's tolerable, but a future image rebuild can pull an un-reviewed newer version of a large dep tree. Follow-up: pin exact + add a lockfile; we already dropped the genuinely-unused `langchain-mcp-adapters`.
- **No rate limit on `/api/chat`** — each llm-mode turn is two OpenAI calls; an authenticated user spamming the chat burns quota/bill. Mitigated for the controlled demo by: login-gated, serial demo, `gpt-4.1` model, 512-token cap, 8 s timeout, and the rate-limited→keyword-fallback floor (so quota exhaustion is self-limiting, just costly). Documented as a known limitation (`sprint-5.md` §8 R7); a per-`Session` minimum chat interval is a cheap future add.
- **The Stage-8 code/security review agents couldn't write their doc files** (read-only tooling) — they returned 5000-word inline dumps that I had to condense into the `sprint-5-stage-8-*.md` docs by hand. Worked, but next time brief the agents to keep the inline report tight and accept that the parent writes the file. (The architect agent *did* have Write — inconsistent tooling across the same `*-reviewer` family.)
- **Latency is real (~1–3 s added per chat turn — two OpenAI round-trips).** Mitigated as planned; we deliberately didn't pursue a combined router+compose call (the composer needs the post-fan-out tool results, so it can't be merged without restructuring). If latency bites in the demo, revisit.
- **Two big slices got merged (S5.2 router + S5.3 composer landed in one commit `1698dfb`).** The `chat/routes.py` refactor was genuinely one coherent change (the `outcomes` list + `compose_reply` + the outer guard), so splitting it would've been artificial — but the commit is large (24 files, +1513 lines). Acceptable here; in general, prefer the smaller slice when the seam is clean.

## 4. Carry-overs / follow-ups (not blocking M5)

| # | Item | Notes |
|---|------|-------|
| C1 | Pin `langchain` + `langchain-openai` to exact versions; add a lockfile for the orchestrator image. | S5 dropped `langchain-mcp-adapters`. |
| C2 | Pre-commit hook grepping staged content for an OpenAI API key shape (`sk-…`). | Catch a real key before it's committed, not after. |
| C3 | Optional per-`Session` minimum chat interval (~2 s) in `post_chat`. | Cheap DoS/cost hardening; document-only for now. |
| C4 | Chat-history replay (last N turns into the router/composer prompts). | Stretch goal that didn't make S5; would make follow-ups ("what about June instead?") work. Needs a `Session.chat_history` ring buffer. |
| C5 | `pytest -m live_llm` smoke into a manual/nightly CI lane (not the strict suite). | Today it's documented but never run automatically. |
| C6 | `it_service` should resolve `employee_id`→display username for `it.issue_asset` results. | Today the sub is stripped and the reply says "the requested employee"; a username would read better. |
| C7 | Sprint 4 still has an open manual gate (`sprint-4-stage-11-manual-gate.md`) + a Sprint-4 sign-off doc to write. | S5 was started before S4 closed; reconcile when convenient. |

## 5. Process note (for the milestone log)

S5 ran the standard gated flow: Stage 1 PM review → Stage 2 BA (UC-17 + UC-13 update) → Stage 3 binding plan (`sprint-5.md`) → Stages 4/5/6 (UX / API / tech arch) → Stage 7 slice plan → Stage 8 parallel architect/code/security review (all GO-WITH-CHANGES) + reconciliation → Stage 9 implementation (S5.1 → S5.2+S5.3 → S5.4) → Stage 10 test-coverage doc → Stage 11 manual-gate runbook → Stage 12 (this retro + the conditional sign-off). The reconciliation pattern (fold Stage-8 findings into the slices rather than a full Stage-6.5 pass) worked because nothing was a NO-GO.
