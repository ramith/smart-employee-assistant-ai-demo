# UC-02 — Single-specialist query (HR-only or IT-only)

**Sprint:** 1
**Priority:** Critical (foundation for UC-03)
**Maps to N-tests:** N1, N4 (specialist validates inbound token), N7 (peer-trust allowlist), N16 (cross-aud rejection at MCP), N21 (cross-specialist replay), N22 (actor_token reuse), N25 (direct MCP bypass), N28 (client_id collision detection)
**Maps to scenarios:** [user-experience.md](../user-experience.md) Scenario B (single-specialist subset)

## Actors
- **Primary:** User (in chat)
- **Secondary:** SPA Consent Widget, Orchestrator (LLM + router), HR Agent (or IT Agent), HR Server / IT Server (MCP), WSO2 IS, the agent's auto-created Agent App (CIBA initiator)

## Preconditions
- UC-01 has succeeded — orchestrator has a valid session with token-A.
- Chosen specialist (HR or IT) registered and reachable; its OAuth Agent App has CIBA grant + External notification channel enabled.
- The corresponding MCP server is reachable and validates `aud == agent's OAuth Client ID` AND `act.sub == that-agent's-id`.

## Trigger
User types a single-purpose message, e.g., *"What's my leave balance?"* (HR only) or *"What laptops are available?"* (IT only) and hits Enter.

## Main flow
1. SPA `POST <orch>/api/chat` with `{session_id (cookie), user_message}` and `X-Request-ID: <uuid>`.
2. Orchestrator's LLM (OpenAI, with deterministic keyword fallback for demos) inspects the message and decides which specialist to call. The router uses OpenAI function-calling via `ChatOpenAI.bind_tools()`; the model returns a structured `tool_call` (e.g. `agent_id="hr_agent"`, `tool="get_leave_balance"`, `args={}`) — no JSON parsing.
3. Orchestrator emits an SSE event to the SPA: `{type: "routing", agent: "hr_agent"}`. Chat shows: *"Routing to HR Agent…"*
4. Orchestrator `POST <hr_agent>:8001/a2a/message/send` with `Authorization: Bearer <token-A>`, `X-Request-ID: <same>`, body=`{tool, args}`.
5. HR Agent validates token-A: signature via JWKS, `iss` matches IS, `aud` matches `orchestrator-app`, `act.sub` is in `HR_TRUSTED_PEER_AGENTS` allowlist. Extracts `user_sub = token_a.sub`.
6. HR Agent ensures it has its own actor_token via App-Native Auth (cached, re-mint if needed).
7. HR Agent `POST <IS>/oauth2/ciba` authenticated as its Agent App (Basic), body: `scope=openid hr_self_rest, login_hint=<user_sub>, binding_message=<F11.template>, actor_token=<own actor token>, notification_channel=external`.
8. IS responds with `{auth_req_id, auth_url, interval=2, expires_in=300}`.
9. HR Agent returns A2A response immediately with `{type: "consent_required", auth_req_id, auth_url, agent_label: "HR Agent", action: "View your leave balance"}`. Continues polling in background.
10. Orchestrator forwards the consent payload to SPA via SSE: `{type: "ciba_url", agent_label, auth_url, binding_code, expires_in, ...}`.
11. SPA renders the **Consent Widget** in the assistant panel.
12. User clicks **Approve**. Browser opens `auth_url` in a new tab. User sees the IS consent screen with the same binding code; clicks Approve. IS records the consent. The browser tab shows a "you may close this window" page (reused from `_archive/agent.before-v3/obo_flow.py:callback_html`).
13. Meanwhile, HR Agent's polling loop hits `/oauth2/token` → IS returns **token-B**: `{sub=user_sub, aut=APPLICATION_USER, act:{sub:hr_agent-id}, aud=<hr_agent OAuth Client ID>, scope=openid hr_self_rest, exp=now+3600}`.
14. HR Agent records token-B in the orchestrator's session map (via callback or correlation-ID — Stage 4 designs the channel). Then calls hr_server's MCP tool `get_leave_balance` with `Authorization: Bearer <token-B>`, `X-Request-ID: <same>`.
15. HR Server validates token-B: signature, `iss`, `aud == HR_AGENT_OAUTH_CLIENT_ID`, `act.sub == hr_agent-id`, `scope` contains `hr_self_rest`. Returns `{leave_days: 12}`.
16. HR Agent returns A2A final response to orchestrator: `{type: "result", payload: {leave_days: 12}}`.
17. Orchestrator's LLM composes user-facing reply from the tool output: *"You have 12 days of leave."*
18. SSE delivers the reply to the SPA chat view.

## Exception flows

### EX-1 — LLM hallucinates a non-existent specialist or tool (step 2)
1. Orchestrator catches "agent not in registry" or "tool not in agent card" before any A2A call.
2. Returns a structured error to the LLM: `{error: "no_such_tool"}`.
3. LLM recovers: *"I'm sorry, I don't have a way to look that up."*

### EX-2 — Specialist rejects the inbound A2A request (step 5)
- Bad signature / missing `act.sub` / `act.sub` not in allowlist → 401 with structured error.
- Orchestrator surfaces: *"Authorization failed at HR Agent. Please contact admin."*
- Maps to N4, N7.

### EX-3 — IS `/oauth2/ciba` returns an error (step 8)
- `unauthorized_client`: CIBA grant not enabled on the agent app. Operations error, surfaced as: *"HR Agent is misconfigured. Contact admin."*
- `invalid_request` with notification channel hint: External not enabled. Same surfacing.

### EX-4 — User denies CIBA consent (after step 12) — see UC-04
### EX-5 — Browser closed before user clicks (step 12) — see UC-05
### EX-6 — `auth_req_id` expires before consent (300s timeout)
1. HR Agent's polling loop catches `error=expired_token` from `/oauth2/token`.
2. HR Agent returns A2A response: `{type: "error", reason: "consent_window_expired"}`.
3. Orchestrator surfaces: *"You took too long to approve. Please ask again."*
4. Maps to N20.

### EX-7 — MCP server rejects token-B (step 15)
- Wrong `aud`: token theft / config bug. 401 with clear log line.
- Maps to N16, N25, N28.

### EX-8 — Specialist crashes mid-call (step 14 — agent is up but hr_server is down)
- HTTP 503 / connection refused.
- HR Agent returns A2A response: `{type: "error", reason: "backend_unavailable"}`.
- Orchestrator surfaces: *"HR system is unavailable. Try again in a moment."*

## Postconditions
- **Success:** chat shows the answer; token-B is alive in HR Agent for the rest of its TTL (1 hour); orchestrator's session map has a record of `(session_id, hr_agent, jti, exp)`.
- **Failure (most ex-flows):** no answer in chat (or partial), structured error message, no zombie polling loops, all tokens accounted for.

## Design notes for downstream stages

### UX (Stage 3)
- **Routing message:** *"Routing to HR Agent…"* — signal pre-CIBA so user knows what to expect.
- **Consent Widget:** see [`consent-widget-spec.md`](../consent-widget-spec.md). Single widget at a time (serial).
- **Latency states:** Awaiting → Verifying with IS → Working → Done.
- **Error message catalog:** Stage 3 produces a copy deck with one-sentence user-friendly text per EX-NN.

### Architecture (Stage 4)
- Need to design: how does HR Agent communicate token-B back to the orchestrator's session map? Options: (a) callback HTTP request; (b) correlation-ID-keyed in-process map (only works for single-process — fits Q5); (c) include in A2A response and orchestrator persists. **Recommend (c)** — simplest, no extra wires.
- A2A schema: standard JSON-RPC 2.0 with `message/send` method. Body shape: `{tool: str, args: dict}`. Response: `{type: "consent_required" | "result" | "error", ...}`.
- MCP schema: standard MCP tool calls; one tool per business action.

### Testing (Stages 7–8)
- **N4, N7, N16, N21, N25, N28 automated:** unit tests against the JWT validator with hand-crafted fixtures.
- **Manual:** the canonical happy-path demo: ask "What's my leave balance?" → consent → answer.
