# IS audit-log analysis (2026-05-09) — F-19 may need re-test

**Source:** `is_server_logs/wso2is-7.3.0/repository/logs/audit.log` (downloaded from the AWS IS VM, 2026-05-09).
**Analyst:** post-3A.0 spike review.
**Scope:** look for evidence that contradicts or sharpens F-19, F-20, F-21.

This is a research note, not a design change. Recommendations at §5.

---

## §1. Headline finding (potential)

**F-19 may have been a probe artifact, not a fundamental WSO2 IS limitation.**

The F-19 narrative said *"WSO2 IS 7.2 does NOT register CIBA-issued tokens as user-session participants and therefore does NOT fire OIDC Back-Channel Logout."*

The audit log tells a different story.

### Observation 1 — CIBA flows DO update the user session

For `hr_admin_user` (sub `15fab9e7…`, the F-19 spike subject):

| Time | Action | sessionContextId | ServiceProviderName |
|---|---|---|---|
| 05:45:48 | StoreSession (Pattern C login completed) | `2f8fd259…` | `orchestrator-mcp-client` |
| 05:46:03 | UpdateSession (CIBA flow for hr-agent) | **`2f8fd259…` (same!)** | `hr-agent-51de717a-…` |
| 05:47:13 | Logout | (ContextIdentifier `643c3d45…`) | **`null`** |

**The CIBA flow for hr-agent updated the SAME `sessionContextId` as the orchestrator-mcp-client login.** This means CIBA grants ARE session participants on the IS side. The spike inferred the opposite from "Active Sessions: empty" in the IS Console UI, but the audit log clearly shows session activity.

The same pattern held for `employee_user` (sub `2048ad8c…`, today's C13/C14 subject) — sessionContextId `9316bb8d…` was created at 07:20:03 (Pattern C login) and then UpdateSession fired four times across orchestrator-mcp-client and hr-agent claim fetches.

### Observation 2 — The F-19 Logout had no Service Provider context

The Logout audit entry at 05:47:13:

```
Initiator   : 15fab9e7-…
Action      : Logout
ContextIdentifier : 643c3d45-9ee0-4fc1-8fb6-fe74ca60fb18  ← different from sessionContextId 2f8fd259
LoggedOutUser : 15fab9e7-…
ServiceProviderName : null
RelyingParty : null
RequestType : oidc
```

`ServiceProviderName` and `RelyingParty` are both **null**. Per WSO2 IS, these fields are populated only when /oidc/logout receives a valid `id_token_hint` (or, less reliably, `client_id`) — IS uses one of those to resolve which Service Provider the user is signing out of. Without one, IS does what we observed: clears the local cookie, emits a Logout audit row with null SP fields, and **does not walk the session-participants table to fan out BCL** (because there's no SP context to fan from).

The C12 spike `/oidc/logout` call we ran was:

```
https://13.60.190.47:9443/oidc/logout?post_logout_redirect_uri=http://localhost:8090/
```

— **no `id_token_hint`, no `client_id`**. That is exactly the pattern that produces "ServiceProviderName: null" in IS's Logout audit row.

### Plausible reinterpretation of F-19

The empirical evidence (zero BCL POSTs captured, audit log says ServiceProviderName=null) is **consistent with** *both*:

- **F-19 as written** — "CIBA grants aren't session participants, so no BCL ever."
- **The probe-artifact alternative** — "Our /oidc/logout call had no id_token_hint, so IS didn't know which SP the user was signing out of, so no BCL fan-out for any participant — including the orchestrator app, which IS Pattern C–authenticated."

The audit log's *UpdateSession during CIBA* observation favours the probe-artifact alternative. CIBA grants were on the user-session table (sessionContextId `2f8fd259…`) — but our Logout request didn't reference that context.

**The spike never called /oidc/logout with `id_token_hint` set to the orchestrator-mcp-client's id_token. That is the missing test.**

---

## §2. F-20 and F-21 — unaffected

F-20 (auth_req_id revoke is no-op) and F-21 (token-A revoke doesn't propagate to OBO token-B introspection) are **not affected** by this finding:

- F-20 tested `/oauth2/revoke` directly (no `/oidc/logout` involved). No SP-resolution dependency.
- F-21 tested `/oauth2/introspect` of token-B after `/oauth2/revoke` of token-A. Same — no logout endpoint involved.

The audit log does NOT include `/oauth2/revoke` or `/oauth2/introspect` events (these are not audited by default in IS). So F-20 and F-21 cannot be cross-checked from this log file. They stand on their direct-probe evidence.

---

## §3. Version note

The path says `wso2is-7.3.0` but the deployed binary is a **WSO2 IS release candidate** (the 7.3.0 in the install path is a packaging artifact, per operator note 2026-05-09). All F-19/F-20/F-21 evidence comes from the same RC build; treat as one consistent target. No version-related confounder.

---

## §4. Architectural implications IF F-19 turns out to be a probe artifact

If a re-test of /oidc/logout WITH `id_token_hint` shows BCL **does** fire to registered SPs (orchestrator-mcp-client and possibly hr-agent / it-agent if they're listed as session participants), the architectural picture changes materially:

| Concern | Today's Sprint 3 design (Option A) | Possible upgrade (Option C) |
|---|---|---|
| User-driven sign-out | orchestrator-driven cache-bust to 4 receivers | Same, plus IS-driven BCL to orchestrator + agent apps |
| Admin-terminate (D3.2) | orchestrator BCL receiver fans out internally | Each agent could ALSO receive BCL directly (defense-in-depth) |
| Half fan-out failure | SECURITY-DEGRADED until token TTL | If IS BCL fires to agents, native IS BCL is the backstop |
| Demo narrative | Gateway pattern is **required** | Gateway pattern is **preferred for latency**; OIDC BCL is the fallback |

**This does NOT invalidate the locked Option A design.** Option A still works. But it potentially:
1. Restores Option C (hybrid) as a viable defense-in-depth layer.
2. Softens the "SECURITY-DEGRADED" labels in tech-arch §5 — the introspection backstop might become "BCL backstop" which is more reliable.
3. Strengthens the demo: *"We use the gateway pattern for sub-second propagation; OIDC BCL is our spec-compliant backstop."*

**It does NOT affect F-21** — token-A revoke still doesn't kill OBO tokens at IS. So the introspection backstop story for `/oauth2/revoke` paths remains broken regardless.

---

## §5. Recommendations

1. **Re-run the F-19 spike with proper `id_token_hint`.** Steps:
   - Capture the orchestrator-mcp-client's `id_token` after Pattern C login (we already have a recipe — same one used for C13).
   - Bring up the C12 reverse-SSH BCL listener rig.
   - Hit `https://13.60.190.47:9443/oidc/logout?id_token_hint=<id_token>&post_logout_redirect_uri=http://localhost:8090/&client_id=<orchestrator-mcp-client-id>`.
   - Watch the BCL listener for incoming POSTs.
   - Verdict matrix:
     - 1+ POST captured → **F-19 was a probe artifact**. Document as F-19 *correction* in `sprint-1-fixes.md`. Reopen Option C as 3B.1 defense-in-depth.
     - 0 POSTs captured → F-19 stands. Current design unchanged.

2. **No code/design changes today.** F-21 still FAILs; SECURITY-DEGRADED labels still apply for the orchestrator-revoke-mid-fan-out failure modes. F-19 only governs the admin-terminate (D3.2) path; we can ship 3A on Option A regardless and revisit D3.2 for 3B.1.

3. **Add the F-19 re-test as a 3B.1 prerequisite.** It costs ~15 minutes (same C12 rig) and could change the entire D3.2 design. Cheaper to do up-front.

---

## §6. Raw evidence

```
# hr_admin_user (sub 15fab9e7-...) — F-19 spike subject
05:45:48,594  StoreSession   sessionContextId=2f8fd259...  SP=orchestrator-mcp-client
05:46:03,617  UpdateSession  sessionContextId=2f8fd259...  SP=hr-agent-51de717a-...   ← CIBA flow updates the SAME session
05:47:13,097  Logout         ContextIdentifier=643c3d45     SP=null  RelyingParty=null  ← but Logout has no SP

# employee_user (sub 2048ad8c-...) — C13/C14 subject
07:20:03,148  StoreSession   sessionContextId=9316bb8d...  SP=orchestrator-mcp-client
07:26:24,821  UpdateSession  sessionContextId=9316bb8d...  SP=hr-agent-51de717a-...
07:34:08,760  UpdateSession  sessionContextId=9316bb8d...
07:34:39,383  UpdateSession  sessionContextId=9316bb8d...
07:34:55,283  UpdateSession  sessionContextId=9316bb8d...  SP=hr-agent-51de717a-...
                                          ← no Logout (we never logged out today)

# wso2carbon.log corroboration — CIBA validation events match the UpdateSession timestamps
05:46:03,621  CIBA validate auth_req_id=d27e3ccb... user=15fab9e7
07:26:24,823  CIBA validate auth_req_id=93013888... user=2048ad8c
07:34:55,287  CIBA validate auth_req_id=d29df481... user=2048ad8c
```

No `/oauth2/revoke` or `/oauth2/introspect` events appear in the audit log — IS does not audit these by default.
