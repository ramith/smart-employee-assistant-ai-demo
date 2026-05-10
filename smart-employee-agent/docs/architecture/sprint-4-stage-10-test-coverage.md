# Sprint 4 — Stage 10: Automated Test Coverage Audit

**Stage:** 10 (test coverage gap-audit + gap-fill)
**Date:** 2026-05-11
**Branch:** `sprint-4-build` (S4.0–S4.5 committed; gap-fills land here)
**Read order:** [`sprint-4.md`](sprint-4.md) §4 (exit criteria) → [`sprint-4-stage-8-security-audit.md`](sprint-4-stage-8-security-audit.md) §8 (recommended security tests) → this doc.

Sprint 4 used a per-slice test-as-you-build discipline; each S4.N slice landed with its own +N tests + a green strict-mode gate. This stage audits the resulting surface against the locked exit criteria + security-audit recommendations and fills any P1+ gaps.

---

## §1. Sprint 4 test surface — final inventory

| File | New / Extended | Tests |
|---|---|---|
| `tests/common/auth/test_models.py` | extended | +2 (S4.0 username/email field defaults + settable) |
| `tests/common/auth/test_jwt_validator.py` | extended | +9 (S4.0 sanitisation: 8 cases + class wrapper) |
| `tests/orchestrator/auth/test_routes.py` | extended | +1 (S4.0 `scopes[]` exposed on ExchangeResponse) |
| `tests/hr_server/auth/test_jwt_validator.py` | NEW (S4.0 follow-up) | 5 |
| `tests/it_server/auth/test_jwt_validator.py` | NEW (S4.0 follow-up) | 6 |
| `tests/hr_server/service/test_cubicle_service.py` | NEW (S4.1) | 14 |
| `tests/hr_server/mcp/test_tools.py` | extended | +9 (S4.1 cubicle scope guards: 7; S4.4 reject_leave: 1; **S4.10 cross-scope replay: 1**) |
| `tests/orchestrator/chat/test_keyword_fallback.py` | extended | +5 (S4.1 cubicle intents: 3; S4.2 cubicle.lookup_self + dual-agent UC-12: 2) |
| `tests/orchestrator/chat/test_routes.py` | extended | +1 (S4.1 action_text propagates through ciba_url) |
| `tests/it_server/service/test_store.py` | NEW (S4.2) | 6 |
| `tests/it_server/mcp/test_tools.py` | rewritten + extended | +2 net (S4.2 it_assets_self_rest scope + missing username) |
| `tests/it_agent/mcp/test_client.py` | rewritten | 0 net (rename) |
| `tests/it_agent/ciba/test_orchestrator.py` | rewritten | 0 net (rename) |
| `tests/orchestrator/reports/test_proxy.py` | NEW (S4.3) | 4 |
| `tests/orchestrator/reports/test_routes.py` | NEW (S4.3) + extended | 1 + 3 (S4.4 A3/A6/A7) |
| `tests/hr_server/rest_api/test_my_leaves.py` | NEW (S4.3) | 2 |
| `tests/hr_server/rest_api/test_pending_leaves.py` | NEW (S4.4) | 2 |
| `tests/hr_server/rest_api/test_cubicle_assignments.py` | NEW (S4.5) | 2 |
| `tests/it_server/rest_api/test_device_assignments.py` | NEW (S4.5) | 2 |
| **`tests/hr_agent/ciba/test_action_text_sanitisation.py`** | **NEW (S4.10 gap-fill)** | **7** |

**Total Sprint 4 net delta:** +83 tests (target was +36 minimum per Stage 7 §4; S4.5 hit floor 980; this stage adds +8 to land at **988** strict-green).

---

## §2. Coverage matrix — Exit Criteria EC4-1..EC4-12

| EC | Description | Coverage | Gap? |
|---|---|---|---|
| EC4-1 | Multi-turn cubicle assignment end-to-end | `test_cubicle_service.py` (14 tests covering summary, vacant-on-floor, assign happy + idempotent + occupied + not_found, lookup_employee); `test_tools.py` cubicle scope guards; `test_keyword_fallback.py` 3 cubicle intents | None — backend fully covered. SPA path is manual gate (Stage 11). |
| EC4-2 | Employee denied `assign_cubicle` (role-denial) | `test_tools.py:test_assign_cubicle_rejects_hr_read_only` | None |
| EC4-3 | Employee dual-agent self-service | `test_keyword_fallback.py:test_route_dual_agent_self_service_uc12`; `test_cubicle_service.py:get_my_cubicle`; `test_store.py:get_my_assets` | None — backend fully covered |
| EC4-4 | Reports page renders three tabs | Backend: B1 + B2 + B3 + C1 endpoint tests cover the data path; SPA-side render is manual (Stage 11) | None for backend; SPA is manual gate |
| EC4-5 | Non-admin → 403 from reports | `test_pending_leaves.py:test_pending_leaves_missing_scope_returns_403`; `test_cubicle_assignments.py` scope test; `test_device_assignments.py` scope test; `test_proxy.py:test_proxy_preflight_scope_missing_returns_403_and_skips_upstream` | None |
| EC4-6 | Cross-scope replay block | **`test_tools.py:test_hr_assets_write_token_cannot_call_self_endpoint`** (Stage 10 gap-fill) | Filled |
| EC4-7 | UC-13 chat apply → panel updates | Backend B1 covered. SSE-settle re-fetch is SPA-side; manual gate. | SPA path manual; backend covered |
| EC4-8 | UC-14 chat status returns matches panel | Backend `get_my_leave_requests` covered (Sprint 1); cross-surface consistency is manual | SPA-side cross-surface; manual gate |
| EC4-9 | My Leaves panel first-paint | Backend B1 covered; SPA first-paint is manual | SPA manual gate |
| EC4-10 | strict-mode test gate ≥ 906 | **988/60 strict-green** post-gap-fill (S4.0–S4.5 + Stage 10) | None |
| EC4-11 | Demo runbook ≤4 minutes | Manual rehearsal (Stage 11) | n/a — manual |
| EC4-12 | sprint-4-signoff.md | Stage 12 deliverable | n/a — manual |

---

## §3. Coverage matrix — Security audit recommended tests

Source: [`sprint-4-stage-8-security-audit.md`](sprint-4-stage-8-security-audit.md) §8.

| ID | Description | Coverage | Gap? |
|---|---|---|---|
| R-AUD-1 | Audience-list cap (4+ entries → startup raises) | `test_jwt_validator.py:test_build_audiences_cap_enforced` (HR + IT) | None |
| R-AUD-2 | Startup INFO log enumerating accepted audiences | `test_jwt_validator.py:test_build_validator_from_config_logs_audiences` (IT) | None |
| R-CSRF-1 | Approve/Reject without `X-Request-ID` → 400 | `test_routes.py:test_a6_approve_requires_x_request_id_header` | None |
| R-USERNAME-1 | Control-char sanitisation in JWT claims | `test_jwt_validator.py:TestSanitiseUserString::test_strips_control_chars` + newlines + Unicode line separators | None |
| R-USERNAME-2 | Username length cap at 64 | `test_jwt_validator.py:TestSanitiseUserString::test_caps_to_max_len` + email cap at 256 | None |
| R-AUDIT-1 | Audience segregation (token-A on REST, token-C on MCP) | `test_jwt_validator.py:test_validator_accepts_extra_audience` + `test_validator_rejects_unlisted_audience` (HR + IT mirror) | None |
| R-CUBICLE-CONCURRENT | TOCTOU race on `assign_cubicle` | F-04 explicit decision: acceptable for Sprint 4 (single-process, single-uvicorn-worker serialised). Documented in code comment + this audit. | Acknowledged, no test |
| R-ACTIONTEXT-1 | action_text injection (charset whitelist) | **`test_action_text_sanitisation.py`** (Stage 10 gap-fill, 7 cases including HTML injection, control chars, length cap) | Filled |
| R-REDACTION-1 | `sub` redaction in logs | F-07 nice-to-have (security audit §9). Not P1; deferred. | Deferred — not blocking |
| R-AUTHCLAIM-1 | Missing `username` claim → fail-closed 401 | `test_tools.py:test_get_my_assets_missing_username_claim_returns_401` (IT) | None |

---

## §4. Gap-fill landed in Stage 10

Two tests added in this stage:

1. **`tests/hr_agent/ciba/test_action_text_sanitisation.py`** — 7 tests covering R-ACTIONTEXT-1: empty input, allowed charset preserved, HTML injection stripped, control chars + newlines stripped, length cap, Unicode line separators, apostrophe + email-char survival. The sanitiser is the F-08 mitigation; tests verify the contract directly.

2. **`tests/hr_server/mcp/test_tools.py:test_hr_assets_write_token_cannot_call_self_endpoint`** — 1 test covering EC4-6: a token-C with only `hr_assets_write_rest` cannot be replayed at `/mcp/tools/get_my_cubicle` (which requires `hr_self_rest`). Validator rejects with `ERR-MCP-003`.

Net Stage 10 delta: **+8 tests** (sanitiser file = 7; cross-scope replay = 1).

---

## §5. Gaps deliberately deferred (not blocking)

| Item | Why deferred |
|---|---|
| R-REDACTION-1 — `sub` UUID + `assigned_to_sub` redaction patterns in `RedactionFilter` | Security audit F-07 marked nice-to-have. Sprint 4 surfaces never log `sub` in row data (verified via grep). Pre-empts a slip-up but no current attack path. Recommend Sprint 5 if the codebase grows. |
| R-CUBICLE-CONCURRENT — TOCTOU race | F-04 explicit accept: in-memory + single-uvicorn-worker (BLOCK-I) means dict access is serialised within one process; cross-process race is impossible. Documented. Not a demo failure mode. |
| EC4-7 / EC4-8 / EC4-9 SPA-side surfaces | Out of automated-test scope per Sprint 4 testing posture (no Selenium / Playwright for the demo). Covered by Stage 11 manual gate. |

---

## §6. Final test gate (post-Stage-10)

```
tools/run-tests.sh
====================================================
Files passed: 60    Files failed: 0
Total tests:  988
====================================================
```

**+8** over S4.5 close (980/59). All Stage 10 tests strict-green.

---

## §7. Stage exit gate

To exit Stage 10:
- ✅ Coverage matrix complete for all 12 EC4 items + 10 security R-tests.
- ✅ Two material gaps (R-ACTIONTEXT-1, EC4-6) filled with tests.
- ✅ Three deferred items explicitly justified (not blocking).
- ✅ Strict-mode gate green at 988/60.
- ✅ This audit document landed.

Stage 11 (manual gate against live IS) is independent of Stage 10. The SPA-side coverage gaps (EC4-4 / EC4-7 / EC4-9) are Stage 11's responsibility; pre-flight via `scripts/check-is-config.sh`.

---

## §8. References

- [Stage 3 binding plan](sprint-4.md) §4 (exit criteria)
- [Stage 7 slice plan](sprint-4-stage-7-slice-plan.md) §4 (test-count budgets)
- [Stage 8 security audit](sprint-4-stage-8-security-audit.md) §8 (recommended tests)
- [Stage 8 architect review](sprint-4-stage-8-architect-review.md)
- [Stage 8 code review](sprint-4-stage-8-code-review.md)
