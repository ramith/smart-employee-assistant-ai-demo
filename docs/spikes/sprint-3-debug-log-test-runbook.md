# Sprint 3 — Comprehensive debug-log capture runbook

**Purpose:** with WSO2 IS debug logs enabled, run a single test sequence that exercises every revocation/logout primitive in the locked Sprint 3 design. The resulting logs let us verify F-19 / F-20 / F-21 against IS's actual internal behaviour rather than from black-box probe outputs.

**Prerequisite:** WSO2 IS debug logging enabled (operator step). Recommended log levels (per WSO2 IS docs):

```
log4j.logger.org.wso2.carbon.identity.oauth = DEBUG
log4j.logger.org.wso2.carbon.identity.oauth.ciba = DEBUG
log4j.logger.org.wso2.carbon.identity.oauth.endpoint = DEBUG
log4j.logger.org.wso2.carbon.identity.oauth.event = DEBUG
log4j.logger.org.wso2.carbon.identity.oauth2 = DEBUG
log4j.logger.org.wso2.carbon.identity.application.authentication.framework = DEBUG
log4j.logger.org.wso2.carbon.identity.event = DEBUG
```

(Apply via `<IS_HOME>/repository/conf/log4j2.properties`, restart IS, then run this sequence.)

---

## §1. Single test sequence to capture everything

Run as `employee_user` (sub `2048ad8c-16a6-4ec1-bb63-b38300118f28`) on the laptop demo stack.

### Step 0 — bring up rigs

```bash
make demo-up
./scripts/spike-bcl-up.sh    # reverse-SSH tunnel + BCL listener
```

In IS Console, confirm:
- `orchestrator-mcp-client` → Logout URLs → Back-channel logout URL = `http://localhost:8123/bcl`
- HR-AGENT-... → BCL URL = `http://localhost:8123/bcl` (already set per F-19 setup)
- IT-AGENT-... → BCL URL = `http://localhost:8123/bcl` (same)

### Step 1 — fresh session

Sign out of SPA + IS Console + `employee_user` (clear cookies), then sign back in fresh.

Log timestamp marker: **T0**.

### Step 2 — capture token-A and id_token

Add temporary debug prints in `orchestrator/auth/routes.py`:

```python
print(f"C15_PROBE_DEBUG_TOKEN_A {result.token_a.access_token}", flush=True)
print(f"C15_PROBE_DEBUG_ID_TOKEN {result.token_a.id_token}", flush=True)
```

`docker compose up -d --build orchestrator`. Sign in fresh.

```bash
docker compose logs orchestrator | grep C15_PROBE_DEBUG_TOKEN_A | tail -1
docker compose logs orchestrator | grep C15_PROBE_DEBUG_ID_TOKEN | tail -1
```

Save as `TOKEN_A` and `ID_TOKEN` env vars.

### Step 3 — trigger CIBA flow + capture token-B

In SPA: ask *"What's my leave balance?"*. Approve consent.

Add temporary debug print in `hr_agent/ciba/orchestrator.py` near the cache write:

```python
print(f"C15_PROBE_DEBUG_TOKEN_B {token_b.access_token}", flush=True)
```

`docker compose up -d --build hr_agent`. Re-issue the HR query in SPA.

```bash
docker compose logs hr_agent | grep C15_PROBE_DEBUG_TOKEN_B | tail -1
```

Save as `TOKEN_B`.

### Step 4 — F-21 re-confirm: introspect token-B is initially active

```bash
cd idp_capability_test
TOKEN_A=<paste> TOKEN_B=<paste> python3 c13_introspection_capability.py
```

This runs steps C13.1–C13.5: introspect both tokens, revoke token-A, settle 5 s, introspect again. Outcome already captured as F-21 FAIL.

**Log timestamp marker: T1.** Note the IS log lines fired during introspect + revoke.

### Step 5 — F-20 re-confirm: auth_req_id revoke is no-op

In a separate terminal:

```bash
EMPLOYEE_USER_SUB=2048ad8c-16a6-4ec1-bb63-b38300118f28 python3 c14_authreqid_revoke.py
```

Probes 4 revoke shapes + polls. Outcome already captured as F-20 FAIL (soft).

**Log timestamp marker: T2.** Note the IS log lines for the 4 revoke shapes + the polling.

### Step 6 — NEW C15: RP-Initiated Logout with id_token_hint

```bash
ID_TOKEN=<paste from step 2> python3 c15_rp_initiated_logout_bcl.py
```

Probes whether `/oidc/logout?id_token_hint=…&client_id=…` triggers BCL fan-out to session participants (orchestrator-mcp-client + agents).

**Log timestamp marker: T3.** Note IS log lines for /oidc/logout handling, session-participant resolution, and BCL emission.

### Step 7 — admin-terminate via Console

While the user has an active session and (post step 6) potentially still an IS-side session:

IS Console → User Management → `employee_user` → Active Sessions → **Terminate**.

**Log timestamp marker: T4.** Note IS log lines for admin-driven termination + BCL.

### Step 8 — capture all BCL POSTs

```bash
cat tools/_bcl_log/bcl_received.log
```

Save the full file alongside the IS audit + carbon logs.

### Step 9 — revert temporary debug prints

```bash
git diff orchestrator/auth/routes.py hr_agent/ciba/orchestrator.py
# verify ONLY the C15_PROBE_DEBUG_* prints; revert them.
git checkout orchestrator/auth/routes.py hr_agent/ciba/orchestrator.py
```

### Step 10 — bundle logs

```bash
tar czf /tmp/sprint-3-full-test-logs.tgz \
  is_server_logs/wso2is-7.3.0/repository/logs/audit.log \
  is_server_logs/wso2is-7.3.0/repository/logs/wso2carbon.log \
  tools/_bcl_log/bcl_received.log
```

Paste the path or zip contents back to me.

---

## §2. What the analysis will look for

| Marker | Expected log activity (DEBUG level) |
|---|---|
| **T0–T1 setup** | StoreSession (Pattern C login). UpdateSession (CIBA flow). |
| **T1 (C13)** | `/oauth2/introspect` requests + responses for token-A then token-B. `/oauth2/revoke` for token-A. After revoke: any internal "kill grant chain" / "kill child tokens" log lines? |
| **T2 (C14)** | 4 `/oauth2/revoke` requests with `auth_req_id`. CIBA store handler activity. Whether the auth_req_id is marked invalid internally even though it returns 200. |
| **T3 (C15 BCL)** | `/oidc/logout` request handling. **Critical:** does IS walk the session-participants table? Does it look up `backchannel_logout_uri` for each? Does it emit logout_token JWTs? Why does/doesn't it fire to agent apps? |
| **T4 (admin-terminate)** | Same questions for the admin-driven path. |
| **BCL log** | logout_token JWT bodies (decoded by `tools/bcl_listener.py`) — `aud`, `sub`, `sid`, `events` claims. |

**The critical logs are at T3 and T4.** If at T3 we see IS resolving session participants but only emitting BCL to the auth_code SP (orchestrator-mcp-client), that confirms the F-19 partial-stand interpretation. If we see IS emitting BCL to *all* session participants including agent apps, F-19 was a probe artifact and Option C is viable. If we see no resolution attempt at all, F-19 fully stands.

---

## §3. What changes in the Sprint 3 design depending on outcome

| C15 outcome | F-19 status | Tech-arch §5 change | Demo narrative |
|---|---|---|---|
| Full BCL to auth_code + agents | F-19 was probe artifact | Restore introspection backstop for D3.2 path; remove SECURITY-DEGRADED labels for admin-terminate. F-21 still applies for user-driven `/oauth2/revoke`. | "We use orchestrator gateway for sub-second; OIDC BCL is our spec-compliant backstop." |
| BCL to auth_code only | F-19 partial stand | No change for 3A. 3B.1 admin-terminate uses BCL only on orchestrator (already the design). | Unchanged. |
| No BCL fired | F-19 fully stands | No change. SECURITY-DEGRADED labels remain. | Unchanged ("gateway pattern is required"). |

**No design changes happen until the test runs and logs are analysed.** This runbook is the precondition for that analysis.
