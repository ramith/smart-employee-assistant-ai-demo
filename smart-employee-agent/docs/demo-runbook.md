# Demo Runbook — Smart Employee Agent (Sprint 1)

Stage demo: Identity-First AI Governance / UC-03 Two-specialist serial query.
Target duration: 60–90 s on stage.

---

## Pre-demo checklist

- [ ] WSO2 IS reachable: `curl -k https://13.60.190.47:9443/healthcheck` returns 200.
- [ ] All five `.env` files have real credential values (no blank `=` lines for IDs/secrets):
  - `orchestrator/.env` — `ORCHESTRATOR_APP_CLIENT_ID`, `ORCHESTRATOR_AGENT_ID`, etc.
  - `hr_agent/.env` — `HR_AGENT_ID`, `HR_AGENT_OAUTH_CLIENT_ID`, etc.
  - `it_agent/.env` — `IT_AGENT_ID`, `IT_AGENT_OAUTH_CLIENT_ID`, etc.
  - `hr_server/.env` — `CLIENT_ID`, `SPA_CLIENT_ID`, etc.
  - `it_server/.env` — trusted-peer values populated.
- [ ] Ports free on the demo machine: 8000, 8001, 8002, 8004, 8090, 3001.
- [ ] Docker daemon running; `docker compose version` shows Compose v2.
- [ ] `python3 --version` is 3.11 or later; `httpx` installed (`pip install httpx`).
- [ ] Browser signed out of any previous session at `http://localhost:3001`.

---

## Start

```bash
./scripts/demo-up.sh
```

This command builds all images, starts the stack, waits 5 s, and runs the
healthz smoke check. If any service shows `FAIL`, check:

```bash
docker compose logs --tail=50 <service-name>
```

---

## Demo flow (UC-03)

1. Open `http://localhost:3001` in the browser.
2. Click **Sign in**. Log in as `probe.user` / `NewsMax@1234`.
3. In the chat input, type exactly:

   > Show me my leave balance and what laptops are available.

   Hit **Enter**.

4. The chat shows **"Routing to HR Agent..."** and the HR Consent Widget appears.
   - Click **Approve**. The widget cycles: Verifying → Working → Done.
   - Chat renders the leave balance answer (e.g. "You have 12 days of leave.").

5. The chat shows **"Now routing to IT Agent..."** and the IT Consent Widget appears.
   - Click **Approve**. Widget cycles again.
   - Chat renders the asset list.

6. A **final combined reply** appears: both leave balance and laptop list in one sentence.

7. Optional (for audience): switch to WSO2 IS audit log to show the two CIBA
   events attributed to `probe.user`.

8. Click **Sign out** to close the session.

---

## Keyword fallback (F-14)

`LLM_FALLBACK_MODE=keyword` is on by default. Routing rules:

- "leave" / "vacation" / "time off" → `hr_agent.get_leave_balance`
- "laptop" / "asset" / "equipment" → `it_agent.list_available_assets`

The demo query contains both triggers, so routing is deterministic regardless
of Gemini availability. No action required unless you want to demo live LLM
routing (set `LLM_FALLBACK_MODE=llm` in `orchestrator/.env` before starting).

---

## Audit correlation walkthrough (D2.4)

Every service stamps its log lines with the same `X-Request-ID`. After running
the demo flow above, you can pick *any* user message and reconstruct the full
chain across services with one grep.

> **Scope of this walkthrough.** This demonstrates *propagation* of a
> correlation id across A2A and MCP hops. Cryptographic log integrity
> (signed/append-only logs, tamper detection) is **out of scope** for the POC
> — see `docs/architecture/sprint-1-fixes.md` §F-16. The trust anchor for
> "who did what on whose behalf" is the per-agent CIBA token's `act.sub` +
> `sub` pair (validated at every hop), not the rid itself.

### How the id propagates

```
   SPA  ──[chat POST]──▶  orchestrator  ──[A2A]──▶  hr_agent  ──[MCP]──▶  hr_server
                                       ──[A2A]──▶  it_agent  ──[MCP]──▶  it_server
```

- `CorrelationIdMiddleware` (in `common.logging.correlation`) reads or generates
  the header on every inbound request and stores it in a ContextVar.
- `CorrelationIdLogFilter` injects the ContextVar value into every log record,
  so the configured format
  (`%(asctime)s %(levelname)s %(request_id)s %(name)s %(message)s`) renders
  the rid on every line written inside a request scope.
- Outbound HTTP clients propagate the header explicitly:
  - A2A: `common/a2a/client.py` adds `X-Request-ID` on `message/send`,
    `message/await`, and `message/cancel`.
  - MCP: `hr_agent/mcp/client.py` and `it_agent/mcp/client.py` add it on every
    tool call.
- Lines logged outside a request scope (process startup, shutdown, idle
  background coroutines) render `request_id` as `-` — this is expected.

### Find a request id

The orchestrator logs one `chat_request` line per user message:

```bash
docker compose logs --tail=200 orchestrator | grep chat_request
```

Each line includes `request_id=<uuid>`. Copy the uuid for the message you want
to trace.

### Reconstruct the chain

```bash
./scripts/grep-trace.sh <request-id>
```

Example output (abridged) for the UC-03 multi-agent query:

```
orchestrator | 2026-05-08 14:22:31,001 INFO d8f3a2c1-... orchestrator.chat.routes chat_request | session_id=… message_len=51
orchestrator | 2026-05-08 14:22:31,012 INFO d8f3a2c1-... orchestrator.chat.routes chat_fan_out | dispatch agent_id=hr_agent tool=hr.read_balance
hr_agent     | 2026-05-08 14:22:31,089 INFO d8f3a2c1-... common.a2a.server  a2a_message_send received tool=hr.read_balance
hr_agent     | 2026-05-08 14:22:31,094 INFO d8f3a2c1-... hr_agent.ciba.orchestrator ciba_init scope=openid hr_self_rest
hr_agent     | 2026-05-08 14:22:36,402 INFO d8f3a2c1-... hr_agent.mcp.client mcp_call tool=get_leave_balance
hr_server    | 2026-05-08 14:22:36,411 INFO d8f3a2c1-... hr_server.mcp.tools tool_invoked tool=get_leave_balance act_sub=hr-agent
orchestrator | 2026-05-08 14:22:36,498 INFO d8f3a2c1-... orchestrator.chat.routes chat_fan_out | dispatch agent_id=it_agent tool=it.list_available_assets
it_agent     | 2026-05-08 14:22:36,510 INFO d8f3a2c1-... common.a2a.server  a2a_message_send received tool=it.list_available_assets
it_agent     | 2026-05-08 14:22:36,514 INFO d8f3a2c1-... it_agent.ciba.orchestrator  ciba_init scope=openid it_assets_read_rest
it_agent     | 2026-05-08 14:22:41,808 INFO d8f3a2c1-... it_agent.mcp.client mcp_call tool=list_available_assets
it_server    | 2026-05-08 14:22:41,816 INFO d8f3a2c1-... it_server.mcp.tools tool_invoked tool=list_available_assets act_sub=it-agent
orchestrator | 2026-05-08 14:22:41,901 INFO d8f3a2c1-... orchestrator.chat.routes chat_fan_out | done tools=2
```

The same rid appears on every line; sorting by timestamp reproduces the
end-to-end flow including both CIBA legs and both MCP calls.

### Identity-first denial trace (UC-08)

Pick a denied request id (e.g. `please approve LV-004` from `employee_user`).
The same grep produces a chain that ends with the IS-issued downgraded scope
and the resource server's refusal:

```
hr_agent  | … ciba_init scope=openid hr_approve_rest
hr_agent  | … ciba_token_acquired scope_returned="openid hr_self_rest"   # IS dropped hr_approve_rest (Path C, F-18)
hr_agent  | … mcp_call tool=approve_leave
hr_server | … mcp_tool_denied tool=approve_leave error_id=ERR-MCP-003 reason="missing required scope: hr_approve_rest"
hr_agent  | … mcp_call_failed status=401 error_id=ERR-MCP-003
```

The orchestrator then surfaces the user-facing denial copy (`_ERROR_COPY` →
"You don't have permission to perform this action…"). All of this is keyed by
the same `request_id`.

### IS-side touchpoint

WSO2 IS audit logs (`<carbon>/repository/logs/audit.log`) are not stamped with
our `X-Request-ID` because IS does not ingest the header. The correlation
between *our* trace and *IS's* events is therefore by:

- `act.sub` (the agent's OAuth client; the audit row's `Subject`/`Username`),
- `auth_req_id` (logged on our side at CIBA initiation; appears in IS's CIBA
  audit trail), and
- timestamp (sub-second on the same NTP source).

For the demo, point the audience at one IS audit row and the matching
`hr_agent` log line — the `auth_req_id` appears in both.

---

## Tear down

```bash
./scripts/demo-down.sh
```

Add `--volumes` to also remove named Docker volumes.

---

## Known limitations (dev-time only — not relevant for live demo)

- 6 test files require split-phase runs via `./tools/run-tests.sh` due to
  event-loop isolation requirements. This is a dev-time constraint only.
- Full chat-flow smoke automation (UC-03 consent widget sequence) is deferred
  to Sprint 2; `demo-smoke.py` currently validates healthz endpoints only.
- Orchestrator is single-replica only (POC constraint per milestone-plan §5.3).
  Multi-replica requires sticky sessions and Redis-backed state (Sprint 3+).
