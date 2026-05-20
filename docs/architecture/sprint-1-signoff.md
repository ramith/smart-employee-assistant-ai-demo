# Sprint 1 — M1 Sign-off (Stage 10)

**Date:** 2026-05-07
**Sprint owner:** Ramith Jayasinghe
**Status:** **SIGNED OFF** with two carries explicitly accepted into Sprint 2 Wave 1.

---

## §1. Decision

Sprint 1 is closed. M1 milestone is met for the purposes of the POC demo. Sprint 2 may begin.

---

## §2. Deliverables — D1.1..D1.7

| ID | Deliverable | Verdict | Evidence |
|---|---|---|---|
| D1.1 | User can log in via SPA; orchestrator session has token-A | ✓ PASS | Browser walkthrough 2026-05-07 17:55 — `auth_exchange_success` in orchestrator log; SPA app shell shown |
| D1.2 | User chat triggers specialist routing (HR or IT) | ✓ PASS | `/api/chat` 200 → keyword router → `hr_agent` / `it_agent` selected (LLM mode deferred per F-14 / Sprint 2) |
| D1.3 | Specialist initiates CIBA; auth_url reaches Consent Widget | ✓ PASS | `ciba_initiated` log line + AWAITING_APPROVAL widget rendered in browser |
| D1.4 | User Approves; specialist receives OBO token within polling budget | ✓ PASS | `ciba_poll_token_issued` at poll_count=2 (HR) and poll_count=4 (HR), poll_count=2 (IT) — well within 240s budget |
| D1.5 | Specialist calls MCP backend; MCP returns canned data; A2A response | ✓ PASS | `POST /mcp/tools/get_leave_balance HTTP/1.1 200 OK` and `/mcp/tools/list_available_assets 200 OK` |
| D1.6 | Orchestrator renders user-facing answer | ✓ PASS — keyword/template path | LLM composition deferred to Sprint 2 per Sprint 1 scope; `_render_result` per-tool template fix delivered during Stage 8 |
| D1.7 | Two-specialist serial demo + canonical 4-tier scopes | ✓ PASS | UC-04 walkthrough completed both legs; `hr_self_rest` and `it_assets_read_rest` confirmed in `HR_CIBA_SCOPE` / `IT_CIBA_SCOPE` envs and in `mcp/tools.py` `required_scopes` |

---

## §3. Test gates

| Gate | Required | Actual | Verdict |
|---|---|---|---|
| Automated suite (`tools/run-tests.sh`) | All green | 43 files / 810 tests | ✓ |
| N-tests on critical path | N1, N4, N6, N7 covered in Sprint 1 fan-out | All embedded in unit suite | ✓ |
| Manual UC-02 (IT path) | PASS in browser as `employee_user` | PASS at 18:05 (single CIBA, asset list rendered) | ✓ |
| Manual UC-03 (HR canonical demo) | PASS in browser as `employee_user` | PASS at 17:57 (single CIBA, "You have 14 days of leave") | ✓ |
| Manual UC-04 (serial fan-out) | PASS in browser, two CIBA approvals | PASS at 18:14 (HR then IT, both fragments rendered) | ✓ |

---

## §4. Carries into Sprint 2 (explicit)

These items are **accepted as not blocking M1** but are P0 carries for Sprint 2 Wave 1:

| ID | Carry | From |
|---|---|---|
| A-1 | CI `compose-up` smoke test (asserts 6/6 services healthy, UC-03 round-trips a non-empty `chat_message` SSE event) | retro §5 |
| A-3 | Multi-agent UC-04 result label currently shows only one agent — fix attribution UI | retro §5 |

Both are quality-gates rather than functional gaps; the demo runs without them. Out of scope for M1, in scope for Sprint 2 Wave 1.

---

## §5. Definition of Done — milestone-plan §6 cross-check

| # | Requirement | Status |
|---|---|---|
| 1 | Sprint 1 D1.1–D1.7 all green | ✓ — see §2 |
| 2 | Sprint 2 D2.1–D2.6 all green | n/a — Sprint 2 not started |
| 3 | Sprint 3 D3.1–D3.4 all green | n/a — Sprint 3 not started |
| 4 | N-tests in CI | ✓ for Sprint 1 scope; full N-set is M3 sign-off scope |
| 5 | R-tests against live IS | ✓ — UC-02/03/04 against `13.60.190.47:9443` |
| 6 | Spike memo updated with new findings | ✓ — F-17 added during Sprint 1; no new findings in Stage 8 walkthrough |
| 7 | Demo runnable end-to-end in <2 min from `docker compose up` | ⚠️ Carry A-1 (manual verified, not yet automated) |
| 8 | User-experience document updated to reflect actual UX | ⚠️ Carry A-3 (multi-agent attribution) |

Items #2, #3, #4, #7, #8 are full-POC sign-off requirements (M3), not M1. M1 covers only Sprint 1's slice. M1 sign-off is therefore **sufficient on items #1, #5, #6** and explicitly carries items #7, #8 into Sprint 2 Wave 1.

---

## §6. Artifacts produced this sprint

| Path | Purpose |
|---|---|
| `docs/architecture/sprint-1.md` | Stage 4 technical architecture |
| `docs/architecture/sprint-1-fixes.md` | Stage 5 council fixes (F-01..F-17) |
| `docs/architecture/api-contracts.md` | Pydantic / TypedDict / dataclass definitions |
| `docs/architecture/module-layout.md` | 34 modules across 5 services + `common/` |
| `docs/architecture/sequence-diagrams.md` | 8 Mermaid sequences (UC-01..06 + actor-token + UC-04 denial) |
| `docs/architecture/sprint-1-retro.md` | **Stage 9 retrospective (this sprint)** |
| `docs/architecture/sprint-1-signoff.md` | **Stage 10 sign-off (this doc)** |
| `docs/spikes/wso2-is-capability-memo.md` | M0 spike memo, F-17 added during Sprint 1 |
| `docs/use-cases/UC-02..UC-04` | Use-case specs (Sprint 1 scope) |
| `docs/use-cases/UC-07, UC-08` | Use-case specs (Sprint 2 build) |
| `docs/wso2-is-setup.md` | IS Console configuration walkthrough |
| `tools/run-tests.sh` | Per-file test runner (43 files / 810 tests) |
| 5 service `.env` files | All cross-references wired |

---

## §7. Sign-off

> **Sprint 1 / M1: closed.** UC-02, UC-03, UC-04 happy paths verified end-to-end against live WSO2 IS 7.2.0 with per-agent CIBA. Two carries (A-1 CI smoke test, A-3 multi-agent attribution) accepted into Sprint 2 Wave 1.

Sprint 2 begins next session. Scope per milestone-plan §3.4: UC-01 (single-agent), UC-05 (consent denial), UC-06 (token expiry mid-conversation), UC-07 (HR Admin asset issue, requires `it_assets_write_rest` scope), UC-08 (role-based denial Act II).
