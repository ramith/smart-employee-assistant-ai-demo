# Fresh WSO2 IS deployment — smart-employee-agent (rebuild runbook)

**Purpose:** stand up a brand-new WSO2 Identity Server for this POC from scratch (e.g. after a broken/reconfigured box). Reverse-engineered from `docs/wso2-is-setup.md`, `scripts/check-is-config.py`, `common/auth/*`, the three service `config.py` files, `docker-compose.yml`, and the spike docs. Where it differs from `docs/wso2-is-setup.md`, this file is the corrected version (redirect URIs, the 3rd IT scope, back-channel-logout config, role-scope matrix).

**Target:** WSO2 Identity Server **7.3.x** (the project text says 7.2; the runbook is version-agnostic and also works on the 7.3.0 RC). Console at `https://<HOST>:9443/console`, tenant `carbon.super`, default admin `admin/admin`.

**You will create:** 2 OAuth apps + 3 agents (each auto-creates a backing OAuth app) + 2 API resources (10 scopes total) + 2 roles + 2 users → then fill 5 `.env` files → then verify with `scripts/check-is-config.py`.

**Time budget:** ~45–60 min the first time.

---

## Step 0 — Install IS

1. On the VM: download `wso2is-7.3.0.zip` from wso2.com and `unzip` it (e.g. `/home/ubuntu/wso2is-7.3.0`). Below, `<IS_HOME>` = that directory.
2. Java 17+ on PATH.
3. **Decide your database now.** Out of the box IS uses an embedded H2 DB — fine for a throwaway demo, but it loses state on a fresh unzip and isn't what we want for a stable deployment. **Do [Step 0.5](#step-05--point-the-is-databases-at-mysql-recommended) before the first startup** if you're using MySQL. (If you start on H2 first and switch later, you'll re-run the same scripts and lose the H2 data — so just do it up front.)
4. Start: `<IS_HOME>/bin/wso2server.sh` (Linux) / `bin\wso2server.bat` (Windows). Wait for `WSO2 Carbon started`.

---

## Step 0.5 — Point the IS databases at MySQL (recommended)

*Based on the WSO2 docs: <https://is.docs.wso2.com/en/next/deploy/configure/databases/carbon-database/change-to-mysql/>. WSO2 IS 7.x needs two databases — `WSO2_IDENTITY_DB` (identity/OAuth/consent/UMA data) and `WSO2_SHARED_DB` (registry + config). Do all of this **before the first startup**.*

**a. Create the databases** (MySQL 8.x; adjust user/host as you like):
```sql
CREATE DATABASE WSO2_IDENTITY_DB CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
CREATE DATABASE WSO2_SHARED_DB   CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;

CREATE USER 'wso2carbon'@'%' IDENTIFIED BY 'wso2carbon';
GRANT ALL PRIVILEGES ON WSO2_IDENTITY_DB.* TO 'wso2carbon'@'%';
GRANT ALL PRIVILEGES ON WSO2_SHARED_DB.*   TO 'wso2carbon'@'%';
FLUSH PRIVILEGES;
```

**b. Drop in the MySQL JDBC driver.** Download the MySQL Connector/J jar (e.g. `mysql-connector-j-8.x.x.jar`) and copy it to `<IS_HOME>/repository/components/lib/`.

**c. Load the schemas** — run the bundled scripts against the two DBs:
```bash
# Identity DB — three scripts:
mysql -u wso2carbon -p WSO2_IDENTITY_DB < <IS_HOME>/dbscripts/identity/mysql.sql
mysql -u wso2carbon -p WSO2_IDENTITY_DB < <IS_HOME>/dbscripts/uma/mysql.sql
mysql -u wso2carbon -p WSO2_IDENTITY_DB < <IS_HOME>/dbscripts/consent/mysql.sql

# Shared (registry/config) DB — one script:
mysql -u wso2carbon -p WSO2_SHARED_DB   < <IS_HOME>/dbscripts/mysql.sql
```
*(Alternatively you can start the server once with `-Dsetup` and let it populate the schemas, but running the scripts by hand is the documented, predictable path.)*

**d. Edit `<IS_HOME>/repository/conf/deployment.toml`** — replace/add the two datasource blocks:
```toml
[database.identity_db]
type     = "mysql"
hostname = "localhost"
port     = "3306"
name     = "WSO2_IDENTITY_DB"
username = "wso2carbon"
password = "wso2carbon"
# Optional explicit URL (handy on MySQL 8 to pin TLS/timezone):
# url = "jdbc:mysql://localhost:3306/WSO2_IDENTITY_DB?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC"

[database.identity_db.pool_options]
maxActive         = "80"
maxWait           = "360000"
minIdle           = "5"
validationQuery   = "SELECT 1"
validationInterval = "30000"
defaultAutoCommit = false
commitOnReturn    = true

[database.shared_db]
type     = "mysql"
hostname = "localhost"
port     = "3306"
name     = "WSO2_SHARED_DB"
username = "wso2carbon"
password = "wso2carbon"
# url = "jdbc:mysql://localhost:3306/WSO2_SHARED_DB?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC"

[database.shared_db.pool_options]
maxActive         = "80"
maxWait           = "360000"
minIdle           = "5"
validationQuery   = "SELECT 1"
validationInterval = "30000"
defaultAutoCommit = false
commitOnReturn    = true
```
Notes: `type = "mysql"` lets IS pick the driver class (`com.mysql.cj.jdbc.Driver` for Connector/J 8). If you set an explicit `url`, on MySQL 8 add `allowPublicKeyRetrieval=true&useSSL=false` (dev) and `serverTimezone=UTC` to avoid the common connect/timezone errors. Use the *same* MySQL user/password as in Step 0.5a.

**e. Now start the server** (Step 0.4). On a clean MySQL it should come up against the new DBs; check `<IS_HOME>/repository/logs/wso2carbon.log` for `Started WSO2 Carbon` with no DB errors.

---

## Step 0.9 — First login + email login

1. Open `https://<HOST>:9443/console`, log in `admin`/`admin`, set a new admin password when prompted.
   - **Keep `admin` as the userstore username.** Email login (next step) will let you sign in to the Console as `admin@example.com`, but the Management/SCIM REST APIs and `check-is-config.py` Basic-auth against the *userstore username* — that stays `admin`. Don't rename the underlying user.
2. **Enable email login for everyone (Multi-Attribute Login).** Console → **Login & Registration → Alternative Login Identifiers** (a.k.a. Multi-Attribute Login): turn it ON and add `http://wso2.org/claims/emailaddress` to the allowed list. Now any user — admins included — can log in at the Console / SPA with their email address (provided that user has the Email attribute set). This is **required** for this deployment, not optional.
   - **Set the admin's email** so admins can use email login: Console → **Users → admin → Profile** → set Email = `admin@example.com` (or whatever you want). Then `admin@example.com` works at the login screen; `admin` still works too; REST Basic auth still uses `admin`.
   - Note: even with email login on, every demo *end user* you create in Step 4 should still follow the **`username == email`** convention (set the username field itself to the email). That makes `username == email == sub == login_hint`, which is what the CIBA `login_hint` resolution wants — Multi-Attribute Login is then just a convenience/fallback layer on top.
3. **`deployment.toml` sanity check:** leave account-lockout at defaults — agents are not subject to it. If you still have the broken box's `deployment.toml`, diff it against the pristine one from the zip (plus your Step 0.5 MySQL blocks) — an unintended edit there (userstore / agent-identity / auth-framework block) is a likely cause of agent-auth breakage.

> **Note on the `AGENT` userstore.** WSO2 IS 7.x stores agent identities in a secondary userstore (`repository/deployment/server/userstores/AGENT.xml`); their SCIM `userName` is `AGENT/<uuid>`. This is created automatically by the Agents feature — you don't configure it by hand. If `/oauth2/authn` for an agent fails with `ABA-60003 login.fail` even with a freshly-regenerated secret, suspect either the secret value or a `deployment.toml`/userstore misconfig.

---

## Step 1 — `orchestrator-mcp-client` (confidential OIDC app — the runtime client)

Console → **Applications → + New Application → Standard-Based Application → OIDC**.

| Field | Value |
|---|---|
| Name | `orchestrator-mcp-client` |
| Public client | OFF |
| Client authentication | Client Secret Basic |
| Authorized redirect URLs | `http://localhost:8090/agent-callback` **and** `http://localhost:8090/auth/callback` (the codebase has both spellings in flight; register both) |
| Allowed origins | `http://localhost:3001`, `http://localhost:8090` |

- **Protocol tab:** ☑ Authorization Code, ☑ PKCE Mandatory, ☑ Refresh Token, ☑ Client Credentials. Access token = JWT, lifetime 3600s. **Token Exchange OFF** (RFC 8693 is not used in v4).
- **Sign-out / logout:** **Back-channel logout URL** = `http://localhost:8123/backchannel-logout` · **Post-logout redirect URLs** = `http://localhost:8090/`
- **User Attributes tab → Subject:** ☑ *Assign alternate subject identifier*, **Subject attribute = Email**, **Subject type = Public** (leave "include user domain"/"include organization name" unchecked); and ☑ **Email** under "User Attribute Selection". *(Critical: token-A's `sub` must be the user's email so it matches the agents' CIBA tokens — see `docs/architecture/identity-subject-mismatch.md`.)*
- *(Optional — only needed for `check-is-config.py` Section 9 to fully pass: also enable the **Password** grant on this app. Not needed for the demo itself.)*
- **Capture:** Client ID → `ORCHESTRATOR_MCP_CLIENT_ID`; Client Secret → `ORCHESTRATOR_MCP_CLIENT_SECRET`.
- **API Authorization tab:** leave for now — you'll subscribe this app to HR + IT APIs at the end of Step 4 (after the resources exist).

> The legacy SPA app `orchestrator-app` from the old setup doc is **vestigial** — `ORCHESTRATOR_APP_CLIENT_ID` is not required by current code. Skip it.

---

## Step 2 — API Resources + scopes

Console → **API Resources → + New API Resource** (a plain API resource — **not** the "MCP Server" entity; per F-17 the MCP-Server `aud` binding doesn't apply on the CIBA path).

**HR API**
- Identifier: `urn:hr:api` · Display name: `HR API`
- Scopes (5):

  | Scope | Display name | Who has it |
  |---|---|---|
  | `hr_basic_rest` | View company HR info (holidays, policy) | every employee |
  | `hr_self_rest` | View own leave info | every employee |
  | `hr_read_rest` | View all leave requests + cubicle/seat reads | HR Admin only |
  | `hr_approve_rest` | Approve/reject leave | HR Admin only |
  | `hr_assets_write_rest` | Assign cubicles/seats | HR Admin only |

**IT API**
- Identifier: `urn:it:api` · Display name: `IT API`
- Scopes (3):

  | Scope | Display name | Who has it |
  |---|---|---|
  | `it_assets_read_rest` | View IT asset info | every employee |
  | `it_assets_self_rest` | View own IT assets | every employee |
  | `it_assets_write_rest` | Issue IT assets to employees | HR Admin only |

---

## Step 3 — Roles (Organization audience)

Console → **Roles → + New Role**. **Audience = Organization** for both. No inheritance — attach every scope explicitly.

| Role (exact name) | Attached scopes |
|---|---|
| `employee` *(lowercase)* | `hr_basic_rest`, `hr_self_rest`, `it_assets_read_rest`, `it_assets_self_rest` |
| `HR Admin` | all of `employee`'s **plus** `hr_read_rest`, `hr_approve_rest`, `hr_assets_write_rest`, `it_assets_write_rest` |

*(This is the matrix `scripts/check-is-config.py` Section 8b enforces. The denial-path demo works because an `employee`-role user lacks `hr_approve_rest` / `*_write_rest`, so CIBA requests for those scopes get narrowed away at consent.)*

---

## Step 4 — Users (`username == email` convention)

Console → **Users → + Add User**. For each user, set the **username to the email address** and set the **Email** attribute to the same value. Set first/last names too (used by `lookup_employee` and report joins).

| Username = Email | Password | Role assignment |
|---|---|---|
| `employee@example.com` | `NewsMax@1234` | `employee` |
| `hradmin@example.com` | `NewsMax@1234` | `HR Admin` |

> **Why `username == email`:** the email is the OIDC `sub` (Step 1 & Step 5 subject config), so it's both the per-user data key (token-A == token-C) and the CIBA `login_hint`. Making the username equal it too means `username == email == sub == login_hint` — one identifier, and the `login_hint` resolves directly as a plain username. (Multi-Attribute Login from Step 0.4 is still enabled — it lets users *also* type their email at any login screen, and acts as a fallback if a user somehow ends up with username ≠ email — but with the convention it's belt-and-suspenders.) Pick whatever local-parts you like for demo day; what matters is username == email == Email-attribute. There is **no** seeded demo roster — the app resolves users from token profile claims on first sign-in.

**Then:** go to Step 1's app → **API Authorization → + Authorize resource** and subscribe `orchestrator-mcp-client` to:
- **HR API** — all 5 scopes
- **IT API** — all 3 scopes

*(IS silently strips any requested scope the app isn't subscribed to; without these, the SPA's Reports nav / "My Leaves" panel won't load.)*

---

## Step 5 — The 3 agents

Console → **Agents → + New Agent**, three times: `orchestrator-agent`, `hr-agent`, `it-agent`.

For **each** agent in the wizard:

| Field | Value |
|---|---|
| Description | (free text) |
| Allow users to log in with this agent | **ON** (creates the backing OAuth App + exposes `requested_actor`) |
| AI Agent Type | Interactive Agent |
| Callback URL | `http://localhost:9999/agent-callback` (placeholder; App-Native Auth uses `/oauth2/authn`, never redirects here) |

On **Create**, IS shows the **Agent ID (UUID)** and **Agent Secret** (one-shot — copy the secret immediately). This pair is the `/oauth2/authn` `username` / `password`. The secret can be regenerated later from the agent's Credentials tab.

Then for **each agent's auto-created OAuth app** (Console → Applications → named like `…AGENT-<uuid-prefix>`):

- **Protocol tab:** ☑ Authorization Code, ☑ Refresh Token, **☑ CIBA** ← must be ticked (not always on by default); Token Exchange OFF. In the CIBA section:
  - **CIBA Authentication Request Expiry Time:** `300`
  - **Allowed Notification Delivery Methods:** ☑ **External (Client Application Handles Delivery)** ← MANDATORY (without it, `/oauth2/ciba` won't return `auth_url` and the consent widget never appears). Email / SMS: off.
- **Roles tab:** **Role Audience = Organization** (default is Application — flip it; Application-audience roles don't propagate across the agent chain).
- **Advanced tab:** ☑ App-Native Authentication if the toggle is present (it's auto-enabled on agent OAuth apps in some IS builds — don't worry if absent).
- **User Attributes tab → Subject:** for **`hr-agent` and `it-agent` ONLY** — ☑ *Assign alternate subject identifier*, **Subject attribute = Email**, **Subject type = Public**; ☑ **Email** under attribute selection. **Do NOT do this for `orchestrator-agent`** (leave the default — it only ever mints its own actor token; an agent identity has no email attribute).
- **API Authorization tab:**
  - `hr-agent`'s app → subscribe to **HR API — all 5 scopes** (esp. `hr_assets_write_rest`, easy to miss).
  - `it-agent`'s app → subscribe to **IT API — all 3 scopes**.
  - `orchestrator-agent`'s app → **no** subscriptions.
- Click **Update**.

**Capture 4 values per agent** (Agent ID + Agent Secret from the agent's General/Credentials tab; OAuth Client ID + Client Secret from the auto-created app's Protocol/Info tab):

| Agent | → goes in | Vars |
|---|---|---|
| `orchestrator-agent` | `orchestrator/.env` | `ORCHESTRATOR_AGENT_ID`, `ORCHESTRATOR_AGENT_SECRET`, `ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID`, `ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET` |
| `hr-agent` | `hr_agent/.env` | `HR_AGENT_ID`, `HR_AGENT_SECRET`, `HR_AGENT_OAUTH_CLIENT_ID`, `HR_AGENT_OAUTH_CLIENT_SECRET` |
| `it-agent` | `it_agent/.env` | `IT_AGENT_ID`, `IT_AGENT_SECRET`, `IT_AGENT_OAUTH_CLIENT_ID`, `IT_AGENT_OAUTH_CLIENT_SECRET` |

> **Four-value rule (F-03):** the Agent ID/Secret pair is for Agent-Native Auth (`/oauth2/authn`). The OAuth Client ID/Secret pair is for OAuth/CIBA (`/oauth2/{authorize,token,ciba}`, HTTP Basic). They are **not** interchangeable.

---

## Step 6 — Fill the 5 service `.env` files

Edit the in-repo `.env` files: `orchestrator/.env`, `hr_agent/.env`, `it_agent/.env`, `hr_server/.env`, `it_server/.env`. Put `WSO2_IS_BASE_URL=https://<HOST>:9443` and `IDP_INSECURE_TLS=1` in all five.

### `orchestrator/.env`
```bash
WSO2_IS_BASE_URL=https://<HOST>:9443
WSO2_IS_ISSUER=https://<HOST>:9443/oauth2/token
WSO2_IS_JWKS_URL=https://<HOST>:9443/oauth2/jwks
WSO2_IS_INTROSPECT_URL=https://<HOST>:9443/oauth2/introspect
IDP_INSECURE_TLS=1

# orchestrator-mcp-client (Step 1)
ORCHESTRATOR_MCP_CLIENT_ID=<from Step 1>
ORCHESTRATOR_MCP_CLIENT_SECRET=<from Step 1>
ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI=http://localhost:8090/agent-callback

# orchestrator-agent (Step 5 — 4 values)
ORCHESTRATOR_AGENT_ID=<UUID>
ORCHESTRATOR_AGENT_SECRET=<one-shot>
ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID=<auto-created app client_id>
ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET=<auto-created app client_secret>

# Specialist endpoints (docker-compose service names — underscores!)
HR_AGENT_URL=http://hr_agent:8001
IT_AGENT_URL=http://it_agent:8002

# F-15 collision check (must differ from each other and from the orchestrator agent's)
HR_AGENT_OAUTH_CLIENT_ID=<hr-agent's OAuth App client_id>
IT_AGENT_OAUTH_CLIENT_ID=<it-agent's OAuth App client_id>

# Peer-trust allowlist (specialist agent UUIDs allowed to call back into the orchestrator)
TRUSTED_SPECIALIST_SUBS=<hr-agent UUID>,<it-agent UUID>

# LLM routing (keyword fallback works without a key)
LLM_FALLBACK_MODE=llm
GEMINI_API_KEY=<optional>

ORCHESTRATOR_HOST=0.0.0.0
ORCHESTRATOR_PORT=8080
ALLOWED_ORIGINS=http://localhost:3001,http://127.0.0.1:3001
COOKIE_SECURE=false
```

### `hr_agent/.env`
```bash
WSO2_IS_BASE_URL=https://<HOST>:9443
IDP_INSECURE_TLS=1

HR_AGENT_ID=<UUID>
HR_AGENT_SECRET=<one-shot>
HR_AGENT_OAUTH_CLIENT_ID=<auto-created OAuth App>
HR_AGENT_OAUTH_CLIENT_SECRET=<auto-created OAuth App>

HR_TRUSTED_PEER_AGENTS=<orchestrator-agent UUID>
HR_CIBA_SCOPE=openid hr_self_rest
HR_SERVER_URL=http://hr_server:8000

HR_AGENT_HOST=0.0.0.0
HR_AGENT_PORT=8001
```

### `it_agent/.env`
Same shape with `IT_*` prefixes: `IT_AGENT_ID/SECRET/OAUTH_CLIENT_ID/OAUTH_CLIENT_SECRET`, `IT_TRUSTED_PEER_AGENTS=<orchestrator-agent UUID>`, `IT_CIBA_SCOPE=openid it_assets_read_rest`, `IT_SERVER_URL=http://it_server:8004`, `IT_AGENT_PORT=8002`.

### `hr_server/.env`
```bash
WSO2_IS_BASE_URL=https://<HOST>:9443
IDP_INSECURE_TLS=1

# F-17: aud == the paired agent's OAuth Client ID (NOT an mcp://… URI)
HR_SERVER_EXPECTED_AUD=<hr-agent's OAuth Client ID>
HR_SERVER_TRUSTED_PEER_AGENTS=<hr-agent's Agent UUID>

HR_SERVER_HOST=0.0.0.0
HR_SERVER_PORT=8000
```

### `it_server/.env`
Same shape with `IT_*` prefixes pointing at the it-agent's values.

### Root `.env`
Keep `INTERNAL_REVOKE_SHARED_SECRET=<random hex, same across all services>` and `LOG_LEVEL=DEBUG` (and the `AWS_VM_*` lines if you use the log-download script).

> ⚠️ The in-cluster hostnames above (`hr_agent`, `it_agent`, `hr_server`, `it_server`) must match the service names in `docker-compose.yml`.

---

## Step 7 — Verify, then run

```bash
./scripts/check-is-config.py
```
Expect **all sections PASS** — in particular **Section 4d (Agent App-Native authentication)**, which runs the exact `/oauth2/authorize → /oauth2/authn → /oauth2/token` 3-step flow `ActorTokenProvider` uses at runtime. If 4d is green, sign-in will work. (Section 9 may WARN if you didn't enable the Password grant on `orchestrator-mcp-client` — that's fine.)

```bash
make demo-up           # docker compose up -d --build + healthz smoke
make demo-smoke        # repeat the healthz checks anytime
open http://localhost:3001
make demo-down         # tear down
```

Smoke walk: sign in as `employee@example.com` / `NewsMax@1234` → approve the `orchestrator-agent` consent → in the chat type *"Show me my leave balance and what laptops are available"* → approve two CIBA consent widgets (HR then IT) → you get the leave balance and the asset list.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| SPA: "Sign-in is temporarily unavailable" | `/auth/exchange` returned 502 — the orchestrator couldn't mint the `orchestrator-agent` actor token. Run `check-is-config.py` Section 4d; usually a stale `ORCHESTRATOR_AGENT_SECRET` or a `deployment.toml`/userstore misconfig. |
| `ABA-60003: login.fail` on `/oauth2/authn` | Wrong agent secret (regenerate in Console → Agents → \<agent\> → Credentials, copy the **whole** string — it's the *agent* secret, not the OAuth client secret) **or** a broken `deployment.toml`/AGENT userstore. Agents' `accountLocked` is normally false; if it's true, unlock via SCIM. |
| `unauthorized_client` from `/oauth2/ciba` | CIBA grant not ticked on that agent's OAuth app (Step 5, Protocol tab). |
| Consent widget appears but never resolves | "External" notification channel not enabled on the agent's OAuth app (Step 5, CIBA section). |
| `external notification channel is not supported for federated users` from `/oauth2/ciba` | IS couldn't resolve the `login_hint` (the inbound token's `sub`, an email) to a local user. Confirm the user's IS *username* is their full email; or confirm Multi-Attribute Login is on with `http://wso2.org/claims/emailaddress` allowed; or an agent OAuth app lost its Subject=Email config. |
| Leave applied via chat doesn't show in "My Leaves" / a sidebar widget is empty after a chat action | Per-user `sub` keying desynced — `hr-agent`/`it-agent` lost **Subject = Email** (Step 5, User Attributes → Subject), so token-C carries a UUID while token-A carries the email. Re-apply on both. |
| `Impersonator is not found in subject token` from `/token` | Something is attempting RFC 8693 token-exchange — not used in v4. The orchestrator should be calling `/oauth2/token` with `grant_type=authorization_code` (Pattern C) or polling `grant_type=urn:openid:params:grant-type:ciba`. |
| `aud` mismatch at an MCP server | `*_SERVER_EXPECTED_AUD` must equal the paired agent's OAuth Client ID (not an `mcp://…` URI — F-17). |
| F-15 collision warning at orchestrator boot | `HR_AGENT_OAUTH_CLIENT_ID` and `IT_AGENT_OAUTH_CLIENT_ID` are the same string — each agent has its own auto-created OAuth app; re-capture from Console. |
| Mgmt/SCIM REST calls 401 with an email username | The REST APIs need the real userstore username (`admin`), not an alternative login identifier. `check-is-config.py` defaults to `admin`/`admin`; override with `IS_ADMIN_USER` / `IS_ADMIN_PASS` only if you actually changed the userstore username. |

---

## Endpoint reference (this IS instance)

Base: `https://<HOST>:9443`

| Endpoint | Used for |
|---|---|
| `POST /oauth2/authorize` | App-Native Auth step 1 (form, Basic auth, `response_mode=direct`) **and** Pattern C login redirect |
| `POST /oauth2/authn` | App-Native Auth step 2 (JSON: `flowId` + `selectedAuthenticator{authenticatorId, params{username, password}}`) |
| `POST /oauth2/token` | code exchange (`grant_type=authorization_code`, optional `actor_token` in body), client_credentials, CIBA polling (`grant_type=urn:openid:params:grant-type:ciba`, `auth_req_id`) |
| `POST /oauth2/ciba` | CIBA initiation (form, Basic auth = agent OAuth client; `scope`, `login_hint`, `binding_message`, `actor_token`, `notification_channel=external`) → returns `auth_req_id`, `interval`, `auth_url`, `expires_in` |
| `GET /oauth2/jwks` | token signature verification |
| `POST /oauth2/introspect` | token introspection |
| `POST /oauth2/revoke` | token revocation (logout) |
| BCL receiver: `http://localhost:8123/backchannel-logout` | IS POSTs the logout token here (reached via reverse-SSH tunnel from the VM in dev) |
| `GET /scim2/Users`, `/scim2/Agents`, `/scim2/v2/Roles` | admin Basic-auth; used by `check-is-config.py` |
| `GET /api/server/v1/api-resources`, `/applications`, `/applications/{id}/authorized-apis` | admin Basic-auth; Mgmt API, used by `check-is-config.py` |

---

## See also

- `docs/wso2-is-setup.md` — the original (Sprint 1-era) setup guide; this file supersedes its stale bits.
- `docs/architecture/identity-subject-mismatch.md` — why `orchestrator-mcp-client` + `hr-agent` + `it-agent` must all assert Email as the OIDC subject, and the `username == email` convention.
- `docs/spikes/wso2-is-capability-memo.md` — M0 capability findings (Pattern C, App-Native Auth, CIBA); the F1–F7 reasons for the CIBA-over-RFC-8693 pivot.
- `docs/configuring-ciba-grant-type.md` — WSO2's own CIBA reference (vendored).
- `docs/use-cases/UC-01-user-login.md`, `UC-03-two-specialist-serial-query.md` — the login flow and the demo storyboard.
- `scripts/check-is-config.py` — the post-setup audit (run it last; Section 4d is the agent-auth smoke test).
- `idp_capability_test/README.md` — standalone runnable probes (c0/c1/c4/c8/c10).
