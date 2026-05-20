# Sprint 2 — Stage 1: Product Team Review

**Date:** 2026-05-08
**Reviewers:** PM (voltagent-biz:product-manager), BA (voltagent-biz:business-analyst)
**Inputs:** `docs/milestone-plan.md` §3.4 (D2.1..D2.11), `docs/use-cases/UC-04..UC-08`, `docs/architecture/sprint-1-retro.md`, live code state.

---

## §1. Demo arc (PM)

**The single most compelling Sprint 2 demo:** *Role-based denial paired with HR Admin write scope.*

Three-minute story:
1. HR Admin logs in → asks "issue a MacBook Pro 14 to alice@example.com" → consent widget → approve → asset assigned.
2. Sign out, sign in as Employee → ask the same question → orchestrator says "you don't have permission" — *no token issued, no consent prompt shown.*
3. Show the IS audit log: zero write-tier tokens for the Employee.

Identity-first governance is the narrative wedge. Sprint 1 only proved consent; Sprint 2 proves *denial* and *role-bound capability*.

---

## §2. Slice recommendation (PM)

§3.4 promises ~5 working days for 11 deliverables. **Not realistic.** Recommended split:

**Sprint 2A (demo-critical, ~3.5 days):**
- D2.7 — HR Admin invokes `approve_leave` (`hr_approve_rest`)
- D2.8 — HR Admin invokes `issue_asset` (`it_assets_write_rest`) — UC-07
- D2.9 — Employee denied at IS for write scopes — UC-08
- D2.4 — `X-Request-ID` correlation at every hop (council requirement)

**Sprint 2B (supporting error paths, ~2 days, carries if 2A slips):**
- D2.1 — Deny widget UX + graceful fallback — UC-04
- D2.2 — Browser-closed mid-CIBA detection — UC-05
- D2.3 — `auth_req_id` expiry timeout
- D2.5 — Token expiry → re-CIBA — UC-06
- D2.6 — N18..N26 + N29..N34 written and passing

**Already done in Sprint 1:** D2.10 (canonical 4-tier scope rename) per retro §1.

---

## §3. Top risks (PM)

| # | Risk | Mitigation |
|---|---|---|
| R-1 | IS 7.2 may deny write scopes at *consent screen* (`access_denied`) instead of *initiation* (`invalid_scope`). UC-08 design assumes initiation-time denial; if IS uses the other path the error-classification + copy-deck branches invert. | **Day 1 spike:** run `idp_capability_test/c11_role_denial.py` (does not exist yet — author it Day 1) against live IS. Document outcome in `docs/spikes/`. Blocking for N30/N31 fixtures. |
| R-2 | N-tests are mocked-IdP. Sprint 1 found 13 walkthrough bugs none of which were caught by the mocked 810-test suite. | Manual live-IS walkthrough required for each acceptance criterion before sign-off. Allocate Day 5 for this. |
| R-3 | Token-expiry re-CIBA (D2.5) introduces race conditions in multi-specialist flows: HR token expires while IT is mid-poll → both threads write the same session entry. | Per-`(session_id, agent_id)` asyncio lock on token-cache writes. Architecture review at Stage 4. |

---

## §4. BA gaps (concrete blockers, code+config)

These are real implementation gaps surfaced by the BA review. None can be hand-waved at Stage 4; each needs an explicit Stage 6 task or a doc/config update.

| # | Gap | Owner | Stage |
|---|---|---|---|
| G-1 | `scope-policy.md` describes a `_a2a` / `_mcp` two-tier split that does not match the live `_rest`-suffix code. Stale Asgardeo-era doc. | Tech writer | Stage 1 close-out |
| G-2 | `HRAgentConfig.ciba_scope` and `ITAgentConfig.ciba_scope` are single env-sourced strings. UC-07 requires the dispatcher to pick scope *per tool_id* at A2A receive time. No mechanism exists. | python-pro | Stage 4 → Stage 6 |
| G-3 | `orchestrator/chat/keyword_fallback.py` `DEFAULT_RULES` has no entries for write tools (`hr.approve_leave`, `it.issue_asset`). | python-pro | Stage 6 |
| G-4 | `it_server/mcp/tools.py` has no `issue_asset` endpoint — greenfield add (router + Pydantic models + scope guard + canned data write). | python-pro | Stage 6 |
| G-5 | `c11_role_denial.py` capability probe does not exist. Required before N30/N31 fixtures can be written with the correct mock. | user (manual run) | Stage 1 close-out — Day 1 |

---

## §5. WSO2 IS pre-flight checklist (BA)

The user must complete these IS Console actions before any Sprint 2 manual verification can run:

1. **Register new scope** `it_assets_write_rest` on the IT API Resource. Confirm `hr_approve_rest` is also registered (may have been omitted in Sprint 1 since unused).
2. **Role assignments:**
   - `HR Admin`: `hr_read_rest`, `hr_approve_rest`, `it_assets_write_rest` (plus existing reads).
   - `Employee`: confirm only `hr_basic_rest`, `hr_self_rest`, `it_assets_read_rest` (no write scopes).
3. **Agent OAuth Apps' Allowed Scopes:** add `hr_approve_rest` to HR Agent's app; add `it_assets_write_rest` to IT Agent's app. Without this, IS rejects CIBA initiation even for HR Admin.
4. **Notification channel:** confirm IT Agent's OAuth App has `Notification Channel = External` (same as HR in Sprint 1).
5. **Sanity-grep:** the D2.10 acceptance criterion (`grep` for `hr\.read|hr\.write|it\.read|it\.assign` returns zero hits) must include `scope-policy.md` after G-1 is fixed.

---

## §6. N-test inventory (BA — exhaustive)

PM's slice 2A directly exercises N32, N33, N34, N30, N31. Slice 2B exercises N18, N19, N20, N23, N29 and the N24 singleflight (currently unspecified — see G-2). Test bodies for **none** of these exist; all need authoring in Sprint 2.

D2.6 acceptance criterion ("N18..N26 passing") requires a Stage 4 architectural decision on N24 (singleflight key shape: `(user_sub, agent_id, scope_set)` per BA recommendation), which the current dispatcher pending-map does not support.

---

## §7. Acceptance criteria for Sprint 2 sign-off

A stakeholder can verify in ~90 seconds:

1. **Identity-first denial works.** Employee asks for write action → no token issued → audit log shows zero write-tier tokens for that user. (D2.9, N30/N31)
2. **HR Admin write paths succeed.** "Approve leave LV-004" and "Issue MacBook Pro 14 to alice" both work end-to-end. (D2.7, D2.8, N32, N33)
3. **Consent denial is graceful.** Click Deny → "Request cancelled" instead of error. (D2.1, N12/N18/N19)
4. **Audit chain reconstructs by request_id.** `grep <rid>` across all service logs returns the full hop chain. (D2.4)
5. **Token expiry re-prompts.** Mid-conversation expiry → amber "Re-approve" widget. (D2.5, N29)
6. **Tests green.** `tools/run-tests.sh` passes including N18..N26, N29..N34.

---

## §8. Decisions needed before Stage 2 (UX)

Before this review can move to Stage 2, the following decisions are required from the product owner:

| # | Decision | Default | Recommendation |
|---|---|---|---|
| Q1 | Adopt PM's 2A/2B split, or attempt full §3.4 in one sprint? | full §3.4 | **Adopt 2A/2B split.** The combined scope is too big for 5 working days even before BA gaps G-2..G-4 are factored in. |
| Q2 | Is the 2A demo arc (HR Admin write + Employee denial) the right wedge? | yes | **Yes.** Highest story-value-per-day in the backlog. |
| Q3 | Schedule Day 1 for the C11 probe + IS pre-flight checklist? | yes | **Yes.** Both are unblocking for Stage 4 and Stage 6. |
| Q4 | Update `scope-policy.md` (G-1) before or after Stage 2? | before | **Before.** UX/Stage 3 references scope semantics. |
| Q5 | Carry A-1 (CI smoke test) and A-3 (multi-agent attribution) into 2A or 2B? | 2B | **2B.** Both are quality gates, not demo-critical. |

---

## §9. Stage hand-off

**Decisions locked (2026-05-08):**

- **Q1:** Split adopted. Slices sized to fit context window + don't-break-Sprint-1 safety net (each slice ends with `tools/run-tests.sh` green + UC-03 walkthrough).
- **Q2:** Demo wedge confirmed = identity-first governance (HR Admin can; Employee denied at IdP, no token issued).
- **Q3:** Day 1 = C11 probe + WSO2 IS pre-flight (5 console tasks).
- **Q4:** `scope-policy.md` rewrite folded into 2A.1.
- **Q5:** Sprint 1 carries A-1 (CI smoke) and A-3 (multi-agent label) → 2B.3.

**Out of scope — RP-Initiated Logout.** Briefly considered for inclusion (UC-08 demo silently re-uses the IS session if the user only signs out from the orchestrator). Decision: cross-system logoff (RP-initiated `/oidc/logout`, token-A revocation, fan-out cancel of pending CIBA flows in agents, ordered SSE teardown) is its own design effort and is **not in Sprint 2**. Workaround for the UC-08 manual demo: clear browser cookies for the IS host between role switches, or use an incognito window per role. Document the workaround in the demo runbook when 2A.3 ships.

**Final slice plan:**

| Slice | Scope | Why first | Demo-visible outcome |
|---|---|---|---|
| **2A.1** | Foundation: G-1 (scope-policy.md), G-2 (per-tool CIBA scope), G-3 (write-tool keyword rules) | Unblocks 2A.2 + 2A.3; no user-visible change | (none — backend refactor) |
| **2A.2** | D2.7 + D2.8 (HR Admin write paths, G-4 `issue_asset` greenfield) | New tools needed before denial demo | "Approve LV-004" + "Issue MacBook to alice" both work end-to-end as HR Admin |
| **2A.3** | D2.9 + UC-08 (identity-first denial) | The demo wedge | Employee asks "issue laptop" → no token issued → graceful denial copy. Demo workaround: incognito window per role (logoff out of scope) |
| **2A.4** | D2.4 (audit correlation) | Council requirement; fits any time before sprint close | `grep <request_id>` reconstructs full hop chain |
| **2B.1** | D2.1 + D2.5 (deny widget UX + token-expiry re-CIBA) | Error paths | Click Deny → "cancelled"; expired token → "Re-approve" amber widget |
| **2B.2** | D2.2 + D2.3 (browser-closed + auth_req_id expiry) | Error paths | Closing browser mid-CIBA cancels the polling task |
| **2B.3** | D2.6 (N-tests) + A-1 (CI smoke) + A-3 (multi-agent label) | Quality gates | Green CI; multi-agent results show both agent badges |

Stage 1 closed. Proceeding to Stage 2 (UX) for **2A.1** — backend-only refactor; UX impact = none.
