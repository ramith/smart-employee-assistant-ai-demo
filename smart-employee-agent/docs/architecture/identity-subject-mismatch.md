# Identity subject mismatch — one user, two `sub` formats across the demo's OAuth apps

**Status:** open (app-side stopgaps in place; proper IS-side fix not yet applied)
**First surfaced:** Sprint 5 (S5.9 — CIBA "federated user" error; S5.11 — leave applied via chat missing from "My Leaves")
**Owner-facing decision pending:** which generic fix (A / B / C below)

---

## 1. The problem in one sentence

The same human user gets a **different OIDC `sub` claim depending on which OAuth app minted the token**, so anything that keys per-user state by `sub` — or feeds `sub` into IS as a `login_hint` — breaks when one code path used token-A and another used token-C.

## 2. Why the two formats exist (this is intentional, not a bug)

| Token | Issued by (OAuth app) | `sub` form today | Why |
|---|---|---|---|
| **token-A** — the orchestrator's session token (Pattern C) | `orchestrator-mcp-client` | **email-style**: `employee_user@example.com` | The app's **Subject** config was deliberately set to **Email**. Rationale: when a user federates in via **UAE Pass**, the WSO2-IS-local user-id UUID is not a stable natural key across re-federations; the **email is**. So email-as-subject was chosen on purpose for the user-facing app. |
| **token-C** — the per-agent CIBA / OBO token (a.k.a. token-B) | `hr-agent` / `it-agent` / `orchestrator-agent` OAuth apps | **UUID**: `2048ad8c-16a6-4ec1-bb63-b38300118f28` | These apps use the **default** subject (WSO2 `userid`). Nobody changed them. |
| **actor token** — the agent's own I4 token (App-Native Auth as `AGENT/<uuid>`) | same agent apps | the **agent's** identity | becomes `act.sub` in token-C; **must stay the agent's UUID** because the MCP servers gate on `trusted_act_subs` (an allowlist of agent identities). |

Net effect: for `employee_user`, `token-A.sub == "employee_user@example.com"` but `token-C.sub == "2048ad8c-…"`. Both are "correct" per their app config — they're just not the *same string*.

## 3. The cascade of symptoms this caused

1. **CIBA rejects the request — "external notification channel is not supported for federated users" (HTTP 400).**
   The agents pass the inbound token's `sub` as the CIBA `login_hint`. With `sub = employee_user@example.com`, IS's `login_hint` resolver reads `user@something` as *"user `employee_user` in tenant `example.com`"*, can't find that tenant, and bails out classifying it as a federated user. (Verified by direct `curl` to `/oauth2/ciba`: `login_hint=employee_user` → 200 + `auth_req_id`; `login_hint=employee_user@example.com` → 400.)
2. **Leave applied via chat never appears in the "My Leaves" widget.**
   `hr.apply_leave` (MCP tool) runs under **token-C** → leave stored under key `2048ad8c-…`. `GET /api/me/leaves` (SPA REST proxy) runs under **token-A** → reads under key `employee_user@example.com` → finds nothing. Same class of bug hits leave **balance** and **cubicle** lookups.

## 4. App-side stopgaps currently in place (Sprint 5)

These keep the demo coherent **without touching IS**. They are *not* the real fix.

- **S5.9 — `_normalize_login_hint()` in [`common/auth/ciba_client.py`](../../common/auth/ciba_client.py).**
  Before sending the CIBA `login_hint`, if it looks like `localpart@dns.domain` (non-empty local part, a `.` in the domain), send just `localpart`. UUIDs and bare usernames pass through untouched. This is **generic** — it works for any `<userName>@<domain>`, including users added to IS live during the demo. Fixes symptom #1.

- **S5.11 — `store.user_key(sub)` in [`hr_server/service/store.py`](../../hr_server/service/store.py) and [`it_server/service/store.py`](../../it_server/service/store.py).**
  Collapses any known representation of a demo user to one canonical key (the UUID). Used by `get_my_leave_balance` / `get_my_leave_requests` / `apply_leave` / `get_my_cubicle` / `lookup_user_by_sub` so a write under token-C and a read under token-A land on the same record. Fixes symptom #2 — **but it carries a hard-coded `_DEMO_USERNAME_TO_UUID` map** (`employee_user → 2048ad8c-…`, `hr_admin_user → 15fab9e7-…`). That map is the part that is **NOT generic**: a user added to IS during the demo won't be in it, so their cross-token state won't reconcile. The user has explicitly asked for this to be replaced with a generic solution: *"no hard code user names, id, emails… dynamic users… stuff needs to work!"*

## 5. The dual-purpose-agent-app caveat (why "just set everything to Email" isn't free)

The 3 agent OAuth apps (`hr-agent`, `it-agent`, `orchestrator-agent`) are **dual-purpose**:
- they issue the **user's CIBA/OBO token** (token-C — we *want* `sub` = the user, ideally email), AND
- they issue the **agent's own actor token** (App-Native Auth as `AGENT/<uuid>`), whose `sub` becomes `act.sub` in token-C and **must remain the agent's UUID** so the MCP servers' `trusted_act_subs` allowlist matches.

So flipping an agent app's **Subject** to **Email** is safe *only if* WSO2 IS gracefully falls back to `userid` when the principal (an agent) has no `email` attribute. If it doesn't, the actor token's `sub` goes empty/garbage and the MCP `act.sub` trust check fails (`ERR-MCP` / trust error in `docker compose logs hr_server`).

## 6. The three generic-fix options

### Option A — make all OAuth apps use the same OIDC subject (the user's pick)
One setting per app in the IS Console: set **Subject → Email** on `hr-agent`, `it-agent`, `orchestrator-agent` (matching what `orchestrator-mcp-client` already does). Then `token-A.sub == token-C.sub` for every user — including users added live — with **zero code change**, and the S5.11 `user_key` map can be deleted.
- **Risk:** the §5 caveat — might break the agents' actor token.
- **Recommended rollout:** incrementally — flip `hr-agent` first, restart, run one chat turn. If it works → do all three, delete `_DEMO_USERNAME_TO_UUID`. If the actor token breaks → revert that one app and fall back to Option B.
- May also need `email` added to the agents' CIBA scope (so the email claim is actually present to be used as subject).

### Option B — app-side SCIM2 resolution
Give the MCP servers (and/or the REST proxy) IS admin credentials; on each request, resolve whatever `sub` arrived → the canonical SCIM2 user `id` via `GET /scim2/Users?filter=...` (or `userName eq` / `emails eq`), cache it. Replaces the hard-coded map with a live lookup → fully generic, no IS-config change.
- **Cost:** an admin credential in two more services; an IS round-trip (cached) on the hot path.

### Option C — propagate a canonical id end-to-end
Carry the canonical user id explicitly from login → A2A → MCP (a dedicated field, not relying on `sub`). Most invasive: loosens the MCP servers' identity verification (they'd trust a passed-in id rather than deriving it from the token `sub`), biggest refactor.

## 7. How to diagnose recurrences

When any *per-user data keying* bug appears ("I did X via the agent but the sidebar doesn't show it") **or** a CIBA `login_hint`/"federated user" error appears, suspect this mismatch first:
- Compare `token-A.sub` (decode the orchestrator session token) vs `token-C.sub` (decode the OBO token in the MCP server logs) — if they differ in *form* for the same person, it's this.
- The stopgaps live in `common/auth/ciba_client.py::_normalize_login_hint` and `{hr,it}_server/service/store.py::user_key`. The proper fix is consistent OAuth-app **Subject** config in the IS Console.
