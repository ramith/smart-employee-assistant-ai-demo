# WSO2 Identity Server Configuration Guide

**Audience:** lead engineer setting up the WSO2 IS 7.2.0 on-prem server for this POC.
**Outcome:** a configured server + a populated `.env` per service + a passed spike memo (P0, P1–P5, P7, P8, P10.A/B, P11, P12a, P12b, P13–P19; P6 and P9 deferred per v3 POC review).

This is a **living document** — Sprint 0 establishes the baseline; Sprint 1 and Sprint 2 add configuration as new flows are introduced. If you hit something not covered here, fix the doc as part of your PR.

> **History:** this guide originally targeted Asgardeo SaaS (`ddademo` tenant). After validating P0 + P10.B but hitting a hard block on P1 (RFC 8693 token-exchange grant unavailable on SaaS), the POC migrated to WSO2 IS on-prem. See `memory/project_idp_migration_to_wso2_is.md` for the rationale.

---

## 0. Prerequisites

- **WSO2 Identity Server 7.2.0** running locally at `https://13.60.190.47:9443/` (the `iss` claim WSO2's "Multi-Agent Authorization" slide demonstrates against). Default admin: `admin/admin`.
- **Console URL:** `https://13.60.190.47:9443/console`. First-time login may prompt a password reset.
- **Default organization:** `carbon.super` — there is no tenant path segment in URLs (unlike Asgardeo SaaS's `/t/<tenant>/`). When the docs reference `<ORG>`, it's just `carbon.super` for the default install.
- **TLS:** WSO2 IS ships with a self-signed cert at `13.60.190.47:9443`. All curl probes use `-k` via `$CURL_OPTS` (controlled by `IDP_INSECURE_TLS=1` in `_env.sh`). Browsers will require an exception on first visit.
- **Agent Identity feature:** WSO2 IS 7.1+ ships agentic-AI primitives (Agents, MCP Server resource type, Pattern C). 7.2.0 is current; if you don't see "Agents" in the left nav, verify the version.
- Local dev environment with `curl` and `jq`.
- Repository checked out at `/Users/ramith/demo/dda-poc/iam-ai-samples/smart-employee-agent/`.

### What's different from the old Asgardeo guide

| Concern | Asgardeo SaaS (old) | WSO2 IS on-prem (now) |
|---|---|---|
| Base URL | `https://api.asgardeo.io/t/<tenant>` | `https://13.60.190.47:9443` |
| Console | `https://console.asgardeo.io/t/<tenant>` | `https://13.60.190.47:9443/console` |
| Tenant in URL | yes (`/t/<tenant>/`) | no (default `carbon.super`) |
| TLS | trusted CA | self-signed (`-k` for curl) |
| Token Exchange grant | hidden behind Trusted Token Issuer gating | **directly enableable** on confidential apps' Protocol tab |
| Trusted Token Issuer | "Connections" menu | "Identity Providers" menu (only needed for cross-IDP) |
| App templates | SPA / MCP Client App / Agent App | Same names; same shapes |
| Carries over unchanged | Two-app pattern (SPA + MCP Client App), four-value-per-agent rule, `_a2a`/`_mcp` scope tier split, all probe script logic |

---

## 0.5 Architecture orientation — who plays which OAuth role

Before clicking through the Console, internalize this. Confusing the SPA app with the agent identity is the #1 source of "why doesn't this work" in this setup.

| WSO2 IS entity | What it is | Who uses it | Has client_secret? | Token Exchange grant? |
|---|---|---|---|---|
| **`orchestrator-app`** | SPA application (public PKCE client) | The browser SPA at `client/` (port 3001). Handles user login. | No (PKCE) | **No** — SPA template doesn't expose it. |
| **`orchestrator-agent`** | Agent identity | The orchestrator backend (port 8090). Used as `actor` in token-exchange and as caller in client_credentials. | **Yes** | **Yes** — enabled here. |
| **`hr_agent`** | Agent identity | hr_agent backend (port 8001). Used as actor in Hop 4 re-mint. | Yes | Yes |
| **`it_agent`** | Agent identity | it_agent backend (port 8002). Used as actor in Hop 5 re-mint. | Yes | Yes |
| **`HR Agent API`** | API Resource | Audience advertised by hr_agent (`https://hr.smart-employee.local/a2a`). | n/a | n/a |
| **`IT Asset Agent API`** | API Resource | Audience for it_agent (`https://it.smart-employee.local/a2a`). | n/a | n/a |
| **`HR Server`** | **MCP Server** | Audience for hr_server (`mcp://hr_server.local`). Registered as an MCP Server, not a generic API Resource — WSO2 IS distinguishes the two. | n/a | n/a |
| **`IT Server`** | **MCP Server** | Audience for it_server (`mcp://it_server.local`). Same. | n/a | n/a |

**One-line summary:** the SPA app is the user-login front door (PKCE only). The three agent identities (`orchestrator-agent`, `hr_agent`, `it_agent`) are the confidential clients that do real OAuth work. **Agent-tier audiences are regular API Resources; backend-tier audiences are MCP Servers (WSO2 IS's first-class registration type).** This split mirrors the architecture: A2A-speaking specialists vs MCP-speaking backends.

Login flow at runtime (Hop 1):
```
Browser → /authorize (client_id=orchestrator-app, requested_actor=orchestrator-agent)
        → consent screen
        → redirect to SPA http://localhost:3001/callback?code=...
SPA     → /token (PKCE verifier; no secret)
        → user_delegated_token  {sub:user, act.sub:orchestrator-agent}
SPA     → orchestrator backend (8090) /api/* with Bearer <token>
```

All subsequent token-exchange (Hops 2/3a/3b/4/5) is done by the orchestrator backend and the specialist backends, using their respective **agent identity** credentials, never the SPA.

---

## 1. Naming conventions used in this guide

Substitute these into the curl scripts and Console fields. Pick once, use consistently.

| Symbol | Value | Meaning |
|---|---|---|
| `<ORG>` | `carbon.super` | Default WSO2 IS organization (no URL path segment) |
| `<WSO2_IS_BASE>` | `https://13.60.190.47:9443` | IdP base URL |
| `<ISSUER>` | `https://13.60.190.47:9443/oauth2/token` | Token issuer (matches the `iss` claim) |
| `<JWKS_URL>` | `<WSO2_IS_BASE>/oauth2/jwks` | JWKS endpoint |
| `<INTROSPECT_URL>` | `<WSO2_IS_BASE>/oauth2/introspect` | RFC 7662 introspection endpoint |

Canonical resource URIs (used as `aud` in tokens — must be exact match):

| Resource | URI | Used by |
|---|---|---|
| Orchestrator | `https://orchestrator.smart-employee.local` | First-hop user delegated token |
| HR Agent | `https://hr.smart-employee.local/a2a` | Hop 3a |
| IT Agent | `https://it.smart-employee.local/a2a` | Hop 3b |
| HR Server | `mcp://hr_server.local` | Hop 4 (verify exact value with P13) |
| IT Server | `mcp://it_server.local` | Hop 5 (verify exact value with P14) |

**Note:** these URIs need not be reachable; they're identifiers. They must be exact-string-matched between the API resource registration in WSO2 IS and the validator code.

---

## 2. Step 1 — Application: orchestrator-app

Console → **Applications** → **+ New Application** → **Single-Page Application**

| Field | Value |
|---|---|
| Application name | `orchestrator-app` |
| Authorized redirect URLs | `http://localhost:3001/callback` |
| Allowed origins | `http://localhost:3001` |
| Public client | ✓ (required for PKCE) |

**After creating, click into it →**

### Protocol tab
- ☑ **PKCE** mandatory.
- Grant types — keep what the SPA template offers; **do not try to add Token Exchange here**:
  - ☑ Code (Authorization Code)
  - ☐ Implicit (leave OFF)
  - ☑ Refresh Token
- Access token: **JWT**. Lifetime: 3600 s (default).
- Refresh token lifetime: 86400 s.

**Note on Token Exchange:** the SPA application template intentionally does NOT expose `urn:ietf:params:oauth:grant-type:token-exchange` because SPAs are public clients (no secret) and token-exchange requires confidential authentication. Token exchange in this POC is performed by `orchestrator-agent` (the Agent identity registered in Step 2) using its client_id + client_secret — not by `orchestrator-app`. The SPA app handles only the user-login auth-code flow.

### Advanced tab
- *No App-Native Authentication toggle on a SPA* — that setting is for Agent identities (Step 2), not for SPA apps. The `/oauth2/authn` endpoint is only used by the 3-step agent-credential flow. SPAs use the standard browser auth code flow at `/oauth2/authorize`. **Skip this step for `orchestrator-app`.**
- ☑ **Skip user consent** for *this app's own scopes only* (NOT the `requested_actor` consent — that screen MUST appear).

### Roles tab
- **Role audience: Organization** (gotcha #B — Application by default).

### User Attributes tab
- Mandatory: `username`, `email`, `roles`.

### Logout URLs (Sprint 2 prep — can be deferred until Sprint 2)
- **Back-channel logout URL:** `http://localhost:8090/auth/backchannel-logout`
  *(Orchestrator backend host port; WSO2 IS POSTs the logout token here. If your dev machine is behind NAT, you'll need ngrok pointing at 8090 — not 5001.)*
- Confirm `id_token_signed_response_alg`: typically `RS256`.

**Capture for `.env`:**
- Client ID → `ORCHESTRATOR_APP_CLIENT_ID`
- (No secret — public PKCE client.)

---

## 3. Step 2 — Agent identities (3)

Console → **Agents** (left nav) → **+ Register Agent**. Do this three times.

| Display name | Used for |
|---|---|
| Orchestrator Agent | The acting party in delegated tokens; named in `requested_actor`; mints actor token (Hop 2) and exchanged tokens (Hops 3a/3b). |
| HR Agent | Specialist; mints Hop-4 token for hr_server. |
| IT Asset Agent | Specialist; mints Hop-5 token for it_server. |

For each agent, capture **THREE values** (not two — this is a v3 correction):

1. **Agent ID** — UUID. Visible on the Agent's General/Credentials tab. Used as:
   - The `sub` claim of the agent's tokens.
   - The `username` on `/oauth2/authn` during the 3-step App-Native Auth flow.
2. **Agent Secret** — shown ONCE at creation. Used as the `password` on `/oauth2/authn`.
3. **OAuth Client ID** of the auto-created OAuth Application — per WSO2 IS docs, registering an Agent **also creates a backing OAuth Application**. Visible in **Console → Applications**, named after the agent (e.g., "Orchestrator Agent" or similar). Click into it; the Protocol tab shows its **Client ID**. Used as `client_id` on `/oauth2/authorize` and `/oauth2/token` during the 3-step flow.

**Why three values?** The WSO2 IS agentic-AI docs state agents authenticate via `/oauth2/authn` (NOT `client_credentials`). That flow needs an OAuth client — supplied by the auto-created OAuth App. The Agent ID is the user-like identity asserted at `/authn`; the OAuth App is the OAuth client the flow runs through. They are distinct entities with distinct lifecycle (P17 verifies).

**Note on agent UI tabs (`General` / `Credentials` / `Roles`):**
- **Token Exchange grant** is enabled on the agent's auto-created **OAuth Application's Protocol tab** (not on the agent itself). Find the app in Console → Applications, open Protocol tab, ensure `urn:ietf:params:oauth:grant-type:token-exchange` is checked.
- **App-Native Authentication** must be enabled on that same OAuth Application's Advanced tab (the historic gotcha #A applies to this app). Without it, `/oauth2/authn` returns `ABA-60007`.
- **Role assignment** (`employee`/`hr_admin`) is done in §5 (Step 4).

**Capture into `.env` files** (3 values per agent):

| Agent | `.env` location | Variables |
|---|---|---|
| Orchestrator Agent | `orchestrator/.env` | `ORCHESTRATOR_AGENT_ID`, `ORCHESTRATOR_AGENT_SECRET`, `ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID` |
| HR Agent | `hr_agent/.env` | `HR_AGENT_ID`, `HR_AGENT_SECRET`, `HR_AGENT_OAUTH_CLIENT_ID` |
| IT Asset Agent | `it_agent/.env` | `IT_AGENT_ID`, `IT_AGENT_SECRET`, `IT_AGENT_OAUTH_CLIENT_ID` |

---

## 4. Step 3 — API Resources (2 regular + 2 MCP Servers)

Console → **API Resources**. WSO2 IS distinguishes two registration types under this section:
- **+ New API Resource** — generic OAuth-protected APIs. Use for the agent tier (hr_agent, it_agent — they speak A2A JSON-RPC).
- **+ New MCP Server** — first-class MCP server registration. Use for the backend tier (hr_server, it_server — they speak MCP).

Both bind scopes the same way; both produce JWTs with `aud` set to the registered identifier. The distinction is architectural intent — and it matters for our `_a2a` / `_mcp` scope split.

**Important — WSO2 IS scope-name uniqueness:** scopes are unique within the organization. The architecture uses **`_a2a`** suffix for agent-tier scopes and **`_mcp`** suffix for backend-tier scopes — the suffix names a transport, which happens to map cleanly to the tier. See [scope-policy.md](scope-policy.md) §1 for the rationale.

### 4.1 hr_agent-api (agent-tier — speaks A2A JSON-RPC)
- Identifier (audience): `https://hr.smart-employee.local/a2a`
- Scopes: `hr_basic_a2a`, `hr_self_a2a`, `hr_read_a2a`, `hr_approve_a2a`

### 4.2 it_agent-api (agent-tier — speaks A2A JSON-RPC)
- Identifier: `https://it.smart-employee.local/a2a`
- Scopes: `it_assets_read_a2a`

### 4.3 hr_server (register as **MCP Server**, NOT API Resource)
Console → **API Resources** → **+ New MCP Server** (WSO2 IS has a dedicated MCP Server registration type — use it; it's a better architectural fit since hr_server speaks MCP).

- Identifier: `mcp://hr_server.local`
- Display name: HR Server
- Scopes: `hr_basic_mcp`, `hr_self_mcp`, `hr_read_mcp`, `hr_approve_mcp`

### 4.4 it_server (register as **MCP Server**)
Same wizard as 4.3.

- Identifier: `mcp://it_server.local`
- Display name: IT Server
- Scopes: `it_assets_read_mcp`

---

## 5. Step 4 — Roles + scope assignments

Console → **Roles** → ensure these exist (create if missing).

### `employee`
Scopes — both tiers (`_a2a` for agent-tier, `_mcp` for backend-tier; the user's consent must cover the full token-exchange chain):
- `agent_access` (umbrella — required for orchestrator login)
- Agent-tier: `hr_basic_a2a`, `hr_self_a2a`, `it_assets_read_a2a`
- Backend-tier: `hr_basic_mcp`, `hr_self_mcp`, `it_assets_read_mcp`

### `hr_admin`
Scopes — everything in `employee`, plus:
- Agent-tier: `hr_read_a2a`, `hr_approve_a2a`
- Backend-tier: `hr_read_mcp`, `hr_approve_mcp`

### Assign agents to roles (gotcha #D)

In each role's **Agents** tab, assign:
- `employee` ← `orchestrator-agent`, `hr_agent`, `it_agent`
- `hr_admin` ← same

Without this assignment, the agent identity has no permissions (gotcha #D — same end-user symptom as #C).

---

## 6. Step 5 — Subscribe applications to API Resources + MCP Servers

Subscriptions determine which scopes a given OAuth client can request. Each of our four applications needs the right subscriptions.

### 6.1 — `orchestrator-app` (the SPA)
Console → **Applications** → `orchestrator-app` → **API Resources** tab → Subscribe to:
- `HR Agent API`, `IT Asset Agent API` (so the user-delegated token from Hop 1 can carry `hr_*_a2a`, `it_*_a2a` scopes)
- (No backend-tier MCP Servers here — the SPA never targets backends directly)

### 6.2 — Orchestrator Agent's OAuth Application
Console → **Applications** → find the auto-created app for `orchestrator-agent` (named after the agent) → **API Resources** tab → Subscribe to:
- `HR Agent API`, `IT Asset Agent API` — needed because Hop 3a/3b token-exchange is performed by this app, with `resource=hr_agent-api` or `it_agent-api`. Without subscription, requested scopes/audiences are denied.

### 6.3 — HR Agent's OAuth Application
Console → **Applications** → find the auto-created app for `hr_agent` → **API Resources** tab → Subscribe to:
- `HR Server` (MCP Server) — needed for Hop 4 re-mint targeting `mcp://hr_server.local`.

### 6.4 — IT Agent's OAuth Application
Console → **Applications** → find the auto-created app for `it_agent` → **API Resources** tab → Subscribe to:
- `IT Server` (MCP Server) — needed for Hop 5 re-mint.

### 6.5 — Pattern C `requested_actor` permission

**Empirical finding:** WSO2 IS 7.2 does NOT require an explicit "Authorized Actors" allowlist UI on the front-door application — Pattern C with `requested_actor=<agent-id>` works as long as the named agent is registered with "Allow users to log in" enabled. (Verified by capability test C1 — see `docs/spikes/wso2-is-capability-memo.md` §3 / Appendix A.)

If you *do* see an "Authorized Actors" / "Trusted Agents" tab on `orchestrator-app` in your IS Console, add `orchestrator-agent` to it for completeness. If you don't see it, no action needed.

### 6.6 — Enable required grants on agent OAuth Apps (CIBA, not Token Exchange)

For each agent's auto-created OAuth Application (`orchestrator-agent`'s, `hr_agent`'s, `it_agent`'s): Console → Applications → that App → **Protocol** tab → grant types section.

| Grant | Required for | Notes |
|---|---|---|
| ☑ **Code** (Authorization Code) | The 3-step `/oauth2/authn` flow that mints the agent's `actor_token` (capability test C4) | Already on by default for Agent Apps |
| ☑ **Refresh Token** | Optional helper for actor_token re-mint | Already on by default |
| ☑ **CIBA** (`urn:openid:params:grant-type:ciba`) | **The headline grant for v4.** Each specialist initiates `/oauth2/ciba` to mint a per-user OBO when invoked. | **NOT on by default — must be ticked on each agent app, per Finding F2.** |
| ☐ Token Exchange | NOT used in v4 (per F4: depth-2 nested `act` not supported on IS 7.2) | Leave unchecked. |

After ticking CIBA, configure CIBA properties (in the same tab, under "Client Initiated Backchannel Authentication"):
- **CIBA Authentication Request Expiry Time:** `300` (seconds; gives user 5 minutes to click Approve in the Consent Widget)
- **Allowed Notification Delivery Methods:** ☑ **External (Client Application Handles Delivery)** — **MANDATORY.** Without this, CIBA does not return `auth_url` in the response, breaking the architecture.
- ☐ Email — leave off (we don't push notifications via email in the demo)
- ☐ SMS — leave off

On the **Advanced** tab: ensure **App-Native Authentication** is ON (needed for `/oauth2/authn` 3-step flow that mints actor_tokens).

Click **Update**.

**Repeat for each of the 3 agent OAuth Apps** (orchestrator-agent, hr_agent, it_agent). The orchestrator-agent does NOT actually use CIBA in the runtime (only Pattern C login), but enabling it costs nothing and keeps the three apps symmetrical.

### 6.7 — orchestrator-mcp-client confidential app

Per milestone-plan v4 §1.2: the orchestrator's `/oauth2/token` code-exchange (Pattern C) needs a confidential client. Register a separate Standard-Based App named `orchestrator-mcp-client`:
- Public Client: ☐ OFF (must be confidential — has client_secret)
- Authorized redirect URLs: `http://localhost:8090/auth/callback` (orchestrator backend port — not the SPA port!)
- **Allowed grant types:**
  - ☑ Code (Authorization Code)
  - ☑ Client Credentials
  - ☐ Token Exchange (not used in v4)
- Capture `Client ID` + `Client Secret` → `orchestrator/.env` as `ORCHESTRATOR_MCP_CLIENT_ID` / `_SECRET`

---

## 7. Step 6 — Per-service `.env` files

After completing steps 1–6, populate the following:

### `.env` (root, used by `docker-compose.yml`)
```bash
# (No tenant in URL — default org is carbon.super)
WSO2_IS_BASE_URL=https://13.60.190.47:9443
WSO2_IS_ISSUER=https://13.60.190.47:9443/oauth2/token
WSO2_IS_JWKS_URL=https://13.60.190.47:9443/oauth2/jwks
WSO2_IS_INTROSPECT_URL=https://13.60.190.47:9443/oauth2/introspect

ORCHESTRATOR_APP_CLIENT_ID=<from step 1 — orchestrator-app SPA client_id>

# Per-agent triple (Agent ID + Agent Secret + auto-created OAuth App's Client ID).
# Per scope-policy.md and wso2-is-setup.md §3, agents have THREE values, not two.
ORCHESTRATOR_AGENT_ID=<from step 2 — Agent ID UUID>
ORCHESTRATOR_AGENT_SECRET=<from step 2 — Agent Secret (shown once)>
ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID=<from step 2 — auto-created OAuth App's Client ID>

HR_AGENT_ID=<from step 2 — UUID>
HR_AGENT_SECRET=<from step 2>
HR_AGENT_OAUTH_CLIENT_ID=<from step 2 — auto-created OAuth App's Client ID>

IT_AGENT_ID=<from step 2 — UUID>
IT_AGENT_SECRET=<from step 2>
IT_AGENT_OAUTH_CLIENT_ID=<from step 2 — auto-created OAuth App's Client ID>

CACHE_BUST_HMAC_SECRET=<generate: openssl rand -hex 32>
```

### Per-service `.env` overrides (copied from `_archive/agent.before-v3/.env` for any reused values like `GOOGLE_API_KEY`).

---

## 8. Step 7 — Capability tests (HISTORICAL: probes P0–P19 below are pre-v4)

> **Note for v4:** the M0 spike (now complete — see [`docs/spikes/wso2-is-capability-memo.md`](spikes/wso2-is-capability-memo.md)) replaced these P-probes with a Python suite at [`idp_capability_test/`](../idp_capability_test/). The C0/I1/I4/I8 tests there are the canonical capability validations for v4. The probe descriptions below are kept for historical reference; **specifically, all probes that test RFC 8693 token-exchange (P1, P3, P5, etc.) are obsolete since IS 7.2 does not support depth-2 nested act per F4.** The CIBA flow has its own C8 capability test.
>
> **For v4, run instead:**
> ```bash
> cd idp_capability_test
> source .venv/bin/activate
> python c0_reachability.py    # foundation
> python c1_pattern_c.py        # I1 — Pattern C login (depth-1 act)
> python c4_app_native_authn.py # I4 — Agent self-auth
> python c8_ciba.py             # I8 — Per-agent CIBA
> python c5_multi_audience_ciba.py  # F6 finding (negative — multi-aud not supported)
> python c9_token_lifetime.py   # F7 finding (refresh_token available, unused)
> ```
>
> Use the historical P-probes below ONLY if you need to debug a specific OAuth/OIDC behavior in isolation; they are not part of the M0/M1 sign-off.

> **Convention:** all probes assume the env vars from Step 7 are exported. Run `source .env` (or use direnv) first.

### P0 — Mint orchestrator-agent's actor token via 3-step App-Native Auth (NEW; replaces client_credentials)

Per WSO2 IS's documented agent authentication, agents auth via `/oauth2/authn`, NOT `client_credentials`.
You'll need the **OAuth Client ID** of the auto-created OAuth Application that backs `orchestrator-agent` (find in Console → Applications, look for an app named like "Orchestrator Agent" or similar; capture its Client ID into `ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID`).

```bash
# Step 1: kick off auth flow with response_mode=direct
PKCE_VERIFIER="dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # generate fresh
PKCE_CHALLENGE="$(echo -n "$PKCE_VERIFIER" | openssl dgst -sha256 -binary | openssl base64 -A | tr '+/' '-_' | tr -d '=')"

FLOW_RESP=$(curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/authorize" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d "client_id=$ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID" \
  -d "response_type=code" \
  -d "redirect_uri=http://localhost:5001/callback" \
  -d "scope=openid internal_login" \
  -d "response_mode=direct" \
  -d "code_challenge=$PKCE_CHALLENGE" \
  -d "code_challenge_method=S256")

FLOW_ID=$(echo "$FLOW_RESP" | jq -r .flowId)
AUTHENTICATOR_ID=$(echo "$FLOW_RESP" | jq -r '.nextStep.authenticators[0].authenticatorId')
echo "flowId=$FLOW_ID  authenticatorId=$AUTHENTICATOR_ID"

# Step 2: authenticate with Agent ID + Secret
AUTHN_RESP=$(curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/authn" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg fid "$FLOW_ID" --arg aid "$AUTHENTICATOR_ID" \
                  --arg u "$ORCHESTRATOR_AGENT_ID" --arg p "$ORCHESTRATOR_AGENT_SECRET" \
        '{flowId:$fid, selectedAuthenticator:{authenticatorId:$aid, params:{username:$u, password:$p}}}')")

CODE=$(echo "$AUTHN_RESP" | jq -r .authData.code)
echo "auth code=$CODE"

# Step 3: exchange code for access token
ORCH_ACTOR_TOKEN=$(curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d "grant_type=authorization_code" \
  -d "client_id=$ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID" \
  -d "code=$CODE" \
  -d "code_verifier=$PKCE_VERIFIER" \
  -d "redirect_uri=http://localhost:5001/callback" | jq -r .access_token)

echo "ACTOR token (first 40): ${ORCH_ACTOR_TOKEN:0:40}..."
```

**Pass:** `ORCH_ACTOR_TOKEN` is a non-empty JWT.
**Fail:** Inspect each step's response. Common causes: OAuth Client ID wrong, App-Native Auth not enabled on the agent's OAuth App, redirect_uri mismatch, PKCE method wrong.

### P1 — RFC 8693 token-exchange with `actor_token` populates `act` claim

Uses `ORCH_ACTOR_TOKEN` from P0 as the `actor_token`. You also need a `USER_TOKEN` — a user-delegated token from a real Hop 1 (Pattern C, see P10) or a manually-captured token from the SPA login.

The token-exchange request is made by the **orchestrator-agent's auto-created OAuth Application** (the same OAuth Client ID used in P0), not by `orchestrator-app` (SPA, public PKCE — can't do token-exchange grant).

```bash
EXCHANGED=$(curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/token" \
  -u "$ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID:" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=$USER_TOKEN" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:jwt" \
  -d "actor_token=$ORCH_ACTOR_TOKEN" \
  -d "actor_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "resource=https://hr.smart-employee.local/a2a" \
  -d "scope=hr_read_a2a")

echo "$EXCHANGED" | jq .
echo "$EXCHANGED" | jq -r .access_token | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```

**Pass:** decoded JWT payload contains `act.sub == <ORCHESTRATOR_AGENT_ID>` (or its UUID).
**Fail:** `act` absent or only `sub` populated. Activate fallback in plan §3.6.

> **Note:** the OAuth App for the orchestrator-agent must have **Token Exchange grant** enabled and be **subscribed to `HR Agent API`** (with `hr_read_a2a` granted). If not, you'll get `unauthorized_client` or `invalid_scope`. Fix in WSO2 IS Console → Applications → orchestrator-agent's app → API Resources tab.

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
curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/revoke" \
  -u "$ORCHESTRATOR_APP_CLIENT_ID:" \
  -d "token=$USER_REFRESH_TOKEN" \
  -d "token_type_hint=refresh_token" -i
```

Then check whether the **exchanged** access token from P1 is still valid:

```bash
EX_TOKEN=$(echo "$EXCHANGED" | jq -r .access_token)
curl -sS -X POST "$WSO2_IS_INTROSPECT_URL" \
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
ngrok http 8090  # if behind NAT (orchestrator backend host port)
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
curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/revoke" \
  -u "$ORCHESTRATOR_APP_CLIENT_ID:" \
  -d "token=$USER_TOKEN" \
  -d "token_type_hint=access_token"

# Poll introspect until active=false
while true; do
  ACTIVE=$(curl -sS -X POST "$WSO2_IS_INTROSPECT_URL" \
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
**Fail:** Layer A's 2-s cache + WSO2 IS introspection latency exceeds budget. Lengthen the cache or accept a wider window in `docs/user-experience.md` §6.2.

### ~~P6~~ — DEFERRED (BCL retry policy)
Layer A is the safety net; BCL retry behavior doesn't change the demo's claim. Re-run in production hardening.

### P7 — `sid` in access tokens

Decode any access token (P1's exchanged one is fine):
```bash
echo "$EX_TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```

**Pass:** `sid` claim present.
**Fail:** `sid` only in ID tokens. Sprint 2 stores `sid` from ID token at login (already planned).

### P8 — Nested `act` chain across two exchanges (corresponds to P12a)

Take P1's exchanged token (`EX_TOKEN`); use it as `subject_token` in another exchange (e.g., HR Agent → hr_server, Hop 4 in §3.3 of the plan).

**First, mint hr_agent's actor token via the 3-step App-Native Auth flow (same shape as P0, but with hr_agent's credentials):**

```bash
# 1) /authorize with response_mode=direct, hr_agent's OAuth App as client
PKCE_VERIFIER_HR="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
PKCE_CHALLENGE_HR="$(echo -n "$PKCE_VERIFIER_HR" | openssl dgst -sha256 -binary | openssl base64 -A | tr '+/' '-_' | tr -d '=')"

FLOW_RESP_HR=$(curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/authorize" \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d "client_id=$HR_AGENT_OAUTH_CLIENT_ID" \
  -d "response_type=code" \
  -d "redirect_uri=http://localhost:8001/callback" \
  -d "scope=openid internal_login" \
  -d "response_mode=direct" \
  -d "code_challenge=$PKCE_CHALLENGE_HR" \
  -d "code_challenge_method=S256")

FLOW_ID_HR=$(echo "$FLOW_RESP_HR" | jq -r .flowId)
AUTH_ID_HR=$(echo "$FLOW_RESP_HR" | jq -r '.nextStep.authenticators[0].authenticatorId')

# 2) /authn with hr_agent credentials
AUTHN_RESP_HR=$(curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/authn" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg fid "$FLOW_ID_HR" --arg aid "$AUTH_ID_HR" \
                  --arg u "$HR_AGENT_ID" --arg p "$HR_AGENT_SECRET" \
        '{flowId:$fid, selectedAuthenticator:{authenticatorId:$aid, params:{username:$u, password:$p}}}')")
CODE_HR=$(echo "$AUTHN_RESP_HR" | jq -r .authData.code)

# 3) /token to get hr_agent actor token
HR_ACTOR_TOKEN=$(curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/token" \
  -d "grant_type=authorization_code" \
  -d "client_id=$HR_AGENT_OAUTH_CLIENT_ID" \
  -d "code=$CODE_HR" \
  -d "code_verifier=$PKCE_VERIFIER_HR" \
  -d "redirect_uri=http://localhost:8001/callback" | jq -r .access_token)
```

**Then perform Hop 4-style RFC 8693 token-exchange, using hr_agent's OAuth App as the confidential client:**

```bash
HR_SERVER_TOKEN=$(curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/token" \
  -u "$HR_AGENT_OAUTH_CLIENT_ID:" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=$EX_TOKEN" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "actor_token=$HR_ACTOR_TOKEN" \
  -d "actor_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "resource=mcp://hr_server.local" \
  -d "scope=hr_read_mcp")

echo "$HR_SERVER_TOKEN" | jq -r .access_token | cut -d. -f2 | base64 -d 2>/dev/null | jq '.act'
```

**Pass:** `{"sub": "<HR_AGENT_ID>", "act": {"sub": "<ORCHESTRATOR_AGENT_ID>"}}` — chain depth 2 preserved.
**Fail:** flattened to `{"sub": "<HR_AGENT_ID>"}` only. Drop the stretch HR→IT specialist-to-specialist demo; lock validators to depth-1.

> **Note:** for the token-exchange to succeed, `hr_agent`'s auto-created OAuth Application must be **subscribed to `HR Server` MCP Server** (per §6.3) and have **Token Exchange grant** enabled (per §6.6). Otherwise expect `unauthorized_client` or `invalid_scope`.

### ~~P9~~ — DEFERRED (jti stability)
Implementation falls back to `hash(token)` as cache key unconditionally.

### P10 — `requested_actor` populates `act.sub` (Pattern C — Hop 1)

Two modes to test, in order:

**P10.A — Without `actor_token` attached (lazy mode):**
SPA does the full PKCE flow itself. Login URL includes `&requested_actor=<ORCHESTRATOR_AGENT_ID>`. SPA does code-exchange directly at `/oauth2/token` with the PKCE verifier (no orchestrator backend in the path).

```bash
# Decode the resulting access token after SPA login completes:
echo "$USER_TOKEN_FROM_SPA" | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```

**Pass:** `act.sub == <ORCHESTRATOR_AGENT_ID>`. ⇒ Pattern C works WITHOUT actor_token attached. SPA can do code-exchange directly. Simpler implementation.
**Fail:** `act` absent. Move to P10.B.

**P10.B — With `actor_token` attached at code-exchange (BFF mode, full Pattern C per docs):**
SPA forwards the auth code + verifier to orchestrator backend; orchestrator backend performs code-exchange with `Authorization: Bearer $ORCH_ACTOR_TOKEN` header.

```bash
USER_TOKEN=$(curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/token" \
  -H "Authorization: Bearer $ORCH_ACTOR_TOKEN" \
  -d "grant_type=authorization_code" \
  -d "client_id=$ORCHESTRATOR_APP_CLIENT_ID" \
  -d "code=<auth code from SPA callback>" \
  -d "code_verifier=<the PKCE verifier the SPA generated>" \
  -d "redirect_uri=http://localhost:3001/callback" | jq -r .access_token)

echo "$USER_TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```

**Pass:** `act.sub == <ORCHESTRATOR_AGENT_ID>` after BFF flow. ⇒ Hop 1 implementation MUST use BFF (orchestrator backend does code-exchange). Document this in plan §3.3.
**Fail (both A and B):** Major fallback per plan §3.6 — escalate to WSO2 support / community forum or use RFC 8693 for the first hop.

### P11 — Consent screen rendered for `requested_actor`

During the P10 walkthrough, observe whether WSO2 IS renders a consent prompt naming the orchestrator agent.

**Pass:** screen visible, with **Approve** and **Deny** buttons. Click Deny once and verify the deny-flow surfaces an error in the SPA (UX scenario A).
**Fail:** no consent screen. Configure in-orchestrator consent (weaker; document the deviation).

### P12 — Composing `requested_actor` token with subsequent RFC 8693 exchange

P10 produces a user delegated token with `act.sub=orchestrator-agent`. Now use it as `subject_token` in token-exchange (Hop 3a):

```bash
# Same call shape as P1, with subject_token=$USER_DELEGATED_TOKEN (from P10)
echo "$EXCHANGED" | jq -r .access_token | cut -d. -f2 | base64 -d 2>/dev/null | jq '.act'
```

**Pass:** `{"sub": "orchestrator-agent", "act": {"sub": "orchestrator-agent"}}` (preserves the existing `act` from `requested_actor`). Chain composes.
**Acceptable but limited:** `{"sub": "orchestrator-agent"}` only — WSO2 IS flattens. Document; locks the validator to depth-1; HR→IT chain (depth 3) becomes infeasible.
**Fail:** `act` absent entirely. Major fallback — escalate to WSO2 support / community forum.

### P13 — `hr_server` `EXPECTED_AUD`

Verify what `aud` the existing `hr_server/auth/jwt_validator.py` checks against:

```bash
grep -nE "aud|audience|EXPECTED" hr_server/auth/jwt_validator.py
grep -nE "AUD|audience" hr_server/.env hr_server/config.py 2>/dev/null
```

**Pass:** record the exact value (e.g., `mcp://hr_server.local` or the MCP Client app's client_id). Use that as `resource` in Hop 4 token-exchange.
**Fail:** value cannot be determined → reconfigure hr_server to a known canonical URI before Hop 4 testing.

### P14 — `it_server` `EXPECTED_AUD`

Symmetric to P13, but `it_server` is greenfield in v3 — choose the value at MCP Server registration (Step 4.4 above; we picked `mcp://it_server.local`) and configure the new `it_server/auth/jwt_validator.py` to expect exactly that.

**Pass:** registered resource URI === validator's expected value === Hop 5 token-exchange `resource` parameter, all three identical strings.

### P15 (new) — actor_token shape, lifetime, introspection behavior

Decode the actor token from P0 and check its structure:
```bash
echo "$ORCH_ACTOR_TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```
Then introspect it (using orchestrator-agent's OAuth App credentials):
```bash
curl -sS -X POST "$WSO2_IS_INTROSPECT_URL" \
  -u "$ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID:" \
  -d "token=$ORCH_ACTOR_TOKEN" | jq .
```

**Pass:** Document in spike memo: token format (JWT/opaque), `exp` (TTL), `aud`, `scope`, introspection `active=true`. Used by Sprint 1's actor-token cache to choose refresh strategy.

### P16 (new) — `/oauth2/authn` rate-limiting / brute force protection

Run a tight loop sending bad credentials:
```bash
for i in $(seq 1 20); do
  curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$WSO2_IS_BASE_URL/oauth2/authn" \
    -H 'Content-Type: application/json' \
    -d '{"flowId":"fake","selectedAuthenticator":{"authenticatorId":"x","params":{"username":"bad","password":"bad"}}}'
done
```

**Pass:** WSO2 IS applies a sensible rate limit (some 429s appear, or auth attempts are throttled tenant-wide).
**Fail:** Document and treat as a known POC limitation; production hardening adds in-app throttling.

### P17 (new) — Agent vs auto-created OAuth App revocation

Console → **Agents** → orchestrator-agent → **Block Agent** toggle. Then immediately:
```bash
curl -sS -X POST "$WSO2_IS_INTROSPECT_URL" \
  -u "$ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID:" \
  -d "token=$ORCH_ACTOR_TOKEN" | jq .active
```

**Pass:** `active=false` within 5 s.
**Fail:** Token still active. Document — Sprint 2 needs separate revocation operation per surface.

### P18 (new) — User-session revocation cascade to act-bearing tokens

Capture a USER_TOKEN with `act.sub=orchestrator-agent` (from P10). Revoke the user's session via Asgardeo:
```bash
curl -sS -X POST "$WSO2_IS_BASE_URL/oauth2/revoke" \
  -u "$ORCHESTRATOR_APP_CLIENT_ID:" \
  -d "token=$USER_REFRESH_TOKEN" -d "token_type_hint=refresh_token"
```
Then introspect USER_TOKEN.

**Pass:** `active=false` within 5 s. Document for Sprint 2's Layer A.

### P19 (new) — actor_token revocation cascade to exchange-derived tokens

Take `EXCHANGED` (from P1; Hop 3a output). Revoke ORCH_ACTOR_TOKEN. Introspect EXCHANGED.

**Pass:** `EXCHANGED` becomes inactive (good — Sprint 2's revocation is implicit through actor-token cascade).
**Fail:** `EXCHANGED` stays active until natural exp. Sprint 2 must rely on Layer A introspection only.

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
App-Native Auth is disabled on the Agent identity (or wherever the 3-step `/oauth2/authn` flow expects it). For an Agent identity in Asgardeo, this toggle is on the Agent's settings tab (not the SPA app). If you don't see it, your tenant may auto-enable it for Agent-type identities — proceed and let probe P1 verify.

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
