# Asgardeo Configuration Guide

**Audience:** lead engineer setting up the Asgardeo tenant for this POC.
**Outcome:** a configured tenant + a populated `.env` per service + a passed spike memo (P1–P14, with P6 and P9 deferred per v3 POC review).

This is a **living document** — Sprint 0 establishes the baseline; Sprint 1 and Sprint 2 add configuration as new flows are introduced. If you hit something not covered here, fix the doc as part of your PR.

---

## 0. Prerequisites

- Asgardeo tenant with admin access (Console URL like `https://console.asgardeo.io/t/<your-tenant>`).
- Tenant should have **Agent Identity** feature available (visible as "Agents" in the left nav). If not, contact Asgardeo support.
- Local dev environment with `curl` and `jq`.
- The tenant should be a **fresh / dedicated POC tenant** — don't reuse one with production users, since some configuration changes are tenant-wide.
- Repository checked out at `/Users/ramith/demo/dda-poc/iam-ai-samples/smart-employee-agent/`.

---

## 1. Naming conventions used in this guide

Substitute these into the curl scripts and Console fields. Pick once, use consistently.

| Symbol | Example | Meaning |
|---|---|---|
| `<TENANT>` | `myorg-poc` | Your Asgardeo tenant name |
| `<ASGARDEO_BASE>` | `https://api.asgardeo.io/t/<TENANT>` | Tenant API base URL |
| `<ISSUER>` | `https://api.asgardeo.io/t/<TENANT>/oauth2/token` | Token issuer (matches the `iss` claim) |
| `<JWKS_URL>` | `<ASGARDEO_BASE>/oauth2/jwks` | JWKS endpoint |
| `<INTROSPECT_URL>` | `<ASGARDEO_BASE>/oauth2/introspect` | RFC 7662 introspection endpoint |

Canonical resource URIs (used as `aud` in tokens — must be exact match):

| Resource | URI | Used by |
|---|---|---|
| Orchestrator | `https://orchestrator.smart-employee.local` | First-hop user delegated token |
| HR Agent | `https://hr.smart-employee.local/a2a` | Hop 3a |
| IT Agent | `https://it.smart-employee.local/a2a` | Hop 3b |
| HR Server | `https://hr-server.local/mcp` | Hop 4 (verify exact value with P13) |
| IT Server | `https://it-server.local/mcp` | Hop 5 (verify exact value with P14) |

**Note:** these URIs need not be reachable; they're identifiers. They must be exact-string-matched between the API resource registration in Asgardeo and the validator code.

---

## 2. Step 1 — Application: orchestrator-app

Console → **Applications** → **+ New Application** → **Single-Page Application**

| Field | Value |
|---|---|
| Application name | `orchestrator-app` |
| Authorized redirect URLs | `http://localhost:5001/callback` |
| Allowed origins | `http://localhost:3001` |
| Public client | ✓ (required for PKCE) |

**After creating, click into it →**

### Protocol tab
- ☑ **PKCE** mandatory.
- Grant types:
  - ☑ Authorization Code
  - ☑ **Token Exchange** (urn:ietf:params:oauth:grant-type:token-exchange) — required for Hops 3 / 4 / 5.
  - ☑ Refresh Token
- Access token: **JWT**. Lifetime: 3600 s (default).
- Refresh token lifetime: 86400 s.

### Advanced tab
- ☑ **App-Native Authentication: enabled** (gotcha #A — off by default).
- ☑ **Skip user consent** for *this app's own scopes only* (NOT the `requested_actor` consent — that screen MUST appear).

### Roles tab
- **Role audience: Organization** (gotcha #B — Application by default).

### User Attributes tab
- Mandatory: `username`, `email`, `roles`.

### Logout URLs (Sprint 2 prep)
- **Back-channel logout URL:** `http://localhost:5001/auth/backchannel-logout`
- Confirm `id_token_signed_response_alg`: typically `RS256`.

**Capture for `.env`:**
- Client ID → `ORCHESTRATOR_APP_CLIENT_ID`
- (No secret — public PKCE client.)

---

## 3. Step 2 — Agent identities (3)

Console → **Agents** → **+ Register Agent** (do this three times).

| Display name | Identifier | Purpose |
|---|---|---|
| Orchestrator Agent | `orchestrator-agent` | The acting party in delegated tokens; called via `requested_actor` |
| HR Agent | `hr-agent` | Specialist with own identity; mints Hop-4 tokens for hr-server |
| IT Asset Agent | `it-agent` | Specialist with own identity; mints Hop-5 tokens for it-server |

For each agent:
- Capture **Agent ID** (the `sub` claim it'll appear under).
- Capture **Client ID + Client Secret** (the credentials used in client_credentials grant for actor-token minting).

**Capture for `.env` files:**
- Orchestrator agent → `ORCHESTRATOR_AGENT_ID`, `ORCHESTRATOR_AGENT_CLIENT_ID`, `ORCHESTRATOR_AGENT_CLIENT_SECRET`
- HR agent → `HR_AGENT_CLIENT_ID`, `HR_AGENT_CLIENT_SECRET`
- IT agent → `IT_AGENT_CLIENT_ID`, `IT_AGENT_CLIENT_SECRET`

---

## 4. Step 3 — API Resources (4)

Console → **API Resources** → **+ New API Resource** (do this four times). Set audience exactly as below.

### 4.1 hr-agent-api
- Identifier (audience): `https://hr.smart-employee.local/a2a`
- Scopes: `hr_basic_mcp`, `hr_self_mcp`, `hr_read_mcp`, `hr_approve_mcp`

### 4.2 it-agent-api
- Identifier: `https://it.smart-employee.local/a2a`
- Scopes: `it_assets_read_mcp`, `it_assets_write_mcp` *(reserved; not used in Sprint 1)*

### 4.3 hr-server-api *(may already exist from prior work — verify audience)*
- Identifier: `https://hr-server.local/mcp` (or whatever the existing tenant has registered — **verify with P13**)
- Scopes: same as above (`hr_*_mcp`)

### 4.4 it-server-api
- Identifier: `https://it-server.local/mcp`
- Scopes: `it_assets_read_mcp`

---

## 5. Step 4 — Roles + scope assignments

Console → **Roles** → ensure these exist (create if missing).

### `employee`
Scopes:
- `agent_access` (umbrella — required for Orchestrator)
- `hr_basic_mcp`, `hr_self_mcp`
- `it_assets_read_mcp`

### `hr_admin`
Scopes: everything in `employee`, plus:
- `hr_read_mcp`, `hr_approve_mcp`

### Assign agents to roles (gotcha #D)

In each role's **Agents** tab, assign:
- `employee` ← `orchestrator-agent`, `hr-agent`, `it-agent`
- `hr_admin` ← same

Without this assignment, the agent identity has no permissions (gotcha #D — same end-user symptom as #C).

---

## 6. Step 5 — Connect orchestrator-app to API resources

Console → **Applications** → `orchestrator-app` → **API Resources** tab → Subscribe to:
- `agent-api` (existing — for `agent_access` umbrella scope)
- `hr-agent-api`, `it-agent-api`, `hr-server-api`, `it-server-api`

This authorizes `orchestrator-app` to request these resources via RFC 8693 token-exchange.

### Configure `requested_actor` policy
Console → `orchestrator-app` → **Authorized Actors** (or equivalent — exact field name varies by tenant version):
- Permitted actors: `orchestrator-agent`

This authorizes the SPA to send `requested_actor=orchestrator-agent` on `/authorize`. **Without this, P10/P11 will fail.**

---

## 7. Step 6 — Per-service `.env` files

After completing steps 1–6, populate the following:

### `.env` (root, used by `docker-compose.yml`)
```bash
ASGARDEO_TENANT=<TENANT>
ASGARDEO_BASE=https://api.asgardeo.io/t/<TENANT>
ASGARDEO_ISSUER=https://api.asgardeo.io/t/<TENANT>/oauth2/token
ASGARDEO_JWKS_URL=https://api.asgardeo.io/t/<TENANT>/oauth2/jwks
ASGARDEO_INTROSPECT_URL=https://api.asgardeo.io/t/<TENANT>/oauth2/introspect

ORCHESTRATOR_APP_CLIENT_ID=<from step 1>
ORCHESTRATOR_AGENT_ID=<from step 2>
ORCHESTRATOR_AGENT_CLIENT_ID=<from step 2>
ORCHESTRATOR_AGENT_CLIENT_SECRET=<from step 2>

HR_AGENT_CLIENT_ID=<from step 2>
HR_AGENT_CLIENT_SECRET=<from step 2>
IT_AGENT_CLIENT_ID=<from step 2>
IT_AGENT_CLIENT_SECRET=<from step 2>

CACHE_BUST_HMAC_SECRET=<generate: openssl rand -hex 32>
```

### Per-service `.env` overrides (copied from `_archive/agent.before-v3/.env` for any reused values like `GOOGLE_API_KEY`).

---

## 8. Step 7 — Spike probes (P1–P14, P6/P9 deferred)

Run these from your shell after configuration is complete. Capture each probe's curl + response excerpt into `docs/spikes/asgardeo-capability-memo.md` per the template at the end of this section.

> **Convention:** all probes assume the env vars from Step 7 are exported. Run `source .env` (or use direnv) first.

### P1 — Token-exchange with `actor_token` populates `act` claim

Pre-mint an Orchestrator-Agent access token (used as `actor_token`):

```bash
ORCH_ACTOR_TOKEN=$(curl -sS -X POST "$ASGARDEO_BASE/oauth2/token" \
  -u "$ORCHESTRATOR_AGENT_CLIENT_ID:$ORCHESTRATOR_AGENT_CLIENT_SECRET" \
  -d "grant_type=client_credentials" \
  -d "scope=internal" | jq -r .access_token)
echo "Actor token (first 40 chars): ${ORCH_ACTOR_TOKEN:0:40}..."
```

You also need a real user delegated token. For probe purposes, use a service account or manually go through the SPA login once and save the access token. Set `USER_TOKEN=...`.

```bash
EXCHANGED=$(curl -sS -X POST "$ASGARDEO_BASE/oauth2/token" \
  -u "$ORCHESTRATOR_APP_CLIENT_ID:" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=$USER_TOKEN" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:jwt" \
  -d "actor_token=$ORCH_ACTOR_TOKEN" \
  -d "actor_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "resource=https://hr.smart-employee.local/a2a" \
  -d "scope=hr_read_mcp")

echo "$EXCHANGED" | jq .
echo "$EXCHANGED" | jq -r .access_token | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```

**Pass:** decoded JWT payload contains `act.sub == <ORCHESTRATOR_AGENT_ID>`.
**Fail:** `act` absent or only `sub` populated. Activate fallback in plan §3.6 (custom claim mapping).

### P2 — Resource parameter narrows `aud` exactly

Same call as P1; check decoded payload:

```bash
echo "$EXCHANGED" | jq -r .access_token | cut -d. -f2 | base64 -d 2>/dev/null | jq .aud
```

**Pass:** `aud == "https://hr.smart-employee.local/a2a"` exactly (string, not array — or array with that exact entry).
**Fail:** `aud` is the orchestrator-app's default audience. Activate fallback (custom-claim policy).

### P3 — Refresh-token revocation cascade

Get a refresh token from the user login (capture from SPA login response). Revoke it:

```bash
curl -sS -X POST "$ASGARDEO_BASE/oauth2/revoke" \
  -u "$ORCHESTRATOR_APP_CLIENT_ID:" \
  -d "token=$USER_REFRESH_TOKEN" \
  -d "token_type_hint=refresh_token" -i
```

Then check whether the **exchanged** access token from P1 is still valid:

```bash
EX_TOKEN=$(echo "$EXCHANGED" | jq -r .access_token)
curl -sS -X POST "$ASGARDEO_INTROSPECT_URL" \
  -u "$ORCHESTRATOR_APP_CLIENT_ID:" \
  -d "token=$EX_TOKEN" | jq .active
```

**Pass:** `false` — exchange-derived token invalidated when parent revoked.
**Fail:** `true` — no cascade. Sprint 2 relies on Layer A introspection only; document.

### P4 — Back-channel logout delivers a logout token (Sprint 2)

This requires a registered `backchannel_logout_uri`. Spin up a tiny listener:

```bash
# Terminal 1: listener
python3 -m http.server 9999 --bind 127.0.0.1
# OR for actual handling:
ngrok http 5001  # if behind NAT
```

Then trigger logout (e.g., from SPA, or admin-terminate the session in Console).
**Pass:** an HTTP POST hits `/auth/backchannel-logout` within ~30 s with a `logout_token` form field.
**Fail:** no POST received. Sprint 2 falls back to Layer A only.

Capture the logout token; decode it:
```bash
echo "<logout_token_received>" | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```
Verify presence of `events`, `iss`, `aud`, and `sid` (P7 dependency).

### P5 — Introspect reflects revocation within 5 s

```bash
# Capture timestamp before revoke
START=$(date +%s%N)
curl -sS -X POST "$ASGARDEO_BASE/oauth2/revoke" \
  -u "$ORCHESTRATOR_APP_CLIENT_ID:" \
  -d "token=$USER_TOKEN" \
  -d "token_type_hint=access_token"

# Poll introspect until active=false
while true; do
  ACTIVE=$(curl -sS -X POST "$ASGARDEO_INTROSPECT_URL" \
    -u "$ORCHESTRATOR_APP_CLIENT_ID:" \
    -d "token=$USER_TOKEN" | jq -r .active)
  NOW=$(date +%s%N)
  ELAPSED_MS=$(( (NOW - START) / 1000000 ))
  echo "elapsed=${ELAPSED_MS}ms active=$ACTIVE"
  if [ "$ACTIVE" = "false" ]; then break; fi
  if [ "$ELAPSED_MS" -gt 5000 ]; then echo "FAIL: > 5s"; exit 1; fi
  sleep 0.2
done
```

**Pass:** `active=false` within 5000 ms.
**Fail:** Layer A's 2-s cache + Asgardeo introspection latency exceeds budget. Lengthen the cache or accept a wider window in `docs/user-experience.md` §6.2.

### ~~P6~~ — DEFERRED (BCL retry policy)
Layer A is the safety net; BCL retry behavior doesn't change the demo's claim. Re-run in production hardening.

### P7 — `sid` in access tokens

Decode any access token (P1's exchanged one is fine):
```bash
echo "$EX_TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```

**Pass:** `sid` claim present.
**Fail:** `sid` only in ID tokens. Sprint 2 stores `sid` from ID token at login (already planned).

### P8 — Nested `act` chain across two exchanges

Take P1's exchanged token; use it as `subject_token` in another exchange (e.g., HR Agent → hr-server, Hop 4):

```bash
HR_ACTOR_TOKEN=$(curl -sS -X POST "$ASGARDEO_BASE/oauth2/token" \
  -u "$HR_AGENT_CLIENT_ID:$HR_AGENT_CLIENT_SECRET" \
  -d "grant_type=client_credentials" -d "scope=internal" | jq -r .access_token)

HR_SERVER_TOKEN=$(curl -sS -X POST "$ASGARDEO_BASE/oauth2/token" \
  -u "$ORCHESTRATOR_APP_CLIENT_ID:" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=$EX_TOKEN" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "actor_token=$HR_ACTOR_TOKEN" \
  -d "actor_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "resource=https://hr-server.local/mcp" \
  -d "scope=hr_read_mcp")

echo "$HR_SERVER_TOKEN" | jq -r .access_token | cut -d. -f2 | base64 -d 2>/dev/null | jq '.act'
```

**Pass:** `{"sub": "hr-agent", "act": {"sub": "orchestrator-agent"}}` — chain depth 2 preserved.
**Fail:** flattened to `{"sub": "hr-agent"}` only. Drop the stretch HR→IT specialist-to-specialist demo; lock validators to depth-1.

### ~~P9~~ — DEFERRED (jti stability)
Implementation falls back to `hash(token)` as cache key unconditionally.

### P10 — `requested_actor` populates `act.sub` in user delegated token

Walk through SPA login at `http://localhost:3001`. The login URL must include `&requested_actor=<ORCHESTRATOR_AGENT_ID>`. After login, decode the resulting access token:

```bash
# Open browser dev tools → Application → Storage → look for the bearer
# OR if you have it in env:
echo "$USER_TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```

**Pass:** `act.sub == <ORCHESTRATOR_AGENT_ID>` in the access token returned to the orchestrator.
**Fail:** `act` absent in access token (may be only in ID token, or not at all). Activate P10 fallback in §3.6: use RFC 8693 for the first hop too.

### P11 — Consent screen rendered for `requested_actor`

During the P10 walkthrough, observe whether Asgardeo renders a consent prompt naming the orchestrator agent.

**Pass:** screen visible, with **Approve** and **Deny** buttons. Click Deny once and verify the deny-flow surfaces an error in the SPA (UX scenario A).
**Fail:** no consent screen. Configure in-orchestrator consent (weaker; document the deviation).

### P12 — Composing `requested_actor` token with subsequent RFC 8693 exchange

P10 produces a user delegated token with `act.sub=orchestrator-agent`. Now use it as `subject_token` in token-exchange (Hop 3a):

```bash
# Same call shape as P1, with subject_token=$USER_DELEGATED_TOKEN (from P10)
echo "$EXCHANGED" | jq -r .access_token | cut -d. -f2 | base64 -d 2>/dev/null | jq '.act'
```

**Pass:** `{"sub": "orchestrator-agent", "act": {"sub": "orchestrator-agent"}}` (preserves the existing `act` from `requested_actor`). Chain composes.
**Acceptable but limited:** `{"sub": "orchestrator-agent"}` only — Asgardeo flattens. Document; locks the validator to depth-1; HR→IT chain (depth 3) becomes infeasible.
**Fail:** `act` absent entirely. Major fallback — escalate to Asgardeo support.

### P13 — `hr-server` `EXPECTED_AUD`

Verify what `aud` the existing `hr-server/auth/jwt_validator.py` checks against:

```bash
grep -nE "aud|audience|EXPECTED" hr-server/auth/jwt_validator.py
grep -nE "AUD|audience" hr-server/.env hr-server/config.py 2>/dev/null
```

**Pass:** record the exact value (e.g., `https://hr-server.local/mcp` or the MCP Client app's client_id). Use that as `resource` in Hop 4 token-exchange.
**Fail:** value cannot be determined → reconfigure hr-server to a known canonical URI before Hop 4 testing.

### P14 — `it-server` `EXPECTED_AUD`

Symmetric to P13, but `it-server` is greenfield in v3 — choose the value at API resource registration (Step 4.4 above) and configure the new `it-server/auth/jwt_validator.py` to expect exactly that.

**Pass:** registered resource URI === validator's expected value === Hop 5 token-exchange `resource` parameter, all three identical strings.

---

## 9. Spike memo template (`docs/spikes/asgardeo-capability-memo.md`)

```markdown
# Asgardeo Capability Memo

**Tenant:** <TENANT>
**Date:** YYYY-MM-DD
**Engineer:** <name>

## Probe results

### P1 — actor_token populates act
- Curl: <pasted command>
- Response excerpt:
  ```json
  { "access_token": "...", ... }
  ```
- Decoded `act` claim:
  ```json
  { "sub": "orchestrator-agent" }
  ```
- Verdict: PASS / FAIL
- Notes: <any deviations>

[... repeat for P2 through P14, omitting P6 and P9 ...]

## Decisions

- Fallback paths activated: <none, or list>
- Stretch HR→IT chained demo: in scope / out of scope (based on P8/P12)

## Sign-off

- Lead engineer: ____________  date: ____________
- Security engineer (review): ____________  date: ____________
```

---

## 10. Troubleshooting

### "ABA-60007" on agent startup
App-Native Auth is disabled. Step 2 → Advanced tab → enable.

### Dashboard shows no data after login
User token has only `openid profile`. Step 1 → Roles tab → Role Audience = **Organization** (gotcha #B).

### Agent calls return `insufficient_scope` for everything
Either (gotcha #C) the MCP Client app's Role Audience is Application, or (gotcha #D) the agent isn't assigned to the user's role. Check both.

### Consent screen for `requested_actor` not shown
P11 fail. Either Asgardeo doesn't render it for this tenant version, or the actor isn't permitted on the application. Step 6 → Authorized Actors.

### Token-exchange returns `invalid_request: actor_token`
Either (a) the actor_token isn't a token (you passed the client_secret directly — pre-mint via client_credentials first), or (b) Asgardeo doesn't recognize the issuer of the actor_token (rare; if it's the same tenant, this should always work).

### Token-exchange returns no `act` claim (P1 fail)
Activate plan §3.6 P1 fallback: Asgardeo claim-mapping policy adds `x_actor_client_id` custom claim mapped from the actor_token's `sub`.

### Logout token doesn't arrive at `/auth/backchannel-logout` (P4 fail)
Possible causes: orchestrator not reachable from Asgardeo (NAT — use ngrok), URL not registered, app's logout_uri field empty. Test with admin-terminate first (more reliable than user logout).

### Stale token still works after logout (R1 fail)
P5 latency exceeds 5 s, OR introspection isn't being called (cache TTL too long, or feature flag off). Check `HR_INTROSPECT_ENABLED=true` and `IT_INTROSPECT_ENABLED=true`.

---

## 11. References

- Plan: [milestone-plan.md](milestone-plan.md) — esp. §2.1 (probes), §3.3 (token flow), §3.6 (fallbacks).
- Asgardeo Token Exchange: https://wso2.com/asgardeo/docs/guides/authentication/configure-token-exchange/
- Asgardeo Back-Channel Logout: https://wso2.com/asgardeo/docs/guides/authentication/oidc/add-back-channel-logout/
- Asgardeo Agent Quickstart: https://wso2.com/asgardeo/docs/quick-starts/agent-auth-py/
- Existing README troubleshooting (gotchas A–D): repository root `README.md`.
