# Sprint 2 — M2 Sign-off

**Sprint:** 2 (post-CIBA-pivot, demo-critical + error-paths)
**Branch:** `sprint-1-build` (carried over)
**Closed:** 2026-05-08
**Tests:** 822 / 43 files green
**Sign-off basis:** browser walkthrough against live WSO2 IS 7.2.0 at `13.60.190.47:9443`

## Acceptance — Sprint 2 D2.x deliverables

| Ref | Acceptance | Status | Evidence |
|---|---|---|---|
| **D2.1** | Deny on consent widget → graceful "HR access not granted" partial reply | ✓ | `_friendly_error` (agent-aware copy); test `test_chat_mid_flow_denial_includes_agent_label_in_copy` |
| **D2.2** | Browser closed mid-CIBA → orchestrator detects + cancels pending CIBA | ✓ | SSE `on_disconnect` hook in `orchestrator/main.py` fan-outs A2A `cancel`; tests `test_on_disconnect_callback_fires_when_stream_ends` and `test_on_disconnect_hook_exception_does_not_propagate` |
| **D2.3** | `auth_req_id` expires before user clicks → graceful timeout message | ✓ | Dispatcher → ERR-CIBA-009 → SSE `EXPIRED` → agent-aware "approval timed out" copy; test `test_chat_auth_req_id_expiry_emits_expired_state_and_friendly_copy` |
| **D2.4** | Every log line at every hop carries `X-Request-ID`; manual `grep <rid>` reconstructs the user→orchestrator→specialist→MCP chain end-to-end | ✓ | `scripts/grep-trace.sh` + new "Audit correlation walkthrough" in `docs/demo-runbook.md` + in-app trace panel; live-verified rid `22ce8467-…` reproduced full chain |
| **D2.5** | Token expires mid-task → next user request triggers re-CIBA framed as "Re-authorizing" | ✓ | Per-(user_sub, scope) `_CachedToken` in both dispatchers; `is_refresh=True` + `prior_consent_at` + REFRESH binding-message variant; SPA Session Refresh widget already wired; 4 unit tests for cache outcomes |
| **D2.6** | N-tests N18–N26 passing (council BA recommendations) | Partial | N18 ✓, N19 ✓, N20 ✓, N23 ✓, N26 ✓ (covered by 2A.4 correlation tests). N21 / N22 / N25 deferred — existing aud + scope guards already block these paths and explicit replay tests need a less-mocked fixture; N24 singleflight deferred (no current symptom). N27 manual usability — out of automation scope. |
| **D2.7** | HR Admin can approve a leave request via `hr.approve_leave` end-to-end | ✓ | Live-verified UC-07: `please approve LV-004` as `hr_admin_user` → `hr_approve_rest` token → 200 → success copy |
| **D2.8** | HR Admin can issue an asset via `it.issue_asset` end-to-end | ✓ | Live-verified UC-07: `issue MBP-14-001 to alice` as `hr_admin_user` → `it_assets_write_rest` token → 200 → success copy |
| **D2.9** | Employee `please approve LV-004` denied at MCP with permission-denied copy (UC-08) | ✓ | Live-verified: `employee_user` → token issued with downgraded scope (Path C / F-18) → MCP returns 401 ERR-MCP-003 → SPA shows "You don't have permission to perform this action…" |
| **D2.10** | All scope strings use `_rest` 4-tier names (zero hits for legacy `_a2a` / `_mcp`) | ✓ | `docs/scope-policy.md` rewritten 2A.1; `tests/fixtures/agent_cards/{hr,it}_agent_valid.json` migrated |
| **D2.11** | ERR-MCP-003 reachable + tested (token-B with `hr_self_rest` to `approve_leave_request` returns 401) | ✓ | `tests/it_server/mcp/test_tools.py::T-IT-MCP-12` (read-token rejected) + live UC-08 walkthrough |

## Acceptance — A-x architectural items

| Ref | Acceptance | Status | Evidence |
|---|---|---|---|
| **A-1** | CI smoke runs on every push/PR | ✓ | `.github/workflows/test.yml` — runs full pytest suite (822 tests) on push to `main`/`agent-2-agent` and on PR |
| **A-3** | Multi-agent routing label reads naturally ("first…" / "Now routing to…") | ✓ | RoutingEvent now carries `tool_index` + `total_tools`; SPA picks `routingSingle` / `routingFirst` / `routingSecond` deterministically; tests `test_chat_all_deny_combined_chat_message_mentions_both_agents` (asserts `total_tools=2`, indices `[0,1]`) and `test_chat_routing_event_carries_total_tools_metadata` |

## Use-case coverage at sign-off

| UC | Status | Path |
|---|---|---|
| UC-01 (login) | ✓ | Live |
| UC-02 (single specialist read) | ✓ | Live |
| UC-03 (two-specialist serial) | ✓ | Live + grep-trace verified |
| UC-04 (deny mid-flow) | ✓ | Tests + agent-aware copy |
| UC-05 (browser closed) | ✓ | SSE `on_disconnect` hook; tests |
| UC-06 (token-expiry re-CIBA) | ✓ | Cache + re-CIBA + Session Refresh widget |
| UC-07 (HR Admin write) | ✓ | Live (approve + issue) |
| UC-08 (Employee identity-first denial) | ✓ | Live (Path C / F-18) |

## Carry-overs to Sprint 3+

Documented in `sprint-2-retro.md` §"Carry-overs for Sprint 3+". Highlights:
- **Logoff** — its own design effort; spans IS RP-initiated logout + token revocation + agent fan-out cancel.
- **N21 / N22 / N25** — explicit replay / cross-aud tests with a less-mocked fixture.
- **N24 singleflight** — parallel CIBA dedup on (user, agent, scope).
- **Persistent session store** — Redis-backed, eliminates restart-loss-of-session friction.
- **EX-2 abort-on-first-deny** — revisit if stakeholder feedback says the partial-result default is wrong.

## Sign-off

Sprint 2 (M2) is **complete and ready to demo**. All demo-critical D2.x acceptance plus the targeted error-path improvements (D2.1–D2.5) are live-verified or covered by tests. The known carry-overs are documented for Sprint 3+ planning and do not block stage demo of UC-02 / UC-03 / UC-07 / UC-08.

— Sprint 2 close, 2026-05-08.
