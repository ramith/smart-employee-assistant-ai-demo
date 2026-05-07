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
