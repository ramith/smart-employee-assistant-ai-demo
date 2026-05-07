# IdP Capability Tests — WSO2 IS 7.2.0

**Purpose:** validate that the fundamental OAuth/OIDC capabilities the
smart-employee-agent POC depends on are actually supported and enabled
on this WSO2 IS install — **before** investing time in the full topology
(SPA + MCP Client App + 3 agents + 5 API resources + 2 roles).

**Why this folder is separate from `scripts/probes/`:** those probes
exercise the full POC topology against the production-shaped config.
These tests prove the IdP itself can do what we need with the absolute
minimum of artifacts.

---

## Order of operations

1. **Set up the foundation substrate** in the IS Console (~10 min, see §1
   below).
2. **Copy `.substrate.env.example` → `.substrate.env`** and fill in the
   four values that came out of step 1.
3. **Run the tests in order** — stop on first FAIL and triage before
   proceeding.

| # | Type | What it proves | Stop-on-fail action |
|---|---|---|---|
| C0 | curl | IdP reachable + JWKS readable | check IS is actually running |
| C1 | UI check | Token Exchange grant checkbox is exposed | escalate to WSO2 expert |
| C2 | curl | Basic RFC 8693 produces depth-1 `act.sub` | the SaaS-blocker test — fixing this fixes the architecture |
| C3 | curl | RFC 8693 chained twice → depth-2 nested `act.act.sub` | architecturally significant; depth-1 only forces drop-middleware decision |
| C5 | curl | RFC 8707 multi-`resource` produces multi-audience token | optional optimization |
| C6 | UI check | "Agents" left-nav present (7.1+ feature) | the version may be too old |
| C7 | UI check | "MCP Server" registration type present in API Resources | falls back to plain API Resource |
| C8 | curl | `/oauth2/authn` 3-step App-Native Auth flow works | falls back to client_credentials for agents |

(C4 was rolled into C2's diagnostic — we test if Trusted Token Issuer
gating exists by testing C2 *without* it first.)

---

## §1 — Foundation substrate setup (do this once)

In `https://13.60.190.47:9443/console`:

### 1.1 — Test user

Console → **Users** → **+ New User**
- Username: `probe.user`
- Password: pick something memorable; capture into `.substrate.env`
- Skip: groups, roles (not needed for capability tests)

### 1.2 — Two confidential apps (`probe-client-a`, `probe-client-b`)

For C3 (nested `act`) we need **two** distinct actor identities. Repeat
this twice with names `probe-client-a` and `probe-client-b`.

Console → **Applications** → **+ New Application** → **Standard-Based
Application** (or "Web Application" — whichever offers full grant
configuration).

| Field | Value |
|---|---|
| Application name | `probe-client-a` (then `-b`) |
| Authorized redirect URLs | `http://localhost:9999/callback` (placeholder) |

After creating, click into the app:

**Protocol tab:**
- ☑ Authorization Code
- ☑ Client Credentials
- ☑ **Password (Resource Owner Password Credentials)** — needed for C2
  to mint a user token without a browser dance
- ☑ Refresh Token
- ☑ **Token Exchange (`urn:ietf:params:oauth:grant-type:token-exchange`)**
  — **the headline checkbox; if it's missing here, C1 FAILS, stop**
- Access Token: JWT, lifetime 3600s

Capture **Client ID** + **Client Secret** into `.substrate.env`.

### 1.3 — One API Resource

Console → **API Resources** → **+ New API Resource**
- Identifier: `urn:probe:api`
- Display name: Probe API
- Scopes: `probe.read`, `probe.write`

### 1.4 — Subscribe both apps

For each of `probe-client-a` and `probe-client-b`:
- Console → Applications → app → **API Authorization** (or "Subscribed
  APIs") → Subscribe to **Probe API** → grant both scopes

### 1.5 — Done

If any of the steps above is impossible because the menu/option doesn't
exist, that's already a finding worth surfacing — note it down before
continuing.

---

## §2 — Running the tests

Tests are written in **Python** for readability and easy debugging
(`breakpoint()` works inside any script).

```bash
cd idp_capability_test

# One-time: virtualenv + deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# One-time: fill substrate values
cp .substrate.env.example .substrate.env
$EDITOR .substrate.env   # paste the IDs/secrets/password from §1

# Run individually (recommended for debugging)
python c0_reachability.py
python c2_basic_token_exchange.py
python c3_nested_act.py
python c5_multi_resource.py

# Or run all of them sequentially (stops on first FAIL)
python run_all.py
```

Every HTTP request prints what it sent and what came back (request body,
auth used, response JSON), so when something fails you can see exactly
which `/oauth2/token` call rejected what.

UI-check tests (`C1`, `C6`, `C7`) are documented in this README — no scripts.

To debug a specific call interactively, drop a `breakpoint()` into the
test and re-run it.

---

## §3 — UI checks (no scripts)

### C1 — Token Exchange grant exposure

**Where:** Console → Applications → `probe-client-a` → Protocol tab → grant types section.

**Pass:** the grant `urn:ietf:params:oauth:grant-type:token-exchange`
appears as a checkbox you can tick **without** any prerequisite
configuration (no "register a Trusted Token Issuer first" dialog, no
greyed-out checkbox).

**Fail:**
- Checkbox missing → product gap; abort migration; talk to WSO2 expert.
- Checkbox visible but greyed-out → click it and read the tooltip; that
  tells you what prerequisite is required. Document it.

### C6 — "Agents" left-nav menu

**Where:** Console → left navigation pane.

**Pass:** "Agents" is one of the menu items. WSO2 IS 7.1+ ships this.

**Fail:** menu absent. Either the version is older than 7.1, or the
agentic-AI feature pack isn't enabled. Check IS version under "?" or
About. If 7.2.0 doesn't show this, escalate.

### C7 — MCP Server registration type

**Where:** Console → API Resources → **+ New** button.

**Pass:** the dropdown/dialog offers a choice between **API Resource**
and **MCP Server** (or shows MCP Server as a separate entry in the menu).

**Fail:** only generic API Resource is available. Architectural fallback:
register backends as plain API Resources with `mcp://` audiences — works,
but loses semantic richness in audit/UX.

---

## §4 — What "PASS" of all the above means for the architecture

- **C0+C1+C2+C3 PASS** → full v3 architecture works as designed; proceed
  with `docs/wso2-is-setup.md` Steps 1–5.
- **C0+C1+C2 PASS, C3 FAIL** → depth-2 nested `act` not supported; either
  drop the specialist middleware tier (orchestrator calls hr_server/it_server
  directly) or accept depth-1 `act` audit story.
- **C2 FAIL with TTI hint** → register a self-trust Trusted Token Issuer
  in the IS Console (Identity Providers menu), retry C2. If still fails,
  same blocker as Asgardeo SaaS — that would be a real surprise.
- **C1 FAIL** → product gap. Flag to WSO2 expert. Migration was wasted.

The capability tests should take less than 30 minutes once substrate is up.
That's the budget guarding against another 3-day setup-then-discover-blocker.
