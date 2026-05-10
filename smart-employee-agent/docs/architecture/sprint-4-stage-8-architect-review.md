# Sprint 4 — Stage 8: Architect Review

**Stage:** 8 (architect review — gate to Stage 9 implementation)
**Date:** 2026-05-10
**Reviewer:** architect-reviewer
**Branch (entry):** `sprint-3-build` @ `b497616`
**Inputs:** sprint-4.md, sprint-4-stage-1-product-review.md, sprint-4-stage-4-ux-design.md, sprint-4-stage-5-api-design.md (with amendment), sprint-4-tech-arch.md (with amendment), sprint-4-stage-7-slice-plan.md, sprint-3-tech-arch.md (rigour reference), api-contracts.md, module-layout.md, scope-policy.md, UC-11..UC-16. Spot-grep verified against `hr_server/`, `it_server/`, `orchestrator/`.

---

## §1. Verdict

**GO-WITH-CONDITIONS.**

Two structural conditions must clear before S4.3 begins. Neither is large.

1. **F-A — REST attach point unwired.** Stage 6 §3.B / §3.C and the slice plan add handlers to `hr_server/rest_api/server.py` and `it_server/rest_api/server.py`. Those files are not mounted at runtime — `hr_server/main.py:110-113` only mounts `build_hr_mcp_router` at `/mcp/tools`. Same for IT. Without a fix, S4.3's REST handlers would land code that returns 404 in production.

2. **F-B — Amendment drift in Stage 6.** Stage 5 and Stage 6 each carry a top-of-doc amendment "drop `roles`/`groups`", but Stage 6's §2.1 still adds `groups` to `JWTClaims`, §8.3/§8.4 still describes `_derive_roles_and_scopes`, and §9.1 still lists `groups` as pre-flight check #10. Implementer has to reconcile two specs of the same module.

Beyond these, OQ-3 is sound, the identity model is clean, and slice ordering is correct.

---

## §2. Summary

OQ-3's split — REST validator with list-audience including the orchestrator's MCP client; MCP-tool validator strictly `aud == hr_agent_client_id` — maps cleanly onto the read/write partition. Mutations remain reachable only via token-C with the agent audience; the broader REST audience is bounded by what `hr_read_rest` already grants. Decision A (dedicated REST handlers for approve/reject) is the right boundary for transcript hygiene. Identity model (`username` + `email`, no `employee_id`) is clean and consistently plumbed end-to-end. Slice ordering S4.1..S4.6 is correct.

The most likely-to-bite finding is F-A: handler code lands against a module that is not mounted, tests pass under `pytest`, but `curl http://hr_server:8000/api/me/leaves` returns 404 because no route is mounted. The fix is a new `build_hr_rest_router` factory + one `include_router` line in `main.py`, total ~30 minutes of work, but it must be in the *first* commit of S4.3 (with zero handlers) before any handler is added.

---

## §3. Findings table

| id | severity | finding | recommendation | landing slice |
|---|---|---|---|---|
| **F-A** | P0 | `hr_server/rest_api/server.py:42` and `it_server/rest_api/server.py` are not mounted by `main.py`. Stage 6 §3.B/§3.C plans handler additions to dead modules. Verified at `hr_server/main.py:110-113`. | Stage 6 amendment: declare a new wired router `build_hr_rest_router` (and IT counterpart), mounted from `main.py` next to the MCP router with prefix `/api`. Acceptance for S4.3 first commit: `curl http://hr_server:8000/api/healthz` returns 200. | S4.3 — first commit (router scaffold). |
| **F-B** | P0 | Stage 6 still references `groups`-claim derivation in §2.1, §8.3, §8.4, §9.1 — contradicts its own top-of-doc amendment. | Pre-S4.1 sweep: drop `JWTClaims.groups`, rename `_derive_roles_and_scopes` to `_derive_scopes`, remove pre-flight check #10, remove §8.3/§8.4/AR-6. | Pre-S4.1 — doc only. |
| **F-C** | P1 | `apply_leave` is referenced as "backend already shipped" in `sprint-4.md` §2 item 10, but `hr_server/mcp/tools.py` registers only `get_leave_balance`, `get_leave_history`, `approve_leave` — no `apply_leave` MCP entry. Service exists at `hr_server/service/hr_service.py:72`; FastMCP variant at `hr_server/mcp_server/server.py:234` is not wired. UC-13 chat path "apply for 3 days annual leave" cannot reach the service today via the wired router. EC4-7 fails. | Add `apply_leave` MCP tool in `hr_server/mcp/tools.py` as part of S4.5 (treat UC-13 as build-NEW for the MCP entry point). +1 R-test. | S4.5. |
| **F-D** | P1 | `username` claim absence on D4 / E1 must fail-closed (Stage 6 §2.4); without a test, a future refactor could quietly fall back to `claims.sub`. | Add one synthetic-token test asserting 401 with `ERR-AUTH-007` when `username` is absent. | S4.3. |
| **F-E** | P1 | Multi-turn cubicle keyword fallback (Stage 6 §6.2) handles four canonical phrasings only. Off-script ("can you put jane in cubicle twenty-seven") falls through. Demo runbook must list canonical strings as the tested surface. | Add canonical-phrasings list to Stage 11 demo runbook. One regex test per fallback rule (already in S4.4 plan). | S4.4 + Stage 11 runbook. |
| **F-F** | P1 | OQ-3 audience-segregation guarantee has no automated test today. Stage 6 §2.3 promises three assertions but the test must land in S4.3 alongside the new REST router, not later. | Land `tests/hr_server/rest_api/test_audience_segregation.py` in S4.3 first commit (after router scaffold). | S4.3. |
| **F-G** | P2 | After ~5–10 demo runs, cubicle store has accumulated assignments; "show vacant cubicles" output diverges from runbook expectations. | Stage 11 runbook step: hit `/reset` or `docker-compose restart hr_server` before each demo. | Stage 11 runbook. |
| **F-H** | P2 | Vestigial files `hr_server/rest_api/server.py` + `mcp_server/server.py` (and IT counterparts) confuse new contributors and use a retired scope vocabulary (`hr_self_mcp` etc per `hr_server/mcp_server/server.py:13`). | Delete in S4.3 alongside the new wired router. | S4.3. |

---

## §4. OQ-3 audit — audience-list lock, hostile path

**Resolution under review:** Stage 6 §2.3 — REST validator audience list = `[ORCHESTRATOR_MCP_CLIENT_ID, SPA_CLIENT_ID]`; MCP validator strictly `aud == HR_AGENT_OAUTH_CLIENT_ID` (verified at `hr_server/auth/validators.py:11`).

**Why this question deserves pushback.** A naive reading sounds like a smell ("we're widening the audience set on certain paths"). The pushback the design survives is that the path partition is aligned with the risk partition: broader-audience paths are read-only and scope-gated to `hr_read_rest` / `it_assets_read_rest`; mutations are reachable only via the strict-audience MCP path with token-C. The privilege envelope at the wider audience is bounded by what `hr_read_rest` already grants.

**Hostile path 1 — token-A from a different orchestrator reaches reporting endpoint.** Suppose an attacker stands up a parallel orchestrator with the same client ID and gets a real token-A for `hr_admin_user`. They call `GET /api/reports/cubicle-assignments` directly. Outcome: REST validator accepts audience, accepts issuer, accepts signature, accepts scope. The attacker reads cubicle assignments. **Severity:** the same data is reachable from any service that accepts `hr_read_rest` — the audience list does NOT widen the privilege envelope beyond what `hr_read_rest` already grants. Not a new exposure.

**Hostile path 2 — token-A presented to MCP `assign_cubicle`.** Token-A's audience is the orchestrator's MCP client; `assign_cubicle` is on `hr_server/mcp/tools.py` validated by the strict `HRServerTokenValidator`. Audience mismatch → 401. **Boundary holds.**

**Hostile path 3 — token-C presented to REST endpoint.** Token-C's audience is the HR agent's client; REST validator's audience list does not include it. 401. **Boundary holds.**

**Hostile path 4 — denylist on REST.** Sprint 3 Step-7 denylist lives only on the MCP validator (`hr_server/auth/validators.py`). REST validator at `hr_server/auth/jwt_validator.py:88` does sig/iss/aud/exp only. A logged-out admin's still-not-yet-expired token-A could be replayed to `GET /api/reports/cubicle-assignments` until expiry (~1h post-BCL). This is the deferred-introspection limitation surfacing on the new REST surface — it does not widen the limitation, just inherits it. Acceptable as Sprint 4 carry-over per memory `project_introspection_deferred.md`.

**The boundary holds for the demo-relevant paths.** The audience-segregation test in F-F is the architectural lock: if a future maintainer collapses the two validators into one, the test fails and forces a conversation.

**Operational note.** `docker-compose.yml:147,176` already wires `HR_EXPECTED_INBOUND_AUD=ORCHESTRATOR_MCP_CLIENT_ID`. OQ-3 formalises what was already wired since Sprint 1; it isn't a brand-new audience surface.

**Verdict on OQ-3:** sound. Land the audience-segregation test in S4.3 (F-F) and the boundary is locked.

---

## §5. Identity model audit

**Claim 1 — `username` flows from IS into every reporting row without a `sub` leak.**
Chain: IS access token → `JWTClaims.username` (Stage 6 §2.1) → REST handler reads → `hr_service.get_all_cubicle_assignments()` projection (Stage 6 §4.2) → response model `CubicleAssignmentItem.employee_username` (Stage 5 §A4). The store record holds `assigned_to_sub` as an internal join key and the projection drops it. `lookup_employee` (D5) returns `sub` because HR Agent needs it for CIBA `login_hint`, but the result is consumed in-agent and never propagated to chat or SPA.

If `username` is absent from the IS-issued token, the chain dies fail-closed (Stage 6 §2.4 returns 401 with `ERR-AUTH-007`). F-D adds the test that locks this.

**Claim 2 — IT seed migration is one-shot-clean.**
Stage 6 §5.1 rewrites `_SEED_ASSETS` to drop `employee_id` and rekey by `username`. Stage 6 §5.4 acknowledges `tests/it_server/mcp/test_tools.py` will break and is updated in the same slice. S4.2 groups the migration commit with the test-update commit. No residual `employee_id` references remain in the project after S4.2 lands.

**Claim 3 — UX consistency across surfaces.**
Chat reply: "Cubicle C-027 on floor 2 has been assigned to jane.doe (jane.doe@example.com)." Reports tables: Username + Email columns. Consent-widget action text: "Assign cubicle C-027 to jane.doe" (username only — emails would break widget layout). The granularity per surface is correct.

**Net:** identity model is clean. F-D test is the only condition.

---

## §6. Slice ordering audit

**Should S4.2 (IT seed migration) move later?** No. S4.3 introduces the parallel `_SEED_USERS` lookup table on the HR side; if IT lags S4.3, the two servers run with heterogeneous identity models mid-sprint (HR keys by username, IT still keys by `employee_id`). Front-loading at Day 1.5 isolates the migration churn into one slice. Moving it later does not reduce churn; it relocates it into S4.5 where it would entangle with SPA test instability.

**Should S4.4 split into 4a (read-side) / 4b (write-side)?** No. S4.3 already lands the cubicle read-side end-to-end (D1, D2, D4, D5, B3, A4); UC-11 turns 1+2 are walkable after S4.3. S4.4 is the write-side: D3 `assign_cubicle`, A2A `action_text`, SSE `action_text`, SPA widget propagation. These are tightly coupled — D3 without `action_text` plumbing means the SPA shows a generic widget for an admin write. Splitting creates a "demo broken between slices" gap.

**Sequencing risk in S4.5.** S4.5 is the largest slice (proxy + 4 read endpoints + Reports page with 3 tabs + My Leaves panel + IT report endpoint + SSE re-fetch wiring). If S4.5 ships shaky and the demo nears, the runbook's fallback narrative is UC-11 + UC-12 only (chat surfaces). Have this fallback narrative ready before Stage 11.

**Order with F-A in mind.** The new wired REST router (F-A fix) lands in S4.3 as the *first* commit, with zero handlers. Subsequent commits in S4.3 add handlers. This makes the F-A condition observable as a single mergeable commit; if it fails, only the router scaffold is reverted, and the rest of the slice stays clean.

**Net:** S4.1 → S4.2 → S4.3 → S4.4 → S4.5 → S4.6 is correct. The only sequencing addition is "S4.3 first commit = router scaffold + audience-segregation test, before any handlers."

---

## §7. Hidden risk surfaces

Three items not surfaced in Stage 6 / Stage 7. None block; all are demo-relevant.

**HR-1 — Cubicle store erodes between demo runs.**
Stage 6 §4.1 seeds 100 cubicles on container startup. Demo flows assign cubicles. After 5–10 demo runs without restart, "show vacant cubicles" diverges from the runbook's expected counts. F-G addresses with a runbook step.

**HR-2 — `username` PII in audit logs.**
Sprint 1–3 audit logs use `claims.sub` (verified at `hr_server/mcp/tools.py:306-310`). Stage 6 adds `username` extraction but does not propose changing the audit log. Keep `claims.sub` as the primary audit identifier so the log volume doesn't double and `tools/grep-trace.sh` patterns continue to work. `username` is for user-facing surfaces only. Document this convention in Stage 6 §2.

**HR-3 — `apply_leave` not in the wired MCP surface.**
This is the real-world version of F-C. The PM brief and `sprint-4.md` §2 item 10 inherit a "backend already shipped" assumption that does not survive a grep against the wired tool registry. Stage 6 §3.B does not add `apply_leave` to `hr_server/mcp/tools.py`; only the cubicle tools are added. Without F-C's fix, the chat path for "apply for 3 days" cannot reach the service. EC4-7 fails. Confirm at S4.1 whether this is added to S4.5 (recommended) or whether EC4-7 is rescoped to "panel-only".

---

## §8. Recommendations for the implementation team

Three pointers.

1. **First commit of S4.3 is the router scaffold + audience-segregation test, no handlers.** F-A and F-F together. Mount `build_hr_rest_router` from `hr_server/main.py` next to the MCP router with prefix `/api`. Single healthz route under `/api/healthz`. Land the audience-segregation test (token-A → REST 200, token-A → MCP 401, token-C → REST 401). Both clear together; subsequent commits add handlers.

2. **Sweep Stage 6 for `groups` references before S4.1 opens.** F-B. 20 minutes of editing. Drop the field from `JWTClaims`, rename the helper, drop check #10 from the pre-flight script, drop §8.3/§8.4. Re-emit Stage 6.

3. **Pre-stage demo rehearsal must use the canonical phrasings only.** F-E. The four cubicle phrasings in Stage 4 §5 are the demo's tested surface. Off-script phrasings work *probably* via the LLM but are not guaranteed. Keep the runbook tight.

---

## §9. Stage exit gate

**GO-WITH-CONDITIONS.**

- **C-1 (P0):** F-A — wire a real REST router for `hr_server` and `it_server`. Stage 6 amendment + S4.3 first commit. ~1 hour total.
- **C-2 (P0):** F-B — sweep Stage 6 to remove `groups` references contradicting the top amendment. Pre-S4.1. ~20 minutes.
- **C-3 (P1):** F-C, F-D, F-E, F-F — accept into the slice plan as listed in the findings table. Total ~3 hours of work spread across S4.3/S4.4/S4.5.
- **C-4 (P2):** F-G, F-H — runbook + cleanup. Track in PR descriptions; close at sign-off if untouched.

Once C-1 and C-2 are addressed, Stage 9 implementation can begin at S4.1.

---

## §10. References

- `docs/architecture/sprint-4.md` — Stage 3 binding plan
- `docs/architecture/sprint-4-stage-1-product-review.md` — Stage 1 PM brief (historical for narrative)
- `docs/architecture/sprint-4-stage-4-ux-design.md` — Stage 4 UX
- `docs/architecture/sprint-4-stage-5-api-design.md` — Stage 5 API contract
- `docs/architecture/sprint-4-tech-arch.md` — Stage 6 tech-arch
- `docs/architecture/sprint-4-stage-7-slice-plan.md` — Stage 7 slice plan
- `docs/architecture/sprint-3-tech-arch.md` — rigour reference
- `hr_server/main.py:110-113` — runtime mount (only `build_hr_mcp_router`)
- `hr_server/auth/validators.py:11` — strict MCP validator (`aud == HR_AGENT_OAUTH_CLIENT_ID`)
- `hr_server/auth/jwt_validator.py:88-127` — REST validator with list-audience
- `hr_server/rest_api/server.py:42` — vestigial stub (not mounted) — F-A
- `hr_server/mcp/tools.py:257,321,385` — wired MCP tool patterns; no `apply_leave` (F-C)
- `hr_server/service/hr_service.py:72` — `apply_leave` service function (F-C)
- `it_server/main.py` — IT mirror of the structural gap
- `docker-compose.yml:147,176` — `HR_EXPECTED_INBOUND_AUD=ORCHESTRATOR_MCP_CLIENT_ID` already wired

End of Sprint 4 — Stage 8 architect review.
