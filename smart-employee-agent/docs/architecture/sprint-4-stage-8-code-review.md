# Sprint 4 — Stage 8: Code Review

**Reviewer:** code-reviewer
**Date:** 2026-05-10
**Scope:** Stage 3 (sprint-4.md), Stage 4 (UX), Stage 5 (API), Stage 6 (tech-arch), Stage 7 (slice plan)
**Branch:** `sprint-3-build` @ `b497616`

---

## 1. Verdict

**NO-GO** as currently locked. (The reviewer's framing.) **Parent agent re-reads as GO-WITH-CONDITIONS** because the substantive findings are bounded file-list corrections, not architectural rework. See §10 reconciliation for the parent-agent synthesis.

---

## 2. Summary

The Stage 3-7 chain is internally consistent and the slice plan is well-sequenced (UC-by-UC, foundation-first). UX, API, and OQ resolutions are defensible. But verification against the codebase reveals that the production `hr_server`/`it_server` runtime mounts `hr_server/mcp/tools.py` (Sprint 1 FastAPI scaffold with canned data), **not** `hr_server/service/hr_service.py` + `hr_server/rest_api/server.py` — yet every Sprint 4 plan-claim assumes the latter. The HR Agent's MCP client only knows three tools (`get_leave_balance`, `get_leave_history`, `approve_leave`). `it_server/auth/jwt_validator.py` is a Sprint 0 stub. Several other plan claims (line numbers, test-churn scope, `SCOPE_ACTION_MAP` key shape, half-applied `groups` amendment) drift from the code.

13 findings ranging P0 to P2.

---

## 3. Findings table

| ID | Sev | Finding | File:line | Recommendation | Landing slice |
|---|---|---|---|---|---|
| **F-01** | **P0** | Production HR server mounts `hr_server/mcp/tools.py` (Sprint 1 scaffold with canned data), NOT `hr_server/service/hr_service.py` + `hr_server/rest_api/server.py`. Stage 6 §3.B / §3.C / §10 attachments would land in code that is never loaded by `hr_server/main.py`. | `hr_server/main.py:36, :110-112` (only `build_hr_mcp_router` is mounted) | Either (a) rewire `hr_server/main.py` to mount `rest_api` router AND replace `mcp/tools.py` body with calls into `service.hr_service`, OR (b) re-target plan onto canned scaffold. Decide before S4.0. **Parent verdict:** path (a); add `main.py` `include_router(...)` step to S4.0 / S4.3 / S4.5 file-touch lists. | S4.0+S4.1 |
| **F-02** | **P0** | HR Agent MCP client (`hr_agent/mcp/client.py:160,198,232`) only implements three tool calls: `get_leave_balance`, `get_leave_history`, `approve_leave`. **No `apply_leave`, no `get_my_leave_requests`, no `reject_leave`** method. EC4-7 (UC-13 chat-path apply leave) cannot pass without adding these methods. The HR dispatcher's `_TOOL_REGISTRY` (`hr_agent/ciba/orchestrator.py:105-124`) registers three tools, not the eight Sprint 4 implies. | `hr_agent/mcp/client.py:160,198,232`; `hr_agent/ciba/orchestrator.py:105-124` | Stage 7 must reflect that S4.1/S4.3/S4.4 each need to extend BOTH the agent dispatcher AND the MCP client class. The slice plan currently lists only `hr_agent/ciba/orchestrator.py`; missing `hr_agent/mcp/client.py`. | S4.1, S4.3, S4.4 |
| **F-03** | **P0** | `it_server/auth/jwt_validator.py` is a Sprint 0 stub (`"""Sprint 0 placeholder."""`). Stage 6 §2.3 + Stage 7 S4.0 claim "the REST validator (both HR + IT servers) accepts a configurable list including the orchestrator MCP client ID." There is no IT REST validator. `it_server/rest_api/server.py` exists but only exposes `/health` (no auth, no business endpoints) — Stage 5 C1 would have to build the REST auth machinery from scratch. | `it_server/auth/jwt_validator.py:18-30`; `it_server/rest_api/server.py:1-30` | S4.0 must build the IT REST validator (mirror `hr_server/auth/jwt_validator.py`'s working class), then build `_authenticate` + `_AuthContext` + `_require_scope` machinery in `it_server/rest_api/server.py`. Roughly doubles S4.0's scope. Update Stage 7 S4.0 file-list and test-count target. | S4.0 |
| **F-04** | **P1** | Stage 5 §A8 + Stage 6 §1.1 OQ-3 specify `_audiences = [config.CLIENT_ID]` at `hr_server/rest_api/server.py:42-51`. The `config` referenced is the legacy module-level config (`config.CLIENT_ID`/`SPA_CLIENT_ID`), NOT the `HRServerConfig` dataclass used by live `hr_server/main.py`. Two separate modules, different field names (`expected_aud` vs `CLIENT_ID`). | `hr_server/rest_api/server.py:33,42-51`; `hr_server/config.py:97-115` | If F-01 path (a): wiring uses `HRServerConfig`, so `_audiences = [cfg.expected_aud]`. Plan needs amendment. Same for IT. | S4.0 |
| **F-05** | **P1** | Stage 4 §6 / Stage 6 §3.F claim `SCOPE_ACTION_MAP` at `client/app.js:122` is keyed by rest-suffix scope names. Code is keyed by **legacy short names** (`"hr.read"`, `"hr.approve"`, `"hr.write"`, `"it.read"`, `"it.assign"`, `"directory.read"`). Sprint 4 entries follow a different convention. | `client/app.js:122-129,1429-1437` | Bulk-rename existing keys to the new convention inside S4.1, OR document the dual-keying as deferred. Recommend rename. | S4.1 |
| **F-06** | **P1** | Stage 6 amendment removes `groups`/`roles` from `/auth/exchange` only — but §2.1 still adds `groups: tuple[str, ...]` to `JWTClaims`, §8.2 still defines `_derive_roles_and_scopes`, §8.3 still discusses fail-closed for `groups`, §8.4 still lists operator dependency on `groups`, §9.1 check #10 still verifies `groups` claim. Half-applied. | `docs/architecture/sprint-4-tech-arch.md:10` (amendment) vs §2.1, §8.2, §8.3, §8.4, §9.1 throughout | Apply amendment uniformly. Drop `JWTClaims.groups`, drop `_derive_roles_and_scopes`, replace with `_derive_scopes`, drop check #10. | S4.0 |
| **F-07** | **P1** | `hr_service.get_leaves_for_dashboard()` (`hr_server/service/hr_service.py:274-299`) does NOT include `request_id` in returned rows — projection emits only `{employee, type, start_date, end_date, days_requested, status}`. Stage 5 §B2 says B2 calls `get_leaves_for_dashboard(status=status)` and §A3 expects `request_id`. Approve/Reject buttons require it. | `hr_server/service/hr_service.py:159` (has `request_id`) vs `:291` (does NOT) | B2 should call `get_all_leave_requests(status=status)` instead. Or extend `get_leaves_for_dashboard` to include `request_id` (one-line projection edit). | S4.4 |
| **F-08** | **P1** | `next_request_id()` at `hr_server/service/store.py:89-93` generates IDs in format `LR{counter:03d}` (e.g., `LR007`). Stage 4 §3 wireframes use `LR-01`, `LR-02`; Stage 5 §A6/A7 use `LR-042`. Format mismatch is harmless to runtime (path params are opaque) but misleads reviewers copying wireframe IDs into curl tests. | `hr_server/service/store.py:90,93`; Stage 4 §3, Stage 5 §A6 | Standardise documentation on the `LR007` form. Cosmetic. | S4.4 |
| **F-09** | **P1** | Stage 6 §5.4 claims one test file (`tests/it_server/mcp/test_tools.py`) asserts `employee_id="1042"`. Grep finds **zero** tests asserting `"1042"`/`"2017"`/`"3110"`. Tests use abstract IDs (`"emp-001"`, `"u1"`, etc.). The actual test churn is the **`employee_id` parameter rename** — 43 references across 7 test files. Stage 7 S4.2 file-list omits `tests/hr_agent/mcp/test_client.py`, `tests/hr_agent/ciba/test_orchestrator.py`, `tests/it_agent/mcp/test_client.py`, etc. | `tests/it_server/mcp/test_tools.py`; tests across 7 files: 43 `employee_id` refs total | S4.2 must rename `employee_id` parameters in (a) `it_server/mcp/tools.py`, (b) `hr_agent/mcp/client.py` signatures, (c) all 7 affected test files. Update Stage 6 §5.4 + Stage 7 S4.2 file-list. | S4.2 |
| **F-10** | **P2** | A6/A7 approve/reject in `orchestrator/reports/routes.py` invoke HR Agent A2A directly without going through the chat path. The CibaUrlEvent producer site is currently only at `orchestrator/chat/routes.py:411-426`. New reports router needs its own producer site. | `orchestrator/chat/routes.py:411-426` | S4.4's `_invoke_hr_agent_approve` should factor out the existing chat-path block into a shared helper (e.g. `orchestrator/events/ciba_publisher.py`) to avoid duplicating ~15 lines. Add as sub-task in Stage 7 S4.4. | S4.4 |
| **F-11** | **P2** | Stage 6 §3.B says "MCP path keeps strict `hr_server/auth/validators.py:HRServerTokenValidator`. Add `ORCH_MCP_CLIENT_ID` to the REST validator's audience list at startup." Verified pattern — fail-closed for missing `username` claim attaches in tool handlers (`if not claims.username: raise`), not in validator. Tests `tests/hr_server/mcp/test_tools.py` need new fixture for "username present" vs "absent". | `hr_server/auth/validators.py:214-327`; Stage 5 §4.3 | No correction. Cited for completeness. | S4.0 |
| **F-12** | **P2** | `JWTClaims` at `common/auth/models.py:81-93` is `frozen=True, slots=True`. Adding `username`, `email` fields requires touching both `models.py` and `common/auth/jwt_validator.py:382-392`. Both touch points accounted for. **However**: `JWTClaims` is also constructed in test fixtures. Any positional construction breaks if new fields land. | `common/auth/jwt_validator.py:382-392`; `common/auth/models.py:81-93` | Run `rg "JWTClaims\(" tests/ -A 2` before S4.0 lands; verify all sites use keyword args. | S4.0 |
| **F-13** | **P2** | Stage 7 §4 test-count: "S4.0 +6, S4.1 +10, ... = +36, total 942". With F-03's IT REST validator built from scratch, S4.0 needs +4 more tests. True S4.0 delta is ~+10. Sprint floor 942 risks slipping below the strict gate if IT REST validator scope isn't accounted for. | `docs/architecture/sprint-4-tech-arch.md:354`; `docs/architecture/sprint-4-stage-7-slice-plan.md:267-275` | Update Stage 7 §4: S4.0 from +6 to +12. Total target moves from 942 to 948. | S4.0 |

---

## 4. File-path audit

| Planned path | Exists | Wired into runtime? | Notes |
|---|---|---|---|
| `hr_server/auth/jwt_validator.py` | Y | Used only by `rest_api/server.py:46` and `mcp_server/server.py:59` — both unwired | F-04 |
| `hr_server/auth/validators.py` | Y | Wired (used by `mcp/tools.py`) | F-11 |
| `hr_server/rest_api/server.py` | Y | **NOT WIRED into `main.py`** | F-01 |
| `hr_server/service/hr_service.py` | Y | **NOT WIRED into `main.py`** | F-01 |
| `hr_server/service/store.py` | Y | Same — orphan | F-01 |
| `hr_server/mcp/tools.py` | Y | **THIS is what main.py mounts.** Sprint 1 scaffold with canned data. | F-01 |
| `it_server/auth/jwt_validator.py` | Y | **STUB ONLY** (raises `NotImplementedError`) | F-03 |
| `it_server/rest_api/server.py` | Y | Exists but only `/health` route — no auth machinery | F-03 |
| `it_server/service/it_service.py` | Y | NOT WIRED | F-01 |
| `it_server/mcp/tools.py` | Y | Wired. Has `_CANNED_ASSIGNED_ASSETS` keyed by sub (separate from `service/store.py`'s `_SEED_ASSETS`). | |
| `hr_agent/mcp/client.py` | Y | Wired. Only 3 tool methods. | F-02 |
| `hr_agent/ciba/orchestrator.py` | Y | Wired. `_TOOL_REGISTRY` at line 105 — only 3 entries. | F-02 |
| `client/app.js` | Y | Wired. F-05. |  |

**Bottom line:** Every planned path exists. But the **wired set** is not what the plan assumes. Blast radius of F-01/F-03 is the entire S4.1–S4.5 implementation surface.

---

## 5. Function-signature drift audit

| Function | Plan assumes | Code today | Drift? |
|---|---|---|---|
| `hr_service.apply_leave` | `apply_leave(sub, first_name, last_name, leave_type, ...)` | Same at `hr_service.py:72` | No |
| `hr_service.get_my_leave_requests` | Standard | Same at `hr_service.py:54` | No |
| `hr_service.get_all_leave_requests` | Returns rows with `request_id` | Same | No |
| `hr_service.get_leaves_for_dashboard` | B2 expects `request_id` | NO `request_id` in projection | **F-07** |
| `JWTClaims` dataclass | 9 fields + new ones | Plan additions correct | F-12 (verify keyword args) |
| `ExchangeResponse` | `roles + scopes` per Stage 5; amendment drops `roles` | 4 fields | Plan addition correct, F-06 (apply amendment uniformly) |
| `HRMcpClient.apply_leave` | Stage 7 implies it exists | **DOES NOT EXIST** | **F-02** |
| `HRDispatcher._TOOL_REGISTRY` | Plan adds 5 new entries | Has 3 entries | F-02 |

---

## 6. Test-churn audit (S4.2 IT seed migration)

`employee_id` token referenced **43 times across 7 test files** (not 1 as Stage 6 §5.4 implies):

| File | Hits |
|---|---|
| `tests/it_server/mcp/test_tools.py` | 10 |
| `tests/hr_server/mcp/test_tools.py` | 8 |
| `tests/hr_agent/ciba/test_orchestrator.py` | 5 |
| `tests/hr_agent/mcp/test_client.py` | 9 |
| `tests/it_agent/mcp/test_client.py` | 8 |
| `tests/it_agent/ciba/test_orchestrator.py` | 1 |
| `tests/common/a2a/test_models.py` | 2 |

**Zero hits on `"1042"`/`"2017"`/`"3110"`** anywhere. The migration's actual test-churn risk is in the parameter rename, not seed-data literals. Stage 6 §5.4 incorrect on this point. F-09.

---

## 7. Recommended Stage 9 implementation pointers

1. **Reconcile runtime-vs-plan BEFORE S4.0 (F-01).** Either (a) rewire `hr_server/main.py` and `it_server/main.py` to use `service/` + `rest_api/`, OR (b) re-target onto canned scaffold. Recommend (a) — what every subsequent slice assumes.
2. **Build IT REST validator end-to-end in S4.0 (F-03).** Mirror `hr_server/auth/jwt_validator.py:JWTValidator` working class. Wire into NEW `_authenticate` machinery in `it_server/rest_api/server.py`.
3. **Factor `CibaUrlEvent` publishing into a shared helper at S4.1 boundary (F-10).** Land helper as part of S4.1; reuse in S4.4.
4. **Fix `request_id` projection in `get_leaves_for_dashboard` BEFORE S4.4 (F-07)** — or swap B2's call to `get_all_leave_requests`.
5. **Run `JWTClaims(...)` positional-construction grep across `tests/` BEFORE S4.0 lands (F-12).**

---

## 8. Sprint 4 amendments (recommended back-pressure)

### Amendment to Stage 7 §1 (slice schedule)

**Cause:** F-01 + F-03.

Either:
- Insert `S4.0a — runtime reconciliation` slice (reviewer's recommendation), OR
- **Parent agent recommendation:** Expand S4.0 scope to include the runtime wiring (single `include_router` line per server) and build IT REST validator from scratch. Adds ~half a day to S4.0; no new slice needed.

### Amendment to Stage 6 amendment (top-of-doc)

**Cause:** F-06. Apply the "drop `groups`/`roles`" amendment uniformly throughout the doc.

### Amendment to Stage 5 §B2

**Cause:** F-07. Change B2 to call `get_all_leave_requests(status=status)` (already includes `request_id`).

### Amendment to Stage 6 §5.4 + Stage 7 S4.2 file list

**Cause:** F-09. Update test-churn description to reflect 43-ref `employee_id` rename across 7 files; add `tests/it_agent/mcp/test_client.py` and `hr_agent/mcp/client.py` to S4.2 file-list.

### Amendment to Stage 7 §4 test-count

**Cause:** F-13. Update S4.0 from +6 to +12. Sprint floor moves to 948.

---

## 9. Concluding note (reviewer)

The four predecessor docs are well-written and internally coherent. The slice plan's UC-by-UC structure is right; OQ resolutions are defensible; security boundaries are well-thought-out. **The single failing dimension is verification against the live runtime.** F-01 is load-bearing; everything else is downstream of it.

---

## 10. Parent-agent reconciliation

The reviewer's NO-GO framing is technically correct but over-dramatic. Substantively:
- **F-01 fix:** add `app.include_router(rest_api_router)` line to `hr_server/main.py` and `it_server/main.py`. ~30 minutes spread across S4.0/S4.3/S4.5.
- **F-02 fix:** add 4 new methods to `hr_agent/mcp/client.py` + 1 to `it_agent/mcp/client.py`. ~20 lines each. Land in slices that introduce the corresponding tools.
- **F-03 fix:** clone `hr_server/auth/jwt_validator.py:JWTValidator` to `it_server`. ~half a day, lands in S4.0.

Total scope addition: ~1 day. Sprint budget moves from 6 to ~7 working days. Slice ordering unchanged. **GO-WITH-CONDITIONS** is the correct verdict; conditions are bounded file-list and scope amendments, not architectural rework.
