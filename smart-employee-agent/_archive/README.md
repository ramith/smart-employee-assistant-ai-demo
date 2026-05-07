# `_archive/` — historical content (do not implement against)

This folder holds artifacts from earlier versions of the POC that are no longer current but are kept for traceability.

| Path | What it is | When | Why archived |
|---|---|---|---|
| `agent.before-v3/` | Legacy single-process agent code (Asgardeo SDK, embedded Pattern C OBO via `obo_flow.py`). | Pre-M0 | v3 architecture pivot to orchestrator + specialists. Patterns are still useful as reuse anchors (`session.py`, `agent_auth.py` token-cache, `callback_html()`); see [`docs/milestone-plan.md`](../docs/milestone-plan.md) §3 Sprint 1 task hints. |
| `probes-v3-rfc8693/` | Bash probes from M0 spike that targeted Asgardeo SaaS + RFC 8693 token-exchange. `p0`, `p1`, `p10`, `_env.sh`. | M0 spike | Replaced by Python suite at `idp_capability_test/`. RFC 8693 path is a dead-end on IS 7.2 per F4. The bash logic is preserved in case anyone needs to re-validate against an Asgardeo tenant. |
| `poc-vision-pre-m0.md` | Pre-M0 strategic vision document (UAE PASS, API Manager MCP Hub, governance workspace, etc.). | Pre-M0 | Out of scope for the v4 demo; superseded by current architecture docs. Kept for external positioning / conference narrative. |

For the **current architecture**, start at [`docs/milestone-plan.md`](../docs/milestone-plan.md) and [`docs/spikes/wso2-is-capability-memo.md`](../docs/spikes/wso2-is-capability-memo.md).
