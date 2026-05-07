# Sprint 2 Retrospective

**Sprint:** 2 (post-CIBA-pivot, demo-critical + error-paths)
**Branch:** `sprint-1-build` (branch name carried over; rename to `sprint-2-build` deferred)
**Cadence:** 2A (demo-critical) → 2B (error paths) per Stage-1 PM review
**Started from:** Sprint 1 sign-off (`docs/architecture/sprint-1-signoff.md`)
**Closing tests:** 822 / 43 files green

## Slices delivered

| Slice | Deliverable | Commit | Status |
|---|---|---|---|
| Stage 1 | Product/PM review, decisions locked in `sprint-2-stage-1-product-review.md` | — | ✓ |
| 2A.1 | `_rest` scope-policy rewrite, per-tool CIBA scope dispatch, write-tool keyword rules | `db23805` | ✓ |
| 2A.2 | D2.7 + D2.8 — HR Admin write paths (`issue_asset` greenfield in `it_server`, write tier scopes) | `771d5b8` | ✓ |
| 2A.3 | D2.9 + UC-08 — identity-first denial path, ERR-MCP-003 user copy, args-missing fast-fail | `bcba53c` + `b6762a2` | ✓ |
| 2A.4 | D2.4 — audit correlation: clean WARN scoping, SPA-originating rid, in-app trace panel, `grep-trace.sh`, demo-runbook walkthrough | `2f58fcc` | ✓ |
| 2B.1a | D2.1 — agent-aware deny copy in chat fragment | `2a58e20` | ✓ |
| 2B.1b | D2.5 — per-agent OBO token cache + re-CIBA on near-expiry/expired with `is_refresh=True` + `prior_consent_at` | `c040ff9` | ✓ |
| 2B.2 | D2.2 + D2.3 — SSE-disconnect cancel hook + auth_req_id-expiry integration test | `5ba728f` | ✓ |
| 2B.3 | A-3 multi-agent routing label (`tool_index` + `total_tools`) + N19 all-deny test | `f2ac0ca` | ✓ |

## Live-verified end-to-end (browser walkthrough)

As `employee_user` (sub `2048ad8c-…`) and `hr_admin_user` (sub `15fab9e7-…`) against WSO2 IS 7.2.0 at `13.60.190.47:9443`:

- UC-02 (single read), UC-03 (multi-agent serial), UC-07 (HR Admin write), UC-08 (Employee identity-first denial) — all green.
- Audit correlation: rid `22ce8467-…` reproduced the full SPA → orchestrator → hr_agent → IS → hr_server chain in causal order via `./scripts/grep-trace.sh`. The user-approval gap (≈8 s between IS poll #1 fail and poll #2 success) is visible as a contiguous block of log lines under one rid.
- F-18 confirmed live: silent scope downgrade by IS surfaces as ERR-MCP-003 at the resource server with the expected user-facing copy ("You don't have permission to perform this action…").

## What went well

- **Stage-gated cadence held.** PM review → tech arch → slice-by-slice implementation → test → manual verify → commit. No mid-sprint scope drift.
- **2A.4 review-and-revise loop produced a tighter slice.** Four agents (architect, python-pro, security-engineer, frontend-developer) reviewed in parallel before code froze. Their findings (WARN-before-set bug, runbook over-claiming, UUID validation in `grep-trace.sh`, SPA-originating rid) all landed before the slice closed.
- **Token cache (D2.5) didn't break the demo.** Cache lookup is non-breaking for empty cache; the existing happy-path / denied / expired / timeout dispatcher tests remained green throughout.
- **In-app trace panel** turned out to be more useful than expected during manual testing — surfacing the live SSE timeline next to the chat made it trivial to spot which leg of UC-03 was blocking.

## What hurt

- **Container-image cache-bust friction.** Every code change to `common/logging` or any agent required `docker compose build` + `up -d` before the running stack picked it up, and the SPA's static assets were cached by the browser even after a rebuild. Fix: hard-reload (Cmd+Shift+R) or DevTools → Disable cache. Document in the next-sprint runbook.
- **Session loss on orchestrator restart.** The orchestrator's session store is in-process memory; every rebuild forces the user to sign in again. Q5 explicitly accepted this for the POC, but it slowed manual-test iteration. Sprint 3 candidate: persist sessions in Redis.
- **WARN noise on first manual test.** `X-Request-ID absent` fired for `/`, `/app.js`, `/styles.css`, `/auth/login`, `/agent-callback`, `/events/<sid>` — all paths where the browser cannot or should not send custom headers. Fixed in 2A.4 by scoping the WARN to "real" application requests.

## Bug ledger (manual-test surface area)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| B-19 | UC-08 returned `ERR-MCP-005` instead of `ERR-MCP-003` | Dispatcher's catch-all `except httpx.HTTPStatusError` ignored upstream `error_id` | Parse 401 body for `detail.error_id` (2A.3) |
| B-20 | "Issue laptop macss033" returned "MBP-14-001 issued to default" | Dispatcher kwargs_builder filled with canonical defaults | Per-tool `_REQUIRED_ARGS` + fast-fail with ERR-AGENT-002 (2A.3 polish) |
| B-21 | Multi-agent chip showed "Routing to IT Agent…" first because routingCount logic was wrong | SPA only knew tool count after seeing both events | A-3: orchestrator emits `tool_index` + `total_tools` (2B.3) |
| B-22 | "X-Request-ID header absent" WARN on every static asset GET | WARN-on-missing applied to all paths | Path allow-list + prefix match for `/events/` (2A.4) |
| B-23 | The auto-generated WARN itself rendered `request_id=-` | ContextVar set after WARN emission | Set BEFORE warn (2A.4) |
| B-24 | Every chat message triggered a fresh CIBA even seconds later | No token cache | Per-`(user_sub, scope)` cache with 30 s expiry buffer (2B.1b) |

## Carry-overs for Sprint 3+

| Item | Reason deferred | Owner |
|---|---|---|
| Logoff (RP-initiated logout + token revocation + agent fan-out cancel) | Cross-system design effort; needs its own Stage 1 | Council |
| N21 / N22 / N25 explicit replay & cross-aud tests | Existing aud + scope guards already block these paths; explicit tests are nice-to-have but blocked on a less-mocked test fixture | QA |
| N24 singleflight on parallel CIBA for same (user, agent, scope) | New cache state machine; no current symptom in the demo | Backend |
| N27 usability test (consent fatigue under serial fan-out) | Manual moderated bar; out of automation scope | UX |
| EX-2 abort-whole-request on first denial | Per current behaviour partial-result reads better; revisit if stakeholder feedback says otherwise | PM |
| Persistent session store (Redis) — eliminates restart-loss-of-session friction | Q5 explicitly accepted single-process for POC | Backend |
| Branch rename `sprint-1-build` → `sprint-2-build` (or `main` merge) | Branch name confusion; defer to Sprint 3 kickoff | Git wrangler |

## Decisions confirmed in Sprint 2

- Per-agent CIBA architecture (M0 council, F1–F7) — unchanged.
- `_rest` single-tier scope naming — locked in `docs/scope-policy.md`.
- Path C is the IS denial behaviour; agents parse upstream `error_id` to surface ERR-MCP-003 cleanly (F-18).
- Token cache is per-(user, scope) per-dispatcher; no shared state across agents; 30 s expiry buffer; cache write only after MCP call succeeds.
- Audit correlation = rid propagation via `X-Request-ID` (in-app + server logs) + IS audit row joined by `auth_req_id` + `act.sub` + timestamp. **NOT** cryptographic log integrity (out of scope per F-16).
- Logoff is its own future design effort; for the demo, role switching uses a fresh browser/incognito.

## Numbers

- 9 commits across Sprint 2 (Stage 1 review + 8 implementation slices).
- 822 / 43 files of green tests at close (up from 808 at Sprint 1 sign-off).
- 1 backend file rebuilt twice on average per slice (`docker compose build` cycles).
- 0 force-pushes; cumulative ahead-of-main commits.
