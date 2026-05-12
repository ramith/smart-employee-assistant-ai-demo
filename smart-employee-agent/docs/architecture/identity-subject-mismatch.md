# Identity subject mismatch — one user, two `sub` formats across the demo's OAuth apps

**Status:** **RESOLVED** — S5.12 (2026-05-11): IS-side, all OAuth apps assert `email` as the OIDC subject; the app-side stopgap map (`user_key` / `_DEMO_USERNAME_TO_UUID`) and all hard-coded demo-user seed data removed. **S5.18 / S5.18.1 (2026-05-12): the CIBA `login_hint` leg fixed for good** — the `_normalize_login_hint` `@domain`-stripping workaround deleted (agents send the `sub` verbatim), and the operative convention is now **`username == email`** for every IS user (so `username == email == sub == login_hint`, one identifier). See §6.
**First surfaced:** Sprint 5 (S5.9 — CIBA "federated user" error; S5.11 — leave applied via chat missing from "My Leaves"; resurfaced S5.17-era for a real user whose username ≠ email local-part — fixed S5.18/S5.18.1)

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

1. **CIBA rejected the request — "external notification channel is not supported for federated users" (HTTP 400).** The agents pass the inbound token's `sub` as the CIBA `login_hint`; with `sub = employee_user@example.com`, IS's `login_hint` resolver reads `user@something` as *"user `employee_user` in tenant `example.com`"*, can't find that tenant, and bails out classifying it as a federated user. *(S5.12's stopgap was to strip `@domain` → bare username; S5.18 removes that — see §6 — by enabling Multi-Attribute Login so IS resolves the email directly.)*
2. **Leave applied via chat never appeared in the "My Leaves" widget.** `hr.apply_leave` (MCP tool) ran under **token-C** → leave stored under key `2048ad8c-…`; `GET /api/me/leaves` (SPA REST proxy) ran under **token-A** → read under key `employee_user@example.com` → found nothing. Same class hit leave **balance** and **cubicle** / **asset** lookups.

## 4. The fix — Option A (IS-side subject alignment)

In the IS Console, on the OAuth apps backing **`orchestrator-mcp-client`** (issuer of token-A), **`hr-agent`**, and **`it-agent`** (issuers of token-C): User Attributes → Subject → ☑ **"Assign alternate subject identifier"**, **Subject attribute = Email**, **Subject type = Public**, no domain/org suffix; tick **Email** under "User Attribute Selection". **All three must agree** — `orchestrator-mcp-client` was *assumed* to already be email-subject (per the original S5.11 diagnosis), but in practice it was emitting the default `userid` UUID, which re-broke the symptom the other way round (token-C = email, token-A = UUID) until this app was flipped too; don't trust the assumption — verify all three. **NOT** `orchestrator-agent` — it never mints a *user* code-exchange/OBO token; it only mints its own actor token, which (see §5) is unaffected.

Result: `token-A.sub == token-C.sub` for every user that has an `emailaddress` attribute (both = the email), and falls back to the user-id UUID **consistently on both sides** for users that don't have one. Either way the in-memory stores key cleanly on `sub` — no canonicalisation shim, no hard-coded user map.

### What changed in code (S5.12)
- Deleted `_DEMO_USERNAME_TO_UUID` + `_DEMO_UUIDS` + `user_key()` from `hr_server/service/store.py` and `it_server/service/store.py`; removed the `store.user_key(...)` calls in `hr_server/service/hr_service.py` (`get_my_leave_balance` / `get_my_leave_requests` / `apply_leave` / `get_my_cubicle`) and `it_server`'s `lookup_user_by_sub` (now `users.get(sub)`).
- **Removed all hard-coded demo-user seed data** (`_SEED_USERS`, `_SEED_CUBICLE_ASSIGNMENTS`, the user-assigned `_SEED_ASSETS` rows). Cubicles start all-vacant; the asset store starts empty. The demo no longer assumes a fixed roster — it runs against whatever IS users sign in. `ensure_user(sub, first_name, last_name, *, username=None, email=None)` now persists the `username`/`email` profile claims (the REST auth path carries them from token-A; OBO/CIBA tool calls — token-C — pass only `sub` and reuse whatever record the REST path created), which is what lets `lookup_employee` and the report username→email joins resolve real users without a seed. Both REST `_authenticate` paths (`hr_server`, `it_server`) now call `ensure_user` with the token's profile claims.
- `common/auth/ciba_client.py::_normalize_login_hint` was kept as a stopgap (only strips `localpart@dns.domain` when the local-part matches `^[A-Za-z0-9._%+\-]+$`). **Removed in S5.18** — see §6: the agents now send the `sub` to CIBA verbatim.

## 5. The dual-purpose-agent-app caveat — verified safe

The agent OAuth apps mint **both** the user's CIBA/OBO token (token-C) **and** the agent's own actor token (App-Native Auth as `AGENT/<uuid>`), whose `sub` becomes `act.sub` in token-C and must remain the agent's UUID for the MCP servers' `trusted_act_subs` allowlist. Setting Subject → Email on these apps is safe **because WSO2 IS falls back to `userid` when the principal has no `email` attribute** — and an agent identity has none. Verified by spike:

- **`c4`** (App-Native Auth — mints `probe-agent-a`'s own token) after flipping that app's Subject to Email: `sub` still = the agent UUID `e7a5367d-…`. → `act.sub` (= the actor token's `sub`) stays the agent UUID → `trusted_act_subs` / `aud` / `aut` checks unaffected.
- **`c8`** (CIBA OBO for a real user `probe.user`, who *does* have an `emailaddress`) after flipping `probe-agent-b`'s app to Email: token-C came back `{ "sub": "probe.user@example.com", "aut": "APPLICATION_USER", "aud": "<agent's oauth client_id>", "act": { "sub": "<probe-agent-b UUID>" }, "scope": "openid" }`. → `sub` = the user's email ✅, `act.sub` = the agent UUID ✅, `aud` unchanged ✅, no `email` scope needed (`scope=openid` was enough — the alternate-subject toggle alone does it). Independent code review confirmed the MCP validators walk `claims.act` only, never `claims.sub`.

(Not pursued — Spike "G" / a `user_id` access-token claim: app-level user attributes are ID-token-only per the IS Console; getting an extra claim into the *access token* would need a `deployment.toml` change. Not needed, since A works.)

## 6. The `login_hint` leg — RESOLVED (S5.18 / S5.18.1)

**Was a known limitation (S5.12 → S5.17):** the agents turned the inbound token's `sub` (an email) into a CIBA `login_hint` by stripping `@domain` → bare username (`_normalize_login_hint`), which assumed *IS username == email local-part*. That broke for a real user whose username differed (`Nesaratnam`, email `sivanoly@wso2.com`) → IS resolver missed → "external notification channel is not supported for federated users" 400.

**Fix:**
- **Code side (S5.18):** `common/auth/ciba_client.py` — `_normalize_login_hint` (and its regex / the `re` import) deleted; `initiate()` now POSTs `login_hint = <the inbound token's sub>` **unchanged** — no `@domain` mangling. (Also: a composer-prompt rule in `orchestrator/llm/prompts.py` forbidding the LLM from quoting raw error text/JSON/IdP wording — it had been leaking the verbatim "external notification channel" string into the chat.)
- **IS side (S5.18.1) — the operative fix:** create every IS user with **`username == their email address`** (and the same value as the Email attribute). Then `username == email == sub == login_hint` — one identifier, and the email-form `login_hint` resolves directly as a plain local username via `isExistingUser`. This is what's in `docs/wso2-is-setup.md` §5.5. (A user created *without* an email attribute gets `sub` = the `userid` UUID, which IS also resolves as a `login_hint` — still works, just not the recommended shape.)
- **Multi-Attribute Login (the S5.18 intermediate attempt):** enabling MAL for the `emailaddress` claim (Console → Login & Registration → Alternative Login Identifiers) *also* makes an email `login_hint` resolve — IS's `DefaultCibaUserResolver` consults the multi-attribute service before `isExistingUser`. In practice the `username == email` convention proved simpler and more robust for the demo, so MAL is **optional**; with email-form usernames it's a no-op. Leave it on or off.

*Rejected alternatives (researched S5.18):* `id_token_hint` on `/oauth2/ciba` — IS accepts it but just extracts `sub` and treats it as the `login_hint`, so with our email-form `sub` it's the identical failure. `login_hint_token` — hard-unsupported by IS. SCIM2 `email → {userName, userid}` lookup (the old "Option B") — works, but needs a privileged m2m credential with `internal_user_mgt_list` + a cache + ~5 files of code; not worth it once `username == email` is the convention. Custom `CibaUserResolver` OSGi bundle — too much maintenance/version risk.

## 7. How to diagnose recurrences

When any *per-user data keying* bug appears ("I did X via the agent but the sidebar doesn't show it") **or** a CIBA `login_hint`/"federated user" error appears:
- Decode token-A (the orchestrator session token) and token-C (the OBO token in the MCP server logs) for the same person and compare `sub`. They should be **identical** now. If they differ, an agent OAuth app's "Assign alternate subject identifier → Email" toggle has been lost (re-check `hr-agent` / `it-agent` in the Console), or the user has no `emailaddress` attribute (one falls back to UUID, the other might not — set the attribute).
- For a "federated user" 400 on `/oauth2/ciba` (`external notification channel is not supported for federated users`): check that **Multi-Attribute Login** is still enabled on the IS tenant with `http://wso2.org/claims/emailaddress` in the allowed list (§6), and that the user actually has an `emailaddress` attribute. (Pre-S5.18 this was a `username == email-local-part` mismatch; that coupling no longer exists.)
- There is no `user_key` shim, no demo-user seed, and no `_normalize_login_hint` anymore — don't look for one.
