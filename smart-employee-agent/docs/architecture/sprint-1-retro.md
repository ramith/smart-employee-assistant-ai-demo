# Sprint 1 — Retrospective (Stage 9)

**Date:** 2026-05-07
**Sprint goal:** Ship UC-01..UC-04 happy paths end-to-end against on-prem WSO2 IS 7.2 (per-agent CIBA architecture).
**Status:** Demo paths green in browser. Ready for Stage 10 (M1 sign-off) once action items below are triaged.

---

## §1. What shipped

| Use case | Manual verdict (browser, employee_user) |
|---|---|
| UC-02 — single specialist (IT, asset list) | ✓ PASS |
| UC-03 — single specialist (HR, leave balance) | ✓ PASS — canonical demo |
| UC-04 — multi-agent serial fan-out (HR + IT) | ✓ PASS — two consecutive CIBA approvals |

Automated suite: 43 files / 810 tests green via `tools/run-tests.sh`.

---

## §2. What went well

- **Capability spike paid off.** F-01 through F-17 were all surfaced before Stage 6 build. No new IdP-level surprises emerged during the manual walkthrough — every issue was inside our own code/config.
- **Stage-gated process held.** Stage 5 council review caught the F-01 two-call A2A pattern; Stage 6 implementing agents built to it without coordination overhead.
- **Per-agent CIBA architecture works.** Token shape `{sub: user, act.sub: agent}` arrives correctly at MCP servers. Audit trail via correlation IDs is readable end-to-end.
- **UX polish during walkthrough.** ui-designer agent fixed the "raw Python dict" rendering on the spot — single render-result switch in `orchestrator/chat/routes.py:_render_result`.

---

## §3. What went poorly — bug ledger from Stage 8

Thirteen issues surfaced during the browser walkthrough. None blocked sign-off, but all are real defects that the automated suite missed. Categorised by **why we didn't catch them earlier**:

### Build/packaging (drift between code and Docker image)

- **B1.** `hr_server/requirements.txt` and `it_server/requirements.txt` were Sprint 0 stubs missing `fastapi` + `uvicorn`. Fixed by adding both.
- **B2.** Orchestrator Dockerfile didn't bundle the SPA static files or the agent-card fixtures. Fixed by `COPY client/* → /app/client_static` and `COPY tests/fixtures/agent_cards → /app/agent_cards` + `AGENT_CARDS_DIR=/app/agent_cards` env.
- **B3.** `docker-compose.yml` `HR_MCP_SERVER_URL` / `IT_MCP_SERVER_URL` carried a stale `/mcp` path suffix that the new config validator rejects. Fixed (path stripped — MCP client appends `/mcp/tools/...` itself).
- **B4.** Missing `HR_EXPECTED_INBOUND_AUD` / `IT_EXPECTED_INBOUND_AUD` envs not surfaced during compose wiring. Fixed via compose `environment:` block referencing `${ORCHESTRATOR_MCP_CLIENT_ID}`.

### Local environment hygiene

- **E1.** Stale orphan containers from the old hyphenated service-dir naming (`smart-employee-agent-hr-server-1` etc.) blocked port binding. Required `docker rm -f`.
- **E2.** A stray host-side `python3 main.py` (PID 41026, ~33h old) held port 8000 alongside the docker container — host process won, dockerised hr_server unreachable from host.

### Dev architecture (SPA ↔ orchestrator routing)

- **A1.** SPA at `:3001` had no proxy to orchestrator at `:8090`; the relative `/auth/login` URL hit the SPA dev server's SPA-fallback and silently 404'd into `index.html`. Fixed by mounting the SPA inside the orchestrator (same-origin at `:8090`), removing the cross-origin problem entirely.
- **A2.** `ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI` registered as `/agent-callback` but the FastAPI handler was at `/auth/callback`. Renamed handler to `/agent-callback` to match what's registered in IS.

### Pattern C client wiring

- **P1.** Authorize call used `spa_client_id` (BO4...) but token-exchange used `mcp_client_id` (Ry9...). WSO2 IS rejected the cross-client code redemption with `invalid_grant`. Fixed by using the MCP client_id for both — matches the c1 spike.
- **P2.** `ExchangeResponse` returned `{ok, user_label}` only, but the SPA needed `session_id` and `user_display_name` (the SSE URL is `/events/{session_id}` — a path param, not a cookie). Added both fields to the response model + the relay HTML now stashes them in localStorage before redirecting.
- **P3.** Memory-snapshot leaked into setup steps: I (assistant) initially advised the user to register the redirect URI on `orchestrator-mcp-client` only, then incorrectly suggested moving it to `orchestrator-app`, then to the agent OAuth App, then back to `orchestrator-mcp-client`. Each was right for *one* of the three clients used in Pattern C, but I didn't lay all three out up front. Real cost: ~3 console round-trips for the user.

### SPA UI

- **U1.** `#signin-page { display: flex }` overrode the `[hidden]` UA stylesheet rule (specificity 100 vs 1). JS set `signin-page.hidden = true`, but the page stayed visible. Fixed with `[hidden] { display: none !important; }` at the top of `styles.css`.
- **U2.** Agent card IDs were hyphenated (`"id": "hr-agent"`) but the keyword router emitted `"hr_agent"`. Registry lookup failed. Fixed both fixture files.
- **U3.** Orchestrator emitted `VERIFYING` SSE immediately after `ciba_url`, replacing the AWAITING_APPROVAL widget before the user could click the auth_url. Removed the eager publish; widget now stays in AWAITING_APPROVAL until `await_completion` resolves.
- **U4.** Tool result rendered as raw Python dict literal (`{'asset_id': 'MBP-14-001', ...}`). Fixed via per-tool render switch in `_render_result`. (See D-action below for multi-agent label.)

---

## §4. Patterns / themes

1. **The automated suite never exercised the Docker image.** All 13 issues passed the unit tests. The cheapest mitigation is a `compose-up` smoke test in CI that asserts every `/healthz` returns 200, and that the canonical UC-03 path completes through to a non-empty SSE `chat_message`.

2. **OIDC redirect-URI configuration is the single largest source of friction.** Three different OAuth/OIDC clients (orchestrator-app, orchestrator-mcp-client, orchestrator-agent's auto-created OAuth App), each with its own redirect_uri allowlist, with three different intended uses (browser PKCE, back-channel exchange, App-Native Auth). The setup-doc walks through them but doesn't show them on one page side-by-side.

3. **In-memory session store + frequent rebuilds = lots of "clear localStorage and re-login".** Painful for the user during iterative debugging, even though Sprint 1 explicitly chose in-memory per Q5. Mitigated for demo by single-iteration walkthrough, but an SQLite-backed session store would have saved the user ~6 re-logins.

4. **Demo content drift.** Several places assumed `probe.user` (Sprint 0 spike user) instead of `employee_user` / `hr_admin_user`. Each was caught reactively. A demo-readiness checklist that grep-walks for `probe.user` would catch these in one pass.

---

## §5. Action items (entering Sprint 2)

| ID | Action | Owner | When |
|---|---|---|---|
| A-1 | Add `compose-up` healthcheck smoke test to CI (asserts 6/6 services pass `/healthz` after `make demo-up`). | DevOps | Sprint 2 Wave 1 |
| A-2 | Add a "client matrix" table to `docs/wso2-is-setup.md` showing all three OAuth/OIDC clients side-by-side with their redirect_uri requirements, who calls them, and which env var binds where. | Tech writer | Sprint 2 Wave 1 |
| A-3 | Multi-agent UC-04 result currently labelled with only one agent ("IT Agent — completed") even though body has both HR and IT fragments. Either render two stacked agent badges, or relabel to "Combined". | UX | Sprint 2 Wave 2 |
| A-4 | Decide on persistent session store for dev (SQLite) vs. accept in-memory + clear-localStorage friction. | Architect | Sprint 2 planning |
| A-5 | grep-driven demo-content audit for stale identifiers (`probe.user`, hyphenated `hr-agent`, simplified `hr.read` scopes) before each sprint demo. | Tech writer | Sprint 2 Wave 1 |
| A-6 | Capture P3 (redirect-URI confusion) as a decision-tree in setup doc: *"You are configuring redirect_uri for which client? → flow chart"*. | Tech writer | Sprint 2 Wave 1 |

---

## §6. Hand-off to Stage 10 (M1 sign-off)

Pre-conditions for sign-off (per milestone-plan §6):

- [x] Sprint 1 D1.1–D1.7 functional verification (UC-02/03/04 PASS in browser)
- [x] 43 files / 810 tests green
- [x] Spike memo (`docs/spikes/wso2-is-capability-memo.md`) up to date — F-17 added during Sprint 1 build, no new findings during walkthrough
- [ ] Demo runnable end-to-end in <2 minutes from a clean `docker compose up` *(blocked: requires A-1 to verify automatically)*
- [ ] User-experience document updated to reflect actual UX *(blocked: requires A-3 decision)*

Action items A-1, A-3 do not block the *content* of M1 sign-off — they are quality gates that catch regressions. Recommend signing off M1 with A-1 and A-3 deferred to Sprint 2 Wave 1 as P0 carries.

Sprint 2 begins after Stage 10 closure. Sprint 2 scope per milestone-plan §3.4: UC-01 (single-agent), UC-05 (consent denial), UC-06 (token expiry mid-conversation), UC-07 (HR Admin asset issue, requires `it_assets_write_rest`), UC-08 (employee denied scope).
