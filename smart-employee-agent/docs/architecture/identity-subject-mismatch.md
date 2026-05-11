# Identity subject mismatch — one user, two `sub` formats across the demo's OAuth apps

**Status:** **RESOLVED (S5.12, 2026-05-11)** — Option A applied (IS-side: all OAuth apps assert `email` as the OIDC subject) + the app-side stopgap map (`user_key` / `_DEMO_USERNAME_TO_UUID`) and all hard-coded demo-user seed data removed.
**First surfaced:** Sprint 5 (S5.9 — CIBA "federated user" error; S5.11 — leave applied via chat missing from "My Leaves")

---

## 1. The problem in one sentence

The same human user used to get a **different OIDC `sub` claim depending on which OAuth app minted the token**, so anything that keyed per-user state by `sub` — or fed `sub` into IS as a `login_hint` — broke when one code path used token-A and another used token-C.

## 2. Why the two formats existed

| Token | Issued by (OAuth app) | `sub` form (before) | Why |
|---|---|---|---|
| **token-A** — the orchestrator's session token (Pattern C) | `orchestrator-mcp-client` | **email-style**: `employee_user@example.com` | The app's **Subject** config was deliberately set to **Email** ("Assign alternate subject identifier" → Email, Subject type = Public). Rationale: when a user federates in via **UAE Pass**, the WSO2-IS-local user-id UUID is not a stable natural key across re-federations; the **email is**. |
| **token-C** — the per-agent CIBA / OBO token | `hr-agent` / `it-agent` / `orchestrator-agent` OAuth apps | **UUID**: `2048ad8c-…` | These apps used the **default** subject (WSO2 `userid`). Nobody changed them. |
| **actor token** — the agent's own I4 token (App-Native Auth as `AGENT/<uuid>`) | same agent apps | the **agent's** identity | becomes `act.sub` in token-C; **must stay the agent's UUID** because the MCP servers gate on `trusted_act_subs` (an allowlist of agent identities). |

Net effect (before): for `employee_user`, `token-A.sub == "employee_user@example.com"` but `token-C.sub == "2048ad8c-…"` — both "correct" per their app config, just not the *same string*.

## 3. The cascade of symptoms this caused

1. **CIBA rejected the request — "external notification channel is not supported for federated users" (HTTP 400).** The agents pass the inbound token's `sub` as the CIBA `login_hint`; with `sub = employee_user@example.com`, IS's `login_hint` resolver reads `user@something` as *"user `employee_user` in tenant `example.com`"*, can't find that tenant, and bails out classifying it as a federated user.
2. **Leave applied via chat never appeared in the "My Leaves" widget.** `hr.apply_leave` (MCP tool) ran under **token-C** → leave stored under key `2048ad8c-…`; `GET /api/me/leaves` (SPA REST proxy) ran under **token-A** → read under key `employee_user@example.com` → found nothing. Same class hit leave **balance** and **cubicle** / **asset** lookups.

## 4. The fix — Option A (IS-side subject alignment)

In the IS Console, on the OAuth apps backing **`orchestrator-mcp-client`** (issuer of token-A), **`hr-agent`**, and **`it-agent`** (issuers of token-C): User Attributes → Subject → ☑ **"Assign alternate subject identifier"**, **Subject attribute = Email**, **Subject type = Public**, no domain/org suffix; tick **Email** under "User Attribute Selection". **All three must agree** — `orchestrator-mcp-client` was *assumed* to already be email-subject (per the original S5.11 diagnosis), but in practice it was emitting the default `userid` UUID, which re-broke the symptom the other way round (token-C = email, token-A = UUID) until this app was flipped too; don't trust the assumption — verify all three. **NOT** `orchestrator-agent` — it never mints a *user* code-exchange/OBO token; it only mints its own actor token, which (see §5) is unaffected.

Result: `token-A.sub == token-C.sub` for every user that has an `emailaddress` attribute (both = the email), and falls back to the user-id UUID **consistently on both sides** for users that don't have one. Either way the in-memory stores key cleanly on `sub` — no canonicalisation shim, no hard-coded user map.

### What changed in code (S5.12)
- Deleted `_DEMO_USERNAME_TO_UUID` + `_DEMO_UUIDS` + `user_key()` from `hr_server/service/store.py` and `it_server/service/store.py`; removed the `store.user_key(...)` calls in `hr_server/service/hr_service.py` (`get_my_leave_balance` / `get_my_leave_requests` / `apply_leave` / `get_my_cubicle`) and `it_server`'s `lookup_user_by_sub` (now `users.get(sub)`).
- **Removed all hard-coded demo-user seed data** (`_SEED_USERS`, `_SEED_CUBICLE_ASSIGNMENTS`, the user-assigned `_SEED_ASSETS` rows). Cubicles start all-vacant; the asset store starts empty. The demo no longer assumes a fixed roster — it runs against whatever IS users sign in. `ensure_user(sub, first_name, last_name, *, username=None, email=None)` now persists the `username`/`email` profile claims (the REST auth path carries them from token-A; OBO/CIBA tool calls — token-C — pass only `sub` and reuse whatever record the REST path created), which is what lets `lookup_employee` and the report username→email joins resolve real users without a seed. Both REST `_authenticate` paths (`hr_server`, `it_server`) now call `ensure_user` with the token's profile claims.
- `common/auth/ciba_client.py::_normalize_login_hint` kept (still needed — see §6) and hardened: only strips `localpart@dns.domain` when the local-part matches `^[A-Za-z0-9._%+\-]+$`; logs at INFO when it normalises; **deliberately does NOT fail-closed on foreign domains** (a live demo user `shammi0107@gmail.com` must be stripped to `shammi0107`).

## 5. The dual-purpose-agent-app caveat — verified safe

The agent OAuth apps mint **both** the user's CIBA/OBO token (token-C) **and** the agent's own actor token (App-Native Auth as `AGENT/<uuid>`), whose `sub` becomes `act.sub` in token-C and must remain the agent's UUID for the MCP servers' `trusted_act_subs` allowlist. Setting Subject → Email on these apps is safe **because WSO2 IS falls back to `userid` when the principal has no `email` attribute** — and an agent identity has none. Verified by spike:

- **`c4`** (App-Native Auth — mints `probe-agent-a`'s own token) after flipping that app's Subject to Email: `sub` still = the agent UUID `e7a5367d-…`. → `act.sub` (= the actor token's `sub`) stays the agent UUID → `trusted_act_subs` / `aud` / `aut` checks unaffected.
- **`c8`** (CIBA OBO for a real user `probe.user`, who *does* have an `emailaddress`) after flipping `probe-agent-b`'s app to Email: token-C came back `{ "sub": "probe.user@example.com", "aut": "APPLICATION_USER", "aud": "<agent's oauth client_id>", "act": { "sub": "<probe-agent-b UUID>" }, "scope": "openid" }`. → `sub` = the user's email ✅, `act.sub` = the agent UUID ✅, `aud` unchanged ✅, no `email` scope needed (`scope=openid` was enough — the alternate-subject toggle alone does it). Independent code review confirmed the MCP validators walk `claims.act` only, never `claims.sub`.

(Not pursued — Spike "G" / a `user_id` access-token claim: app-level user attributes are ID-token-only per the IS Console; getting an extra claim into the *access token* would need a `deployment.toml` change. Not needed, since A works.)

## 6. Known limitation — `login_hint` and the username↔email-local-part assumption

The agents turn the inbound token's `sub` (now an email) into a CIBA `login_hint` by stripping the `@domain` → bare username (`_normalize_login_hint`). WSO2 IS then resolves that as a local username (or accepts a user-id UUID). **This assumes the IS username equals the email's local-part.** Holds for the seeded-style accounts (`employee_user` ↔ `employee_user@example.com`) and for live demo accounts **as long as they're created with `username` = the email local-part** — the operator controls both username and email when creating demo-day accounts (`shammi0107@gmail.com` → username `shammi0107`). If a user's username differs from their email local-part, CIBA for that user fails with the *federated user* 400.

The fully-generic fix (the "Option B" we costed but did not build): give a component the `internal_user_mgt_list` scope and resolve `email → {userName, userid}` via `GET /scim2/Users?filter=emails eq "<email>"` (cached), then feed the UUID to CIBA as `login_hint`. Deliberately out of scope for the POC — a confirmed `/scim2/Me` probe with a plain user token returns `403`, so it requires an IS-side scope grant + an admin-ish credential in a service. Documented here so it's a known follow-up, not a surprise.

## 7. How to diagnose recurrences

When any *per-user data keying* bug appears ("I did X via the agent but the sidebar doesn't show it") **or** a CIBA `login_hint`/"federated user" error appears:
- Decode token-A (the orchestrator session token) and token-C (the OBO token in the MCP server logs) for the same person and compare `sub`. They should be **identical** now. If they differ, an agent OAuth app's "Assign alternate subject identifier → Email" toggle has been lost (re-check `hr-agent` / `it-agent` in the Console), or the user has no `emailaddress` attribute (one falls back to UUID, the other might not — set the attribute).
- For a "federated user" 400 on `/oauth2/ciba`: check the user's IS `username` equals their email's local-part (§6).
- There is no `user_key` shim and no demo-user seed anymore — don't look for one.
