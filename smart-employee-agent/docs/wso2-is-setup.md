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
| 6 | `HR API` | **API Resource** with 4-tier scopes `hr_basic_rest`, `hr_self_rest`, `hr_read_rest`, `hr_approve_rest` | Subscribed by hr-agent's auto-created Agent App | NEW |
| 7 | `IT API` | **API Resource** with scopes `it_assets_read_rest`, `it_assets_write_rest` | Subscribed by it-agent's auto-created Agent App | NEW |
| 8 | `HR Admin` | **Role** (Organization audience) | Grants `hr_read_rest`, `hr_approve_rest`, `it_assets_write_rest`; assigned to user `hr.admin` | NEW |
| 9 | `hr.admin` user | Local user | Tests HR-Admin-only flows (approve leave, issue assets) | NEW |

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

**User Attributes tab — Subject (S5.12) — REQUIRED:**
- ☑ **Email** under "User Attribute Selection".
- Under **Subject**: ☑ **Assign alternate subject identifier**, **Subject attribute = Email**, **Subject type = Public**.
- Why: token-A (this app's user token, behind the SPA's `/api/me/*` proxy) must carry the **same `sub`** as the per-agent CIBA/OBO tokens (token-C, issued by `hr-agent`/`it-agent`, which are also email-subject — see Step 3). If this app emits the default `userid` UUID while the agent apps emit the email, per-user state desyncs (e.g. a leave applied via chat doesn't show in "My Leaves"). The original S5.11 diagnosis assumed this app was already email-subject — verify it actually is.

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
- ☑ **App-Native Authentication** (needed for `/oauth2/authn` 3-step flow). Note: this toggle may be absent on Agent OAuth Apps in IS 7.2 — agents have it auto-enabled. Don't worry if you can't find it; C4 probe in M0 confirmed the flow works.

**Roles tab — IMPORTANT:**
- **Role Audience: Organization** (default is Application; flip to Organization). Application-audience roles don't propagate across the agent chain; Organization audience does. Required for Sprint 2/3 role-based scopes (e.g. `hr_admin` for `approve_leave`).

**User Attributes tab — Subject (S5.12) — DO THIS for `hr-agent` and `it-agent` (NOT `orchestrator-agent`):**
- Under **Subject**: ☑ **Assign alternate subject identifier**, **Subject attribute = Email**, **Subject type = Public**, leave "Include user domain" / "Include organization name" unchecked. Also ☑ **Email** under "User Attribute Selection".
- Why: the per-agent CIBA/OBO token (token-C) issued by these apps must carry the **same `sub`** as the orchestrator session token (token-A from `orchestrator-mcp-client` — which **also** needs Subject = Email, see Step 2), or per-user state keyed by `sub` desyncs across code paths. **All three of `orchestrator-mcp-client` + `hr-agent` + `it-agent` must agree** — set the same Subject config on all three. See [`architecture/identity-subject-mismatch.md`](architecture/identity-subject-mismatch.md). The agent's *own* actor token is unaffected — an agent identity has no `email` attribute, so IS falls back to the `userid` UUID for it (which is what `act.sub` / `trusted_act_subs` need).
- **`orchestrator-agent`**: leave the default subject. It only ever mints its own actor token (the user code-exchange happens at `orchestrator-mcp-client`), so flipping it buys nothing.

Click **Update** to save.

Capture from the Agent's General/Credentials tab AND the auto-created OAuth App's Info/Protocol tab:

| Agent | Env file | Vars |
|---|---|---|
| `orchestrator-agent` | `orchestrator/.env` | `ORCHESTRATOR_AGENT_ID` (Agent ID), `ORCHESTRATOR_AGENT_SECRET`, `ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID`, `ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET` |
| `hr-agent` | `hr_agent/.env` | `HR_AGENT_ID`, `HR_AGENT_SECRET`, `HR_AGENT_OAUTH_CLIENT_ID`, `HR_AGENT_OAUTH_CLIENT_SECRET` |
| `it-agent` | `it_agent/.env` | `IT_AGENT_ID`, `IT_AGENT_SECRET`, `IT_AGENT_OAUTH_CLIENT_ID`, `IT_AGENT_OAUTH_CLIENT_SECRET` |

ALSO: copy `HR_AGENT_OAUTH_CLIENT_ID` into `hr_server/.env` as `HR_SERVER_EXPECTED_AUD`. Same for IT side. (The MCP server's expected audience is the paired agent's OAuth Client ID — see F-17.)

---

## 5. Step 4 — Create the 2 API Resources (legacy 4-tier HR + 2-tier IT)

Console → **API Resources** → **+ New API Resource** (regular, NOT MCP Server — see F-17 note above).

The scope structure mirrors the existing `hr_server/service/hr_service.py` 4-tier organization, which the original Sprint 0 demo built around. The Wave 6 build (`hr_server/mcp/tools.py`) used simplified `hr.read`/`hr.write` names; those will be patched after this step to match the legacy tier names below.

### 5.1 HR API
- **Identifier:** `urn:hr:api`
- **Display name:** HR API
- **Scopes (5):**

| Scope | Display name | Description | Tier / who has it |
|---|---|---|---|
| `hr_basic_rest` | View company HR info | Holidays, leave policy | every employee |
| `hr_self_rest` | View own leave info | Own leave balance + own requests | every employee (default Sprint 1 demo path) |
| `hr_read_rest` | View all leave requests | Dashboard view across all employees + cubicle/seat reads (`get_cubicle_summary`, `get_vacant_cubicles_on_floor`, `lookup_employee`) | **HR Admin only** |
| `hr_approve_rest` | Approve/reject leave | Modify leave requests; also `get_all_leave_requests` (the `hr.read_all_leaves` chat skill) | **HR Admin only** |
| `hr_assets_write_rest` | Assign cubicles/seats | `assign_cubicle` (the cubicle/seat-assign chat flow) | **HR Admin only** |

### 5.2 IT API
- **Identifier:** `urn:it:api`
- **Display name:** IT API
- **Scopes (2):**

| Scope | Display name | Description | Tier / who has it |
|---|---|---|---|
| `it_assets_read_rest` | View IT asset info | Look up employee asset assignments | every employee |
| `it_assets_write_rest` | Issue IT assets to employees | Assign laptops, mice, screens (HR onboarding flow) | **HR Admin only** |

## 5.5 Step 4.5 — Roles + HR Admin user

Console → **Roles** → **+ New Role**

| Role | Audience | Granted scopes |
|---|---|---|
| `HR Admin` | **Organization** | **All HR API scopes** (`hr_basic_rest`, `hr_self_rest`, `hr_read_rest`, `hr_approve_rest`) **+ `it_assets_write_rest`**. Rationale: HR admins also take their own leave; they should be a superset of Employee for HR-domain operations. |
| `Employee` | **Organization** | `hr_basic_rest`, `hr_self_rest`, `it_assets_read_rest`. Granted to `employee_user`. |

The denial path tests (N30, N31, UC-08) still work cleanly: `employee_user` does NOT have `hr_approve_rest` or `it_assets_write_rest` in its role, so any CIBA request for those scopes fails — that's the security demo.

Console → **Users** → **+ Add User**:

| Username | Email Address (required) | Password | Role |
|---|---|---|---|
| `employee_user` | `employee_user@example.com` | `NewsMax@1234` | **Employee** (explicit role assignment) |
| `hr_admin_user` | `hr_admin_user@example.com` | `NewsMax@1234` | **HR Admin** (Employee tier inherited if not explicit) |
| `probe.user` | `probe.user@example.com` | `NewsMax@1234` | (legacy from M0 spike — keep for capability tests; not used in the demo) |

> **One hard requirement for every user that will sign in (including live demo-day accounts):**
> **The user MUST have an `emailaddress` attribute.** With the OAuth apps set to email-subject (§4) the `sub` is that email — which is both the per-user data key (token-A == token-C) *and* the CIBA `login_hint` (resolved via Multi-Attribute Login — see §5.6). A user *without* an email falls back to the `userid` UUID — still self-consistent for keying, and IS still resolves a UUID `login_hint`, so it won't break, but set the email anyway.
>
> *(S5.12→S5.17 also required `username == email-local-part`; **that constraint is gone as of S5.18** — Multi-Attribute Login (§5.6) makes IS resolve the email `login_hint` to whatever the user's actual username is.)*
>
> There is **no** seeded demo roster — the demo runs against whatever users you create here; `lookup_employee` / the report username→email joins resolve them from the token's profile claims on first sign-in.

Credentials captured in `scripts/probes/.test-users.env` (gitignored).

---

## 5.6 Step 4.6 — Enable Multi-Attribute Login (email) — *required for CIBA*

Console → **Login & Registration** → **Alternative Login Identifiers** (some builds: *Account Login* / *Login Identifier*):

- Toggle **Enabled** on.
- **Allowed Attribute List**: `http://wso2.org/claims/username,http://wso2.org/claims/emailaddress` (keep `username`; add the email claim).
- **Update**.

Why: each per-action CIBA call sends `login_hint = <the inbound token's sub>` = the user's **email**. WSO2 IS's CIBA user resolver (`DefaultCibaUserResolver`) checks the multi-attribute-login service **first**, so with the email claim in the allowed list it resolves that `login_hint` straight to the local user — *regardless of what their username is*. Without this, an email `login_hint` whose local-part isn't a username produces `external notification channel is not supported for federated users` (HTTP 400) and every write tool fails.

(Optional: enabling **Uniqueness Validation** on the Email attribute — *Attributes → Email* — is good practice but not required for the demo. The orchestrator no longer mangles the `login_hint`; it sends the `sub` verbatim, so a bare-UUID `sub` for a user with no email attribute still resolves via IS's userid branch.)

---

## 6. Step 5 — Subscribe agent OAuth Apps to API Resources

For each agent's **auto-created OAuth Application**:

| Subscribe app | To resource | Authorized scopes |
|---|---|---|
| `hr-agent`'s OAuth App | HR API | **all 5**: `hr_basic_rest`, `hr_self_rest`, `hr_read_rest`, `hr_approve_rest`, `hr_assets_write_rest` |
| `it-agent`'s OAuth App | IT API | **all of**: `it_assets_read_rest`, `it_assets_write_rest`, `it_assets_self_rest` |

(The agent's CIBA initiation requests a SUBSET of these per-tool; the IS consent screen narrows further to what the user's role permits. So an Employee asking for `hr_approve_rest` will be denied by IS even though hr-agent's OAuth App is subscribed to it. **Conversely, if the OAuth app is NOT subscribed to a scope a tool requests, IS silently strips it from the issued token-C — the failure surfaces as a 401/ERR-MCP-003 from the MCP server, not at CIBA initiation.** `hr_assets_write_rest` is easy to miss here — `scripts/check-is-config.py` Section 4c verifies it.)

Console → Applications → that app → **API Authorization** tab → **+ Authorize resource** → pick the API → check the scopes → **Add**.

`orchestrator-agent`'s OAuth App does NOT need any API subscriptions (it doesn't call MCP backends).

---

## 7. Step 6 — Test users

Console → **Users** → **+ Add User**

For the demo, one user is sufficient (you can add more for multi-role testing later):

| Username | Password | Role |
|---|---|---|
| `probe.user` (already exists from M0 spike — reuse) | `NewsMax@1234` | (none required for Sprint 1 happy path) |

For HR-Admin-only flows (approve leave, issue assets), use `hr.admin` from Step 4.5. Roles wiring is set up in 4.5; Sprint 1 demo's canonical UC-03 query (leave balance + asset list) only needs `probe.user` (Employee tier).

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
HR_CIBA_SCOPE=openid hr_self_rest

# MCP backend
HR_MCP_SERVER_URL=http://hr-server:8000

HR_AGENT_HOST=0.0.0.0
HR_AGENT_PORT=8001
```

### `it_agent/.env`
Identical shape with `IT_*` prefixes; `IT_CIBA_SCOPE=openid it_assets_read_rest`; `IT_MCP_SERVER_URL=http://it-server:8004`.

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

### `"external notification channel is not supported for federated users"` from `/oauth2/ciba`
IS couldn't resolve the `login_hint` (= the inbound token's `sub`, an email) to a local user. Most likely **Multi-Attribute Login is off / missing the email claim** — re-check Step 4.6 (allowed list must include `http://wso2.org/claims/emailaddress`). Also possible: the user has no `emailaddress` attribute (then `sub` is a UUID — that *should* still resolve), or an agent OAuth app lost its email-subject config (Step 4's User Attributes → Subject). Background: [`architecture/identity-subject-mismatch.md`](architecture/identity-subject-mismatch.md) §6.

### Leave applied via chat doesn't appear in "My Leaves" / a sidebar widget is empty after a chat action
The per-user `sub` keying desynced — `hr-agent`/`it-agent` lost their **Subject = Email** config (Step 4, User Attributes → Subject), so token-C carries a UUID while token-A carries the email. Re-apply it. Also confirm the user has an `emailaddress` attribute. Background: [`architecture/identity-subject-mismatch.md`](architecture/identity-subject-mismatch.md).

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
