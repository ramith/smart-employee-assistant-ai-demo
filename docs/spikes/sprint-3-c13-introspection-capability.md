# C13 — Does WSO2 IS introspect CIBA-issued OBO tokens as inactive after parent token revoke?

**Sprint:** 3 (Stage 4 BLOCK-A mitigation; runs on 3A Day 1)
**Author:** Stage 4 review (architect-reviewer + security-engineer)
**Outcome documented as:** F-21 in `docs/architecture/sprint-1-fixes.md`

## Why this matters

Sprint 3's locked Option A design relies on **two layers of defense** against captured-token replay after logout:

1. **Denylist** — orchestrator fan-outs `/internal/events` to all 4 receivers; receivers add `jti` to in-process denylist; subsequent calls are rejected within 1 s. **Primary security boundary.**
2. **Introspection backstop** — MCP servers introspect on cache miss; if `active=false`, return 401 within ≤60 s (cache TTL). **Required for half-fan-out failures (R-LOGOUT-7) and orchestrator-crash mid-flow (R-1, EX-4).**

The introspection backstop **assumes that revoking token-A at IS makes child OBO tokens (token-B, token-C) introspect as inactive**. This is *expected* by OIDC convention but **never empirically verified** on WSO2 IS 7.2 for CIBA-issued grants.

F-19 already showed IS does NOT treat CIBA-issued tokens as session-bound for BCL fan-out. **C13 closes the parallel question for introspection.**

## What we're testing

| Scenario | Expected result | Outcome maps to |
|---|---|---|
| C13.1 (PASS path) | Revoke token-A → introspect token-B → `active=false` within IS-side propagation latency | Backstop honest; §5 error matrix as drafted; SECURITY-DEGRADED label fires only on the all-legs-fail row. |
| C13.2 (FAIL path) | Revoke token-A → introspect token-B → `active=true` | Backstop NOT honest; rewrite §5 to mark half-fan-out and orchestrator-crash rows as SECURITY-DEGRADED until natural TTL (1 h). Add a runbook step: on partial failure ERROR, restart receivers. |

## Prerequisites

- WSO2 IS 7.2.0 reachable at `https://13.60.190.47:9443`.
- `employee_user` (Pattern C subject) with HR-AGENT app subscribed to scope `hr_basic_rest`.
- The C12 spike rig is **NOT required** for C13 (introspection is a direct call, no BCL listener needed).
- Existing capability-test scaffolding in [`idp_capability_test/`](../../idp_capability_test/) — model on `c11_role_denial.py`.

## Probe outline (auto + manual)

### Auto-probe (`idp_capability_test/c13_introspection_capability.py` — to author 3A Day 1)

```python
"""
C13 — IS introspection of OBO tokens after parent token revoke.

Steps:
1. Pattern C login as employee_user → token-A (with jti_a).
2. Initiate CIBA from HR-AGENT for scope=hr_basic_rest → token-B (with jti_b).
3. Confirm: introspect token-B → active=true (sanity).
4. Revoke token-A via /oauth2/revoke.
5. Wait 5 s (IS propagation budget).
6. Introspect token-B again. Record active=true|false.
7. Verdict:
   - active=false → C13.1 PASS, F-21 PASS — backstop holds.
   - active=true  → C13.2 FAIL, F-21 FAIL — denylist is load-bearing.
"""
```

### Manual recipe (operator runs in <10 minutes)

1. Bring up the demo stack: `make demo-up`.
2. Sign in as `employee_user` in the SPA.
3. Trigger any HR query (e.g., "What's my leave balance?"), approve consent — this mints token-B in HR-AGENT.
4. From the orchestrator container, capture token-A: `docker compose exec orchestrator python -c "from orchestrator.session_store import _SESSIONS; ..."` (one-liner to print `session.token_a.access_token`). Capture token-B from `hr-agent`'s `_token_cache`.
5. Sanity-check both introspect as active:
   ```
   curl -k -s -u <orchestrator-mcp-client>:<secret> \
     -d "token=<token_a>" \
     https://13.60.190.47:9443/oauth2/introspect | jq
   # expect: {"active": true, ...}

   curl -k -s -u <hr-agent-client>:<secret> \
     -d "token=<token_b>" \
     https://13.60.190.47:9443/oauth2/introspect | jq
   # expect: {"active": true, ...}
   ```
6. Revoke token-A:
   ```
   curl -k -s -u <orchestrator-mcp-client>:<secret> \
     -d "token=<token_a>" \
     https://13.60.190.47:9443/oauth2/revoke
   # expect: HTTP 200, no body or {"...": ...}
   ```
7. Wait 5 seconds.
8. Re-introspect token-B:
   ```
   curl -k -s -u <hr-agent-client>:<secret> \
     -d "token=<token_b>" \
     https://13.60.190.47:9443/oauth2/introspect | jq
   ```
9. **Record outcome:**
   - `{"active": false, ...}` → **F-21 PASS**. Capture in `sprint-1-fixes.md` §F-21.
   - `{"active": true, ...}` → **F-21 FAIL**. Capture in `sprint-1-fixes.md` §F-21 with the implication: *"WSO2 IS 7.2 does not link CIBA-issued OBO tokens to the parent token's revocation state; introspection cannot be relied on for revocation propagation. Denylist is the only enforcement layer for half-fan-out and orchestrator-crash scenarios. Demo runbook must surface SECURITY-DEGRADED state on partial fan-out failures."*

## Time-box

≤30 minutes including operator setup and result documentation. If the probe takes longer, suspend and escalate to a Stage 4 reviewer — the answer drives §5 of the tech-arch and the §3 risk language in Stage 1, and Sprint 3A implementation cannot start without it.

## Result-handling matrix

| Outcome | Action |
|---|---|
| F-21 PASS | Capture in `sprint-1-fixes.md` §F-21. No design changes. Proceed with 3A.1. |
| F-21 FAIL | Capture in `sprint-1-fixes.md` §F-21. Patch tech-arch §5 rows 1, 2, 3 to SECURITY-DEGRADED. Update demo-runbook with operator-action note ("on logout_fanout_partial WARN, restart affected receivers within demo window"). Add R-LOGOUT-7b acceptance: ERROR label is emitted for all-legs-failure (no behavioural change to fan-out, just log signal). Proceed with 3A.1 with documented caveat. |

## Why this is run *before* implementation, not during

The §5 error matrix's backstop story is the design's claim to robustness in front of stakeholders. If the claim is wrong, demo runbook narration changes — and so does the engineer's mental model of "what defends what". Better to know this on Day 1 than discover it during 3A.4's manual walkthrough.
