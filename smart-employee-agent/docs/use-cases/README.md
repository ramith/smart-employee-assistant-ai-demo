# Use Cases — Sprint 1

This folder contains formal use cases for Sprint 1 of the smart-employee-agent POC. Each UC follows the template defined in this README.

## Sprint 1 scope

Sprint 1 = orchestrator + 2 specialist agents + per-agent CIBA + serial fan-out + happy path. See [`../milestone-plan.md`](../milestone-plan.md) §3 for backlog.

## Use case template

```markdown
# UC-NN — <name>

**Sprint:** 1
**Priority:** Critical / High / Medium
**Maps to N-tests:** <list of N-test IDs>
**Maps to scenarios:** <user-experience.md scenario IDs>

## Actors
- Primary: <who initiates>
- Secondary: <other participants>

## Preconditions
- <state required before UC can run>

## Trigger
<the event that starts the UC>

## Main flow
1. <numbered step>
2. ...

## Exception flows
### EX-1 — <name>
1. <step where exception occurs>
2. <recovery>

## Postconditions
- Success: <state after happy path>
- Failure: <state after exception>

## Design notes for downstream stages
- UX: <hints for Stage 3>
- Architecture: <hints for Stage 4>
- Testing: <hints for Stages 7–8>
```

## Sprint 1 UCs

| ID | Name | Priority | UX scenario | Status |
|---|---|---|---|---|
| [UC-01](UC-01-user-login.md) | User logs in (Pattern C) | Critical | Scenario A | written |
| [UC-02](UC-02-single-specialist-query.md) | Single-specialist query | Critical | Scenario B (single) | written |
| [UC-03](UC-03-two-specialist-serial-query.md) | Two-specialist serial query (headline demo) | Critical | Scenario B | written |
| [UC-04](UC-04-user-denies-consent.md) | User denies a CIBA consent mid-flow | High | Scenario B-1 | written |
| [UC-05](UC-05-browser-closed-during-ciba.md) | Browser closed during CIBA polling | High | Scenario D-7 | written |
| [UC-06](UC-06-token-expiry-mid-conversation.md) | Token expiry mid-conversation (re-CIBA) | Medium | §7.3 | written |
| [UC-07](UC-07-hr-admin-issues-asset.md) | HR Admin issues IT asset (write-scope demo) | High | Scenario B (HR Admin variant) | written; **Sprint 2 build** |
| [UC-08](UC-08-employee-denied-scope.md) | Employee requests denied scope (role-based denial) | High | §7.4 | written; **Sprint 2 build**; demonstrable verbally in Sprint 1 Act II |

## Sprint 3 UCs (revocation)

| ID | Name | Priority | UX scenario | Status |
|---|---|---|---|---|
| [UC-09](UC-09-logout-cascade.md) | User signs out (logout cascade) | Critical | Sprint 3 Act II | written; **Sprint 3 build** |
| [UC-10](UC-10-admin-terminate.md) | Admin terminates user session via IS Console | High | Sprint 3 Act III | written; **Sprint 3B build**; gated on F-20 verification |

## Sprint 4 UCs (business pivot)

Stage 3 sprint plan: [`docs/architecture/sprint-4.md`](../architecture/sprint-4.md). Identity model: `username` + `email` claims (no `employee_id`, see sprint-4.md §7). Reporting data flow: orchestrator-proxied (sprint-4.md §8).

| ID | Name | Priority | Build status | Status |
|---|---|---|---|---|
| [UC-11](UC-11-hr-admin-assigns-cubicle.md) | HR Admin assigns cubicle (multi-turn, 4 floors) | Critical | NEW (data model + 4 MCP tools + 1 new scope `hr_assets_write_rest`) | written |
| [UC-12](UC-12-employee-self-service-asset-discovery.md) | Employee self-service asset discovery | Critical | NEW (1 new scope `it_assets_self_rest`) | written |
| [UC-13](UC-13-employee-applies-for-leave.md) | Employee applies for leave | High | hybrid (backend Sprint 1; My Leaves panel NEW) | written |
| [UC-14](UC-14-employee-checks-own-leave-status.md) | Employee checks own leave status | High | hybrid (backend Sprint 1; panel shared with UC-13) | written |
| [UC-15](UC-15-hr-admin-pending-leaves-table.md) | HR Admin pending-leaves table | High | hybrid (backend exists, Reports tab NEW) | written |
| [UC-16](UC-16-hr-admin-assignment-reporting-tables.md) | HR Admin cubicle + device reporting tables | High | NEW | written |
