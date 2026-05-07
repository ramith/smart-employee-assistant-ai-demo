# WSO2 IS 7.2.0 — Capability Spike Memo (M0 sign-off)

**Date:** 2026-05-07
**Author:** Ramith Jayasinghe
**Status:** **Spike complete.** Architecture pivoted from RFC 8693 chained delegation to per-agent CIBA. Milestone M0 ready for sign-off.
**Audience:** Product council (PM, BA, Security Engineer, UX Researcher, Python Pro, Tech Writer) reviewing findings before committing to Sprint 1/2 build.

---

## TL;DR

We ran a focused capability spike against on-prem WSO2 Identity Server 7.2.0 to verify the OAuth/OIDC primitives the v3 architecture depends on, **before** committing to a full topology rebuild. Three of four core ingredients pass cleanly. One — the **depth-2 nested `act` chain via RFC 8693 token-exchange** — is **not yet supported on IS 7.2** (confirmed directly with the WSO2 expert). The expert proposed a **CIBA-based workaround** that produces depth-1 OBO tokens per specialist agent, with the user explicitly consenting at runtime to each agent's authorization. We validated the workaround end-to-end with a live probe.

**Architectural decision:** v3's RFC 8693 chained delegation is replaced by **per-agent CIBA**. Each specialist (hr_agent, it_agent) runs its own CIBA flow when invoked; the user sees an Approve/Deny widget for each. Each agent receives an OBO token of shape `{sub: <user>, act: {sub: <this-agent>}}`. Audit becomes a sequence of depth-1 events rather than a nested ladder.

---

## 1. Why we ran a capability spike

We previously spent three days configuring the full Asgardeo SaaS topology (3 agents, 5 API resources, 2 roles, 4 subscriptions) only to discover at the very end that the `urn:ietf:params:oauth:grant-type:token-exchange` grant is not exposed on the SaaS UI. That blocked the architectural cornerstone (depth-2 nested `act`) and forced an IdP migration to on-prem WSO2 IS.

To avoid a repeat, we built `idp_capability_test/` — a Python harness that validates each fundamental OAuth/OIDC ingredient in isolation, against the absolute minimum of artifacts. The intent: **prove every primitive works before investing in the full topology rebuild**.

Total spike duration: ~6 hours including IdP install, console config, capability tests, and expert consultation.

---

## 2. Substrate

Created in IS Console (`https://13.60.190.47:9443/console`, default tenant `carbon.super`):

| Artifact | Type | Purpose |
|---|---|---|
| `probe.user` | Local user | End-user whose identity is delegated. Password: in `.substrate.env` (gitignored). |
| `probe-client-a` | Standard-Based App (OIDC) | Front-door confidential client. Code + Client Credentials + Password + Refresh + **Token Exchange** grants enabled. |
| `probe-client-b` | Standard-Based App (OIDC) | Identical to `-a`; second TX authenticator. |
| `probe-agent-a` | **Agent** (Interactive, login-allowed) | Stand-in for `orchestrator-agent`. Auto-creates an OAuth Application backing it. |
| `probe-agent-b` | **Agent** (Interactive, login-allowed) | Stand-in for `hr_agent` / `it_agent`. |
| `Probe API` (`urn:probe:api`) | API Resource | Trivial scoped resource (`probe.read`, `probe.write`); all 4 apps subscribed. |

Every probe in `idp_capability_test/c*.py` is a self-contained Python script with verbose request/response logging, runnable via `python cN_*.py`. Substrate creds live in `.substrate.env` (gitignored).

---

## 3. Test matrix and verdicts

| # | Ingredient | Probe | Verdict | Evidence |
|---|---|---|---|---|
| C0 | Reachability + JWKS + creds | `c0_reachability.py` | ✅ PASS | JWKS returns 1 key; client_credentials grant succeeds for probe-client-a. |
| **I1** | **Pattern C → depth-1 `act`** | `c1_pattern_c.py` | ✅ **PASS** | Token: `sub=probe.user, aut=APPLICATION_USER, act.sub=probe-agent-a`. Exactly matches WSO2 expert's "Audit with delegation chains" slide. |
| **I4** | **App-Native Auth `/oauth2/authn`** | `c4_app_native_authn.py` | ✅ **PASS** | Agent self-auth via 3-step direct flow. Token: `sub=<agent-id>, aut=AGENT, scope=openid internal_login`. |
| I2 | RFC 8693 → depth-2 nested act | `c3_nested_act.py` (orig); now `c2a_pure_tx_negative.py` | ❌ **BLOCKED** | `HTTP 400 invalid_request: "Impersonator is not found in subject token."` Reproduced with both vanilla user token and Pattern C output as `subject_token`. Confirmed by expert as not-yet-supported in 7.2. |
| **I8** | **Per-agent CIBA → depth-1 OBO** | `c8_ciba.py` | ✅ **PASS** | Token: `sub=probe.user, aut=APPLICATION_USER, act.sub=probe-agent-b`. Polling completed in 8 × 2s = 16s. External notification channel returned `auth_url`. User consent worked end-to-end. **This is the workaround that unblocks the architecture.** |
| I3, I5, I6, I7 | Multi-resource, scope narrowing, introspection | — | DEFERRED | Not on the critical path of the CIBA architecture. Will validate during Sprint 1/2 build. |

---

## 4. Findings ledger

| F# | Finding | Implication |
|---|---|---|
| **F1** | RFC 8693 with a vanilla user `subject_token` (no `act`) is rejected with `"Impersonator is not found in subject token"`. | TX is not for initiating delegation in IS 7.2. |
| **F2** | `urn:ietf:params:oauth:grant-type:token-exchange` is **not** auto-enabled on Standard-Based / MCP Client Apps. Must be ticked manually on the Protocol tab. | Setup-guide checklist item; document explicitly. |
| **F3** | Auto-created Agent Apps' Protocol tab exposes only `Refresh Token`, `Code`, **`CIBA`** (not `Token Exchange`). | Agents perform CIBA themselves — no separate confidential client per agent needed. The two-app pattern is only for the orchestrator's MCP-Client app (front-door Pattern C). |
| **F4** | WSO2 IS 7.2's TX implementation expects an **`impersonator` claim** in `subject_token`. Pattern C populates `act` but not `impersonator`. The two are different concepts. **Depth-2 nested `act` is not supported in this IS version.** Confirmed by the WSO2 expert. | Architectural cornerstone of v3 plan is invalid; must replace. |
| **F5** | CIBA grant produces a **depth-1 OBO token** of shape `{sub: <user>, act: {sub: <agent>}}` directly, asynchronously, with user-in-the-loop consent. `actor_token` is provided on the initiating `/oauth2/ciba` call only, **not** on the polling `/oauth2/token`. The `auth_req_id` carries the actor binding forward. | This is the working substitute for v3's chained TX. Adopted as the new architecture. |
| **F6** | **Multi-audience CIBA is NOT supported on IS 7.2.** Passing multiple `resource=` parameters on `/oauth2/ciba` is silently ignored. The issued token's `aud` defaults to the calling OAuth Client ID, not to any requested URN. RFC 8707 binding does not apply on this CIBA path. | MCP servers must validate `aud == <agent's OAuth Client ID>` rather than `aud == mcp://hr_server.local`. Each specialist's CIBA produces one token bound to itself; sufficient for the architecture since each specialist talks only to its own MCP backend. |
| **F7** | **CIBA + `offline_access` scope DOES issue a refresh_token** on IS 7.2 (default 1hr TTL on the access token; refresh enables silent renewal). Empirically confirmed in c9 probe. | Per user/expert decision, NOT used in the demo (architecture intentionally bounds session = access-token TTL). Documented as an available option for future production-hardening or longer-running tasks. |

---

## 5. Architectural pivot

### What's out

- **RFC 8693 token-exchange between hops** as the re-mint mechanism for user identity propagation across orchestrator → specialist → MCP server.
- **Depth-2 nested `act` claim** as the audit ladder.
- The "single multi-audience token threaded through all hops" alternative (Alt A from earlier discussion) — also abandoned, since CIBA produces narrowly-scoped per-agent tokens cleanly.

### What's in

```
1. SPA login (Pattern C, validated by I1)
   token-A: sub=user, act.sub=orchestrator-agent

2. GUI → orchestrator (carries token-A as Bearer)

3. Orchestrator → specialist (A2A leg, forwards token-A or its sub claim)
   Specialist extracts user identity from token-A.sub.

4. Specialist → IS: POST /oauth2/ciba
   (authenticated as the specialist's auto-created Agent App — F3 says
    Agent Apps support CIBA. login_hint=user.sub, actor_token=specialist-
    agent's token from I4, notification_channel=external)
   IS responds: { auth_req_id, auth_url, interval, expires_in }

5. Specialist returns auth_url to orchestrator → orchestrator pushes to GUI.

6. GUI shows Approve/Deny widget with binding_message.
   User clicks Approve → IS consent screen → consents.

7. Specialist polls IS: POST /oauth2/token
   (grant_type=urn:openid:params:grant-type:ciba, auth_req_id, NO actor_token)
   IS returns token-B: sub=user, act.sub=specialist-agent

8. Specialist uses token-B to call its MCP server.

9. (IT flow is structurally identical and parallel.)
```

### What carries over from v3

- Pattern C with `requested_actor` for the SPA → orchestrator login.
- App-Native Auth (`/oauth2/authn`) for agents to mint their own actor tokens.
- The two-app pattern for the orchestrator (SPA + MCP Client App) — the front-door client still needs Standard-Based / MCP Client App because Agent Apps lack TX (and would need to drive the redirect Pattern C, not CIBA).
- `_a2a` / `_mcp` scope tier split.
- Per-MCP-server audience targeting (`mcp://hr_server.local`, `mcp://it_server.local`).
- All findings about hostname / cert / TLS handling on the on-prem VM.

---

## 6. UX, latency, and audit consequences

### UX
- **User consents per agent invocation** (not once at login). For a single user request that fans out to HR + IT, the user sees two consecutive Approve widgets.
- The expert confirmed the legacy demo already has the consent widget pattern — reuse it.
- Consent UI on the IS side is single Approve/Deny → IS consent page → confirm. Not the verbose scope-list page.
- Token is issued ONCE per consent. Lives until access_token TTL (default 3600s) expires. **No refresh-token extension** — when it expires, next invocation triggers a new CIBA round-trip.

### Latency budget
- CIBA initiation: ~100–300 ms (network round-trip)
- User click + consent screen: highly variable (5s to 30s+ depending on attention)
- Polling: every 2s by default, until consent lands. ~2s minimum overhead beyond user click.
- **Net per specialist invocation: 5–30s of "loading…" UX from user's perspective**, dominated by their own click time. Tighten the GUI consent widget to reduce this.

### Audit
- No nested `act` chain. Each specialist's token captures only the immediate `user → this-agent` delegation.
- The orchestrator's role is NOT in the specialist's token. To reconstruct the full chain `user → orchestrator → hr_agent → hr-mcp`, audit code must correlate:
  - The orchestrator's session log (who called what, when)
  - The CIBA event log in IS (each agent's `auth_req_id`, user consent, issuance)
  - The MCP server's access log (token jti, sub, act.sub)
- Practically: design a **correlation ID** (e.g., `request_id` propagated as a custom header through every A2A and MCP call) so we can `JOIN` the three log streams. Not as elegant as nested `act` but sufficient for compliance.

---

## 7. Risks and open questions for council review

1. **UX cost of per-agent consent.** For demo flows this is acceptable (one or two specialists per user request). For production agentic workflows that fan out widely, it would degrade — needs a future "blanket consent" or "trusted agent set" concept that doesn't yet exist on the IS roadmap.

2. **Token TTL coordination.** Default access_token = 3600s, CIBA `expires_in` = 300s, refresh disabled. If a long-running agent task exceeds the TTL, what's the recovery? Re-CIBA? Cache the work-in-progress? This needs design.

3. **Audit story without nested `act`.** Is correlation-via-logs sufficient for the compliance posture we're claiming? Or does the security-engineer think we need stronger cryptographic binding between the hops? This is exactly the kind of question the council should adjudicate.

4. **Orchestrator → specialist A2A authentication.** Confirmed by expert that the orchestrator forwards its OBO token (`sub=user, act.sub=orchestrator-agent`) and the specialist extracts `sub` from it. But: how does the specialist *trust* the inbound token? It must validate `iss`, `act.sub`, signature via JWKS, and confirm `act.sub` is in an allowlist of trusted upstream agents. This is the existing `common/auth/peer_trust.py` pattern; verify it still applies.

5. **Resource scopes per CIBA.** Ingredient I3 (multi-resource `aud`) is unverified. We don't know yet whether one CIBA can target multiple `aud`s in a single token. If not, hr_agent might need *two* CIBA flows when it needs to call both HR-A2A and HR-MCP audiences. Capability test deferred — needs a `c5_multi_resource.py` adapted to CIBA.

6. **Future UAE Pass federation.** The `Allow Federated user = true` and `Skip user validation = true` settings preserve UAE Pass flexibility per the expert. We should confirm that when UAE Pass is added, federated users also get CIBA prompts on a device they own (not just via internal IS users).

7. **Sprint 2 (revocation / Secure Session Termination) impact.** The original Sprint 2 plan assumed back-channel logout invalidates a single chain of related tokens. With CIBA, each agent has its own independent token life — does logout still cascade? Or does each token expire independently? Sprint 2 design doc needs revision.

---

## 8. Implications for the milestone plan

The product council should review the following sections of `docs/milestone-plan.md` (assumed v3) and propose updates:

| Plan section | Original assumption | Update needed |
|---|---|---|
| §1.1 Architecture | RFC 8693 chained TX between hops | Per-agent CIBA |
| §3.3 Token flow (Hops 1–5) | Hops 3a/3b/4/5 do TX | Hops 3a/3b initiate CIBA on specialist; hop 4/5 (MCP) just consume the CIBA-issued token |
| §3.4 Task lists | TX-grant config on every agent | CIBA-grant + Notification Channels=External on every agent |
| §3.4-H N-tests | N1–N17 framed around TX | Re-frame around CIBA flows; add N-test for "user denies consent at agent N" |
| §3.6 Fallbacks | Multi-audience Pattern C | Drop — no longer applicable |
| §4 Sprint 2 | Cascade revocation across nested-act chain | Per-agent token expiry; cross-agent cascade requires bookkeeping |
| §5.2 Threat model | Token theft mid-chain | Token theft per agent (smaller blast radius); + new threat: actor_token theft enables impersonation |
| §7 Milestones | M1 = full A2A with TX | M1 = full A2A with CIBA (latency budget incorporated) |

---

## 9. What's signed off as of M0

✓ IdP selection: WSO2 IS 7.2.0 on-prem at `https://13.60.190.47:9443`.
✓ Substrate: probe.user, probe-client-a/b, probe-agent-a/b, urn:probe:api.
✓ Capability tests committed to repo (`idp_capability_test/`), runnable, validate every ingredient the new architecture depends on.
✓ Architecture pivot: per-agent CIBA replaces RFC 8693 chain (F4, F5).
✓ Documentation: this memo, `docs/configuring-ciba-grant-type.md`, `idp_capability_test/README.md`, `idp_capability_test/question-for-wso2-expert.md`.
✓ Memory: project_ciba_pivot, project_idp_migration_to_wso2_is, reference_ddademo_tenant_state (archived).

## 10. What requires council input

✓ Re-write of `docs/milestone-plan.md` §3.3 + §3.4 + §5.2 + §7.
✓ Decision on audit strategy (correlation IDs vs other).
✓ Decision on Sprint 2 revocation model under CIBA.
✓ UX walkthrough of the consent widget given per-agent consent prompts.
✓ Threat model update for actor_token-as-credential.
✓ Re-estimation of Sprint 1 / Sprint 2 effort given the pivot.
✓ Decision on which legacy demo patterns (`_archive/agent.before-v3/`) carry forward and which are replaced.

---

## Appendix A — Probe outputs (most recent successful runs)

### C0 PASS
```
JWKS returned 1 key(s); first kid=MmJh...
Token endpoint returned HTTP 401 (expected for bad creds)
probe-client-a got a token (first 40 chars): eyJ4NXQjUzI1NiI...
```

### I1 (Pattern C) PASS
```json
{
  "sub": "2c16b987-4e01-40c1-b706-5f8550ebec0e",
  "aut": "APPLICATION_USER",
  "iss": "https://13.60.190.47:9443/oauth2/token",
  "act": { "sub": "e7a5367d-ba4f-444c-a4d5-51eba153426d" },
  "scope": "openid"
}
```

### I4 (App-Native Auth) PASS
```json
{
  "sub": "e7a5367d-ba4f-444c-a4d5-51eba153426d",
  "aut": "AGENT",
  "iss": "https://13.60.190.47:9443/oauth2/token",
  "scope": "internal_login openid"
}
```

### I8 (CIBA) PASS
```json
{
  "sub": "2c16b987-4e01-40c1-b706-5f8550ebec0e",
  "aut": "APPLICATION_USER",
  "iss": "https://13.60.190.47:9443/oauth2/token",
  "act": { "sub": "941a5f32-a36a-43dc-8fe5-31bd09661c65" },
  "aud": "K4jewPynBce69vKRbEd34LIXtqca",
  "scope": "openid"
}
```

## Appendix B — Files of interest for review

- `idp_capability_test/` — capability tests (Python, runnable)
  - `c0_reachability.py`, `c1_pattern_c.py`, `c4_app_native_authn.py`, `c8_ciba.py` ← passing
  - `c2_basic_token_exchange.py`, `c3_nested_act.py` ← document the F1/F4 negative findings
  - `README.md` ← step-by-step setup
  - `question-for-wso2-expert.md` ← the question that elicited the CIBA workaround
- `docs/milestone-plan.md` — v3 plan, needs revision per §8 above
- `docs/wso2-is-setup.md` — IdP setup guide, needs CIBA-flavored update
- `docs/configuring-ciba-grant-type.md` — the WSO2 doc fragment we're working against
- `_archive/agent.before-v3/` — legacy demo (`obo_flow.py`, `agent_auth.py`, `session.py`, `main.py`) — review for which patterns survive the pivot
- `../hotel-booking-agent-autogen-agent-iam/` — sister sample; closer to the new architecture's per-agent OBO model
