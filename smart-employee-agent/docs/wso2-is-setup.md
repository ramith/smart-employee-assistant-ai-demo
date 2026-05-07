# WSO2 Identity Server — Sprint 1 Setup Guide

**Audience:** lead engineer configuring WSO2 IS 7.2.0 to host the Sprint 1 demo of the smart-employee-agent POC.
**Outcome:** a configured IdP + 5 populated `.env` files + a runnable `make demo-up`.
**Time budget:** ~45 minutes the first time through.

> **History.** This doc replaces a 794-line v3-era guide that mixed Asgardeo references with WSO2 IS instructions. The old doc is archived at `wso2-is-setup-v3-asgardeo-archived.md`. The architecture pivoted from RFC 8693 chained delegation to per-agent CIBA on 2026-05-07; see [`spikes/wso2-is-capability-memo.md`](spikes/wso2-is-capability-memo.md) for the empirical reasons. This doc is **WSO2-IS-7.2-only and CIBA-only**.

---

## 0. Prerequisites

- **WSO2 Identity Server 7.2.0** running at `https://13.60.190.47:9443/` (default admin: `admin/admin` — change immediately).
- **Console URL:** `https://13.60.190.47:9443/console`. First-time login may force a password change.
- **Default organization:** `carbon.super` — no tenant path segment in URLs.
- **TLS:** WSO2 IS ships a self-signed cert. Browser will warn; accept the exception. Services use `INSECURE_TLS=1` in `.env` files.
- **Capability spike completed.** The substrate from M0 (`probe.user`, `probe-client-a/b`, `probe-agent-a/b`, `urn:probe:api`, `mcp://probe-hr-server.local`) remains in the tenant; the Sprint 1 entities below are ADDITIONAL.

---

## 1. Sprint 1 entity inventory

The 7 entities you'll create or already have:

| # | Entity | Type | Sprint 1 role | Reusable from spike? |
|---|---|---|---|---|
| 1 | `orchestrator-app` | **Single-Page Application** (public, PKCE) | SPA front-door for Pattern C login | NEW |
| 2 | `orchestrator-mcp-client` | **Standard-Based Application** (confidential, OIDC) | Backend code-exchange authenticator | NEW (or rename `probe-client-a`) |
| 3 | `orchestrator-agent` | **Agent** (Interactive, "Allow users to log in" ON) | Named in `requested_actor=` at SPA login; mints actor_token | NEW (or rename `probe-agent-a`) |
| 4 | `hr-agent` | **Agent** (Interactive, "Allow users to log in" ON) | Initiates CIBA on `/oauth2/ciba` when invoked over A2A | NEW (or rename `probe-agent-b`) |
| 5 | `it-agent` | **Agent** (Interactive, "Allow users to log in" ON) | Same role as hr-agent | NEW |
| 6 | `HR API` | **API Resource** with scopes `hr.read`, `hr.write` | Subscribed by hr-agent's auto-created Agent App | NEW |
| 7 | `IT API` | **API Resource** with scope `it.read` | Subscribed by it-agent's auto-created Agent App | NEW |

> **About MCP Servers (per F-17 in `architecture/sprint-1-fixes.md`):** WSO2 IS 7.2 has a dedicated "MCP Server" registration entity, but per the c10 probe its `aud` binding does **not** apply on the CIBA grant path — `aud` always collapses to the calling agent's OAuth Client ID. We therefore do **not** register hr-server / it-server as MCP Server entities for Sprint 1. They are pure HTTP resource servers; their token validators check `aud == <agent's OAuth Client ID>`. If you want to register MCP Servers anyway for RFC 9728 protected-resource-metadata discovery, that's fine but not required.

### Per-agent four-value rule

Every Agent registration auto-creates a backing **OAuth Application**. That gives **4 distinct values per agent**:
- **Agent ID** (UUID) — used as `username` on `/oauth2/authn`; also appears as `act.sub` in CIBA-issued tokens.
- **Agent Secret** (one-shot) — used as `password` on `/oauth2/authn`. Can be regenerated.
- **OAuth Client ID** of the auto-created OAuth App — used as `client_id` on `/oauth2/{authorize,authn,token,ciba}`.
- **OAuth Client Secret** of the auto-created OAuth App — Basic-auth password on those endpoints.

The Agent ID/Secret pair is for Agent Native Auth. The OAuth Client ID/Secret pair is for OAuth/CIBA. They are NOT interchangeable.

---

## 2. Step 1 — Create `orchestrator-app` (Single-Page Application)

Console → **Applications** → **+ New Application** → **Single-Page Application**

| Field | Value |
|---|---|
| Application name | `orchestrator-app` |
| Authorized redirect URLs | `http://localhost:8090/auth/callback` |
| Allowed origins | `http://localhost:3001` |
| Public client | ✓ (PKCE) |

After creating, click in:

**Protocol tab:**
- ☑ **Code (Authorization Code)**
- ☑ **PKCE Mandatory**
- ☑ **Refresh Token**
- Access token type: **JWT**, lifetime 3600s

**Roles tab:** Role audience = **Organization**.

**Capture for `orchestrator/.env`:**
- Client ID → `ORCHESTRATOR_APP_CLIENT_ID`
- (No client_secret — public PKCE client)

---

## 3. Step 2 — Create `orchestrator-mcp-client` (Standard-Based, confidential)

Console → **Applications** → **+ New Application** → **Standard-Based Application** → **OIDC**

| Field | Value |
|---|---|
| Application name | `orchestrator-mcp-client` |
| Authorized redirect URLs | `http://localhost:8090/auth/callback` |
| Public client | OFF |

**Protocol tab:**
- ☑ **Code (Authorization Code)** — used for the actor_token-attached code-exchange
- ☑ **Client Credentials**
- Client Authentication method: **Client Secret Basic**

**Capture for `orchestrator/.env`:**
- Client ID → `ORCHESTRATOR_MCP_CLIENT_ID`
- Client Secret → `ORCHESTRATOR_MCP_CLIENT_SECRET`

---

## 4. Step 3 — Create the 3 Agent identities

Console → **Agents** → **+ New Agent** for each of:

| Display name | Description |
|---|---|
| `orchestrator-agent` | Acts on behalf of the user across all flows. Named in `requested_actor=`. |
| `hr-agent` | Specialist for HR queries. Initiates CIBA when invoked. |
| `it-agent` | Specialist for IT asset queries. |

For **each** agent, in the registration wizard:

| Field | Value |
|---|---|
| Description | (free text) |
| Allow users to log in with this agent | ☑ **ON** (creates backing OAuth App + exposes `requested_actor` for it) |
| AI Agent Type | **Interactive Agent** |
| Callback URL | `http://localhost:9999/agent-callback` (placeholder; Agents use `/oauth2/authn` not redirect) |

After **Create**, IS shows the **Agent ID (UUID)** and **Agent Secret** (one-shot — copy immediately).

Then go to Console → **Applications** → find the auto-created app named after the agent (something like `AGENT-<uuid-prefix>`). Open it:

**Protocol tab — verify these grants are checked (they should be by default; Token Exchange is NOT needed for v4):**
- ☑ Code
- ☑ Refresh Token
- ☑ **CIBA** ← critical; not always on by default. If absent, tick it.
- ☐ Token Exchange — leave OFF (not used in v4)

**Below the grants, in the CIBA section:**
- **CIBA Authentication Request Expiry Time:** `300`
- **Allowed Notification Delivery Methods:** ☑ **External (Client Application Handles Delivery)** ← MANDATORY. Without this, CIBA does not return `auth_url`.
- ☐ Email, ☐ SMS — leave off

**Advanced tab:**
- ☑ **App-Native Authentication** (needed for `/oauth2/authn` 3-step flow)

Capture from the Agent's General/Credentials tab AND the auto-created OAuth App's Info/Protocol tab:

| Agent | Env file | Vars |
|---|---|---|
| `orchestrator-agent` | `orchestrator/.env` | `ORCHESTRATOR_AGENT_ID` (Agent ID), `ORCHESTRATOR_AGENT_SECRET`, `ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID`, `ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET` |
| `hr-agent` | `hr_agent/.env` | `HR_AGENT_ID`, `HR_AGENT_SECRET`, `HR_AGENT_OAUTH_CLIENT_ID`, `HR_AGENT_OAUTH_CLIENT_SECRET` |
| `it-agent` | `it_agent/.env` | `IT_AGENT_ID`, `IT_AGENT_SECRET`, `IT_AGENT_OAUTH_CLIENT_ID`, `IT_AGENT_OAUTH_CLIENT_SECRET` |

ALSO: copy `HR_AGENT_OAUTH_CLIENT_ID` into `hr_server/.env` as `HR_SERVER_EXPECTED_AUD`. Same for IT side. (The MCP server's expected audience is the paired agent's OAuth Client ID — see F-17.)

---

## 5. Step 4 — Create the 2 API Resources

Console → **API Resources** → **+ New API Resource** (regular, NOT MCP Server — see F-17 note above).

### 5.1 HR API
- **Identifier:** `urn:hr:api`
- **Display name:** HR API
- **Scopes:**
  - `hr.read` — "View HR information"
  - `hr.write` — "Modify HR information"

### 5.2 IT API
- **Identifier:** `urn:it:api`
- **Display name:** IT API
- **Scopes:**
  - `it.read` — "View IT asset information"

---

## 6. Step 5 — Subscribe agent OAuth Apps to API Resources

For each agent's **auto-created OAuth Application**:

| Subscribe app | To resource | Authorized scopes |
|---|---|---|
| `hr-agent`'s OAuth App | HR API | `hr.read`, `hr.write` |
| `it-agent`'s OAuth App | IT API | `it.read` |

Console → Applications → that app → **API Authorization** tab → **+ Authorize resource** → pick the API → check the scopes → **Add**.

`orchestrator-agent`'s OAuth App does NOT need any API subscriptions (it doesn't call MCP backends).

---

## 7. Step 6 — Test users

Console → **Users** → **+ Add User**

For the demo, one user is sufficient (you can add more for multi-role testing later):

| Username | Password | Role |
|---|---|---|
| `probe.user` (already exists from M0 spike — reuse) | `NewsMax@1234` | (none required for Sprint 1 happy path) |

If you want to test the `hr.write` / `hr.approve_leave` path, create an additional user with an `hr_admin` role and ensure the HR API's `hr.write` scope is conditioned on that role. Roles wiring is a Sprint 2 polish item.

---

## 8. Step 7 — Populate the 5 service `.env` files

The repo has `.env` templates at each service path. Fill in the captured values:

### `orchestrator/.env`
```bash
WSO2_IS_BASE_URL=https://13.60.190.47:9443
IDP_INSECURE_TLS=1

# orchestrator-app (Step 1)
ORCHESTRATOR_APP_CLIENT_ID=<from Step 1>

# orchestrator-mcp-client (Step 2)
ORCHESTRATOR_MCP_CLIENT_ID=<from Step 2>
ORCHESTRATOR_MCP_CLIENT_SECRET=<from Step 2>
ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI=http://localhost:8090/auth/callback

# orchestrator-agent (Step 3 — 4 values)
ORCHESTRATOR_AGENT_ID=<UUID>
ORCHESTRATOR_AGENT_SECRET=<one-shot from creation>
ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID=<auto-created app's client_id>
ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET=<auto-created app's client_secret>

# Specialist endpoints (in-cluster names from docker-compose)
HR_AGENT_URL=http://hr-agent:8001
IT_AGENT_URL=http://it-agent:8002

# F-15 collision-detection inputs (cross-checked at startup)
HR_AGENT_OAUTH_CLIENT_ID=<from Step 3 — hr-agent's OAuth App>
IT_AGENT_OAUTH_CLIENT_ID=<from Step 3 — it-agent's OAuth App>

# F-14 default
LLM_FALLBACK_MODE=keyword
GEMINI_API_KEY=<optional; keyword fallback works without>

ORCHESTRATOR_HOST=0.0.0.0
ORCHESTRATOR_PORT=8090
ALLOWED_ORIGINS=http://localhost:3001
COOKIE_SECURE=false
```

### `hr_agent/.env`
```bash
WSO2_IS_BASE_URL=https://13.60.190.47:9443
IDP_INSECURE_TLS=1

# 4 values for hr-agent (Step 3)
HR_AGENT_ID=<UUID>
HR_AGENT_SECRET=<one-shot>
HR_AGENT_OAUTH_CLIENT_ID=<auto-created OAuth App>
HR_AGENT_OAUTH_CLIENT_SECRET=<auto-created OAuth App>

# Trusted upstream agent (orchestrator-agent's UUID from Step 3)
HR_TRUSTED_PEER_AGENTS=<orchestrator-agent's Agent ID UUID>

# CIBA scopes (Step 4 — what HR API authorizes)
HR_CIBA_SCOPE=openid hr.read

# MCP backend
HR_MCP_SERVER_URL=http://hr-server:8000

HR_AGENT_HOST=0.0.0.0
HR_AGENT_PORT=8001
```

### `it_agent/.env`
Identical shape with `IT_*` prefixes; `IT_CIBA_SCOPE=openid it.read`; `IT_MCP_SERVER_URL=http://it-server:8004`.

### `hr_server/.env`
```bash
WSO2_IS_BASE_URL=https://13.60.190.47:9443
IDP_INSECURE_TLS=1

# Per F-17: aud == hr-agent's OAuth Client ID (NOT mcp://...)
HR_SERVER_EXPECTED_AUD=<hr-agent's OAuth Client ID from Step 3>
HR_SERVER_TRUSTED_PEER_AGENTS=<hr-agent's Agent ID UUID from Step 3>

HR_SERVER_HOST=0.0.0.0
HR_SERVER_PORT=8000
```

### `it_server/.env`
Identical shape with `IT_*` prefixes pointing at it-agent's values.

---

## 9. Step 8 — Run the demo

```bash
make demo-up           # docker compose up -d --build + healthz smoke
make demo-smoke        # repeat the healthz checks anytime
open http://localhost:3001
```

Sign in as `probe.user` / `NewsMax@1234`. The orchestrator will redirect you to IS, you'll see a consent screen for `orchestrator-agent`, click Approve, get back to the chat panel.

Then type:
> Show me my leave balance and what laptops are available.

This triggers the canonical UC-03 storyboard (see [`use-cases/UC-03-two-specialist-serial-query.md`](use-cases/UC-03-two-specialist-serial-query.md)): orchestrator routes to HR-Agent first, you Approve a CIBA consent widget, get the leave balance; then it routes to IT-Agent, second Approve, get assets.

`make demo-down` to tear down.

---

## 10. Troubleshooting

### `"unauthorized_client"` from `/oauth2/ciba`
CIBA grant not enabled on the agent's OAuth App's Protocol tab. Re-check Step 4.

### Consent widget appears but never resolves
Notification Channel not set to External. Re-check Step 4.

### `"Impersonator is not found in subject token"` from any /token call
You're trying RFC 8693 token-exchange — that's NOT used in v4. The orchestrator should be calling `/oauth2/token` with `grant_type=authorization_code` (Pattern C) or polling with `grant_type=urn:openid:params:grant-type:ciba`. Check the request shape in logs.

### `aud` mismatch at MCP server
Check that `HR_SERVER_EXPECTED_AUD` equals the hr-agent's OAuth Client ID (NOT a `mcp://...` URI — see F-17).

### F-15 startup collision warning at orchestrator boot
`HR_AGENT_OAUTH_CLIENT_ID` and `IT_AGENT_OAUTH_CLIENT_ID` happen to be the same string. They must be distinct (each agent has its own auto-created OAuth App). Re-capture from Console.

### N28: token from wrong agent reaches wrong MCP server
`*_SERVER_EXPECTED_AUD` is misconfigured. Each MCP server only accepts tokens whose `aud` is its paired agent's OAuth Client ID. The startup INFO log shows the loaded value.

---

## 11. References

- [`architecture/sprint-1.md`](architecture/sprint-1.md) — overview of the Sprint 1 architecture
- [`architecture/sprint-1-fixes.md`](architecture/sprint-1-fixes.md) — F-01..F-17 binding fixes (especially F-17 for the MCP Server discussion)
- [`spikes/wso2-is-capability-memo.md`](spikes/wso2-is-capability-memo.md) — F-1..F-7 from the M0 capability spike (Pattern C, App-Native Auth, CIBA)
- [`use-cases/UC-01-user-login.md`](use-cases/UC-01-user-login.md) — Pattern C login flow detail
- [`use-cases/UC-03-two-specialist-serial-query.md`](use-cases/UC-03-two-specialist-serial-query.md) — the demo storyboard
- [`configuring-ciba-grant-type.md`](configuring-ciba-grant-type.md) — WSO2's own CIBA reference (pulled into this repo for offline access)
- [`idp_capability_test/README.md`](../idp_capability_test/README.md) — runnable probes for empirical capability validation (c0/c1/c4/c8/c10)
