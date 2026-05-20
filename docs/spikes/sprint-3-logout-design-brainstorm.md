# Sprint 3 — Logout & revocation design brainstorm

**Status:** Pre-Stage-1 spike for Sprint 3 (M3 = revocation per `milestone-plan.md` §3 Sprint 3).
**Authors:** capability-spike investigation, 2026-05-08.
**Decision-impact:** the answers below shape D3.1 / D3.2 acceptance, BCL endpoints on agent apps, and the orchestrator's session-map cleanup.

This document is an **input** to the Sprint 3 Stage 1 PM/BA review — it is **not** a locked design.

## §-1. C12 spike result (2026-05-09) — VERDICT LOCKED

**Empirical finding (F-19):** WSO2 IS 7.2.0 does NOT register CIBA-issued tokens as user sessions, and therefore does not fan out OIDC Back-Channel Logout when those tokens are revoked.

**Evidence collected on 2026-05-09:**

1. **`hr_admin_user` (sub `15fab9e7-…`) successfully completed a CIBA flow** to HR-AGENT (rid `52e0c885-…`, auth_req_id `d27e3ccb-…`). MCP call returned 200; OBO token was minted, accepted by `hr_server`, cached in HR-AGENT's `_CachedToken`.
2. **IS Console → User Management → `hr_admin_user` → Active Sessions** showed **"No active sessions"** four minutes after the CIBA flow completed. (Per OIDC spec, IS would list one session per RP that has an active grant. CIBA grants do not appear.)
3. **BCL listener** (registered for HR-AGENT, IT-AGENT in IS Console) **captured nothing** during a subsequent RP-initiated `/oidc/logout`. Listener + tunnel verified healthy via direct curl from the AWS VM.

**Why this happens:** CIBA is a **decoupled** authentication flow — the user's authentication device and the requesting client are different. IS treats this as "a token was minted via consent" but **does not establish an OIDC session for the agent app**. BCL fan-out is keyed on IS's session table; CIBA grants are absent from it; therefore no BCL.

This is exactly the Curity practitioner observation we had quoted in §9.4 — now confirmed empirically against our specific stack.

### Verdict — what changes for Sprint 3

| | |
|---|---|
| **Selected design** | **Option A** (orchestrator-only OIDC client; orchestrator fans out internally) |
| **Dropped from scope** | Agent-side BCL receivers (option C's defense-in-depth layer) — IS will never call them |
| **Preserved** | Orchestrator-driven cache-bust to specialists by `jti`, MCP server denylist + introspection, RP-initiated logout from SPA → IS for IS-side session cleanup |
| **Sprint 3 simplification** | One BCL receiver to write (orchestrator's, just to absorb admin-terminate fan-out for the orchestrator-mcp-client app — which is the MCP Client type and doesn't expose BCL config either, so even THAT receiver is optional) |

### Implications for the demo narrative

The "5 s cascade" target is now entirely the orchestrator's responsibility — no IS hand-holding. The pitch sharpens: *the orchestrator is the gateway; revocation is observable as a single internal cache-bust fan-out, not a multi-source choreography.* This actually strengthens the demo's "orchestrator IS the policy enforcement point" framing.

### Files of record

- `idp_capability_test/c12_logout_capability.py` — auto-probe + manual recipe (still useful as a reproducible artefact for stakeholder scrutiny).
- `tools/_bcl_log/bcl_received.log` — captured-nothing on the test run; **the empty file is itself the verdict**.
- `docs/architecture/sprint-1-fixes.md` §F-19 — the empirical finding (added with this commit).

---

## §0. TL;DR

- **This is "the new OAuth problem."** Industry-wide unresolved gap with active spec work (CAEP, Grantex, AIP, IETF draft-klrc-aiagent-auth, A2A spec issue #19). We are not breaking new ground; we are picking from a menu the industry is converging on.
- **Real-world bite mark:** Salesloft Drift breach (Aug 2025) — 700+ companies compromised in 10 days because OAuth tokens issued to an AI chat agent had no cross-domain revocation. This is the breach this sprint exists to prevent.
- **Pattern industry recommends, ranked:**
  1. **Gateway pattern** — orchestrator owns auth, fan-outs revocation. ScaleKit, Okta, AWS AgentCore all converge here. **Matches our design.**
  2. **Cascade revocation** — revoking a parent grant kills all child grants atomically. Grantex protocol's headline primitive.
  3. **CAEP (Continuous Access Evaluation Profile)** — OpenID standard event channel for real-time revocation signals between cooperating systems. Microsoft's pitch: "long-lived tokens (24 h) + real-time revoke" instead of short-lived tokens + polling.
- **Industry latency bar:** sub-second propagation. Our milestone-plan target of 5 s is conservative; we should be able to hit ≤2 s for the demo.
- **Recommendation unchanged: hybrid (option C).** Industry context strengthens it — orchestrator is the gateway (matches all three reference architectures), agent-side BCL is defense-in-depth (matches OIDC spec compliance), the internal cache-bust RPC is morally equivalent to a CAEP transmitter→receiver event.

---

## §1. The user-visible asks (from `milestone-plan.md`)

- **D3.1** Sign Out in SPA → orchestrator revokes token-A → all in-flight specialist CIBA flows cancel → all already-issued specialist tokens become invalid within 5 s.
- **D3.2** Admin terminates user's session via IS Console → same effect within 5 s.
- **D3.3** R-tests R1–R16 pass.
- **D3.4** `binding_message` for re-issued tokens after partial revoke is clearly distinct.

So a "logout" in the demo means three observable effects within 5 s:
1. Orchestrator session is gone (cookie cleared, in-memory state purged).
2. Any pending CIBA polls stop and don't bring back a usable token.
3. Any *already-issued* OBO tokens (HR's token-B, IT's token-B') stop working at the resource server.

---

## §2. What the OIDC specs say

### §2.1 RP-Initiated Logout 1.0
- OP exposes `end_session_endpoint`. RP redirects the **user's browser** with `id_token_hint`, optional `post_logout_redirect_uri`, `state`, `client_id`, `logout_hint`.
- OP confirms with the user (consent-screen UX), notifies other RPs in the user's session via registered logout mechanisms, then redirects back.
- Requires the user's browser to be alive.

### §2.2 Back-Channel Logout 1.0
- OP POSTs `application/x-www-form-urlencoded` with body `logout_token=<JWT>` to each RP's pre-registered `backchannel_logout_uri`.
- `logout_token` claims:
  - `iss`, `aud`, `iat`, `jti` (standard, validated like an ID Token)
  - `events: {"http://schemas.openid.net/event/backchannel-logout": {}}` — required, distinguishes from an ID Token
  - `sub` and/or `sid` — at least one must be present
  - `nonce` MUST be absent
  - Recommended: `typ: logout+jwt` JOSE header
- RP returns 200 / 204; OP doesn't normally retry.
- Auth0 / Curity / Zitadel / Ping all implement this faithfully.

### §2.3 Front-Channel Logout 1.0
- OP renders an iframe pointing at the RP's `frontchannel_logout_uri` with `?iss=…&sid=…`.
- Requires user-agent to be alive AND iframe to load.
- Brittle for our case (CSP, third-party cookies, browser closing).

### §2.4 Critical practitioner note (Curity)
> Service applications (bots, agents) that issue tokens without maintaining UI-level sessions face a fundamental challenge: they have no "session" to terminate. Back-channel logout becomes moot if the app doesn't track session state. These applications should rely on token expiration and revocation mechanisms rather than logout flows.

This is **us**. The HR Agent and IT Agent are headless services — they don't host the user's browser session. They mint OBO tokens via CIBA when the user (via the orchestrator) asks them to. IS could send them BCL pings, but:
- The agents don't have a "session" to invalidate per se — what they have is a *cached OBO token + scope-bound consent record*.
- Targeting by `sid` only works if the OBO token carries `sid` (which is what **C12.3** in the probe checks — open question).

---

## §3. What the WSO2 IS Console UI tells us (your screenshot)

Agent applications **do** expose:
- Back channel logout URL field
- Front channel logout URL field

So IS will *let us register* both URLs for the IT-AGENT-9900… application. The remaining open question is whether IS *actually fires* the BCL POST for tokens that were issued via CIBA (vs. only via authorization_code) — this is **C12 manual recipe** below.

WSO2 IS docs (search-result snippets — full pages were Cloudflare-blocked by WebFetch):
- Configuring Back-Channel Logout enabled per-application via Protocol tab.
- Default `iat` validation window 300 s; signature validated against IS's JWKS.
- IS-side BCL endpoint at `https://<is>/identity/oidc/slo` is the *receiver* for upstream logouts when IS is acting as a federated SP — not relevant to us.

---

## §4. Three architectural options

### Option A — Orchestrator-only OIDC client; orchestrator fan-outs internally
*Mirrors `milestone-plan.md` §3 Sprint 3 S3.2 + S3.3 as written.*

```
                  ┌─────────────────────────────────────┐
                  │            WSO2 IS                   │
                  │                                      │
                  │   /oidc/logout (RP-initiated, OR     │
                  │    Console terminate)                │
                  └──────────────┬───────────────────────┘
                                 │ BCL POST (logout_token)
                                 ▼
                  ┌─────────────────────────────────────┐
                  │         Orchestrator                 │
                  │                                      │
                  │  • validates logout_token            │
                  │  • clears orch_sid cookie + session  │
                  │  • walks completed_ciba_log[jti]     │
                  │  • POSTs /internal/revoke?jti=…      │
                  │     to each specialist               │
                  │  • signals pending_ciba.cancel_event │
                  └──────────────┬───────────────────────┘
                                 │ POST /internal/revoke
                                 ▼
                  ┌─────────────────────────────────────┐
                  │   HR Agent / IT Agent (specialists)  │
                  │                                      │
                  │  • adds jti to in-memory denylist    │
                  │  • drops _CachedToken entry          │
                  └──────────────┬───────────────────────┘
                                 │ next MCP call
                                 ▼
                  ┌─────────────────────────────────────┐
                  │   HR Server / IT Server (RS)         │
                  │                                      │
                  │  • introspects token (or checks      │
                  │    denylist by jti)                  │
                  │  • returns 401 ERR-MCP-002           │
                  └─────────────────────────────────────┘
```

**Pros:**
- One BCL surface to register at IS → simpler.
- Orchestrator is the single source of truth for "who has tokens for this user".
- Cleanly maps to the Sprint 3 milestone-plan deliverables S3.1–S3.4.
- Defense-in-depth at the resource server: introspection / denylist still gates every MCP call.

**Cons:**
- If the orchestrator is down at logout time, no fan-out happens. (Mitigated: tokens expire on their own, and IS can also be configured to revoke.)
- "Internal" endpoint isn't a standard OIDC primitive — extra spec surface to test.

### Option B — Each agent registers its own BCL URL
*The "spec-pure" version.*

IS calls each agent app's BCL URL directly. Each agent verifies the logout_token and self-cleans (drops cached token, adds jti to denylist).

**Pros:**
- Spec-clean. No orchestrator-internal protocol.
- Agents don't depend on orchestrator availability.
- IS audit row gets a clean per-app logout event for free.

**Cons:**
- **Open question (C12 manual recipe answers this):** does IS actually fire BCL for an app whose token was issued via CIBA? If "no" → option B doesn't work, we're forced into A.
- Even if IS fires BCL, the `logout_token.aud` will be the agent's OAuth client_id and `sub` will be the user — but the agent might have *multiple* cached tokens for that user across different sessions. Without `sid`, the agent has to invalidate *all* of them. For our single-process POC this is fine.
- More config sprawl: BCL URL on each of HR-AGENT and IT-AGENT plus orchestrator.

### Option C — Hybrid (recommended)
Orchestrator owns the user-facing logout (RP-initiated + BCL receive); orchestrator fans out cache-bust to specialists via internal protocol; specialists *also* register BCL URLs as defense-in-depth.

**Pros:**
- Demo-friendly: one click in the SPA → cleanly observable cascade.
- Spec-honest: BCL on agents documents that we *would* honor IS-driven invalidation.
- Fast: orchestrator's internal fan-out is sub-second; doesn't depend on IS scheduling BCL POSTs to agents.
- Resilient: even if the orchestrator is down or the internal RPC fails, IS's BCL eventually reaches the agents (if C12 confirms IS fires it for CIBA-issued tokens).

**Cons:**
- Most surface area — three BCL receivers + one internal protocol.
- Idempotency matters: a jti might be invalidated by both the orchestrator's internal RPC and IS's BCL at roughly the same time. Trivial — `set.add` is idempotent.

---

## §5. Decision matrix — which do we recommend?

| Concern | Option A | Option B | Option C |
|---|---|---|---|
| Demo "5 s cascade" target met | ✓ (orchestrator drives it) | ? (depends on IS scheduling) | ✓ (orchestrator drives it) |
| Survives orchestrator down at logout | ✗ | ✓ | ✓ |
| Spec-honest narrative for stakeholders | △ (proprietary internal RPC) | ✓ | ✓ |
| Implementation effort (LOC + config) | low | medium | medium-high |
| Risk of design surprise after probe | low | high | medium |
| **Recommendation for Sprint 3** | OK fallback | Risky | **Pick this** |

**Recommendation:** start Sprint 3 implementation with Option A (matches milestone plan); register BCL URLs on the agent apps in IS Console (Option B's setup) only after C12 manual recipe confirms IS fires BCL for CIBA-issued tokens. If confirmed, layer in the agent-side BCL receivers as a small follow-up slice — that promotes us from A to C without rework.

---

## §6. Open questions (the brainstorm)

These need PM/BA + your gut-check before Sprint 3 Stage 1 closes:

1. **Q-LOGOUT-1: Does IS fire BCL for CIBA-issued tokens at all?**
   The C12 manual recipe is the way to find out. Run on a quiet morning, takes ~10 min. If "no" we know to skip Option B/C's agent-BCL surface entirely.

2. **Q-LOGOUT-2: When the user clicks Sign Out, do we want IS's logout consent screen or just silent invalidation?**
   - With consent screen: spec-correct, but adds a second click ("Yes, sign me out").
   - Silent: drop the orch_sid cookie, hit `/oauth2/revoke` for token-A, fan-out to specialists, redirect to `/?reason=signed_out`. Faster demo. Most consumer apps do this.
   - **Recommendation:** silent for the demo; document RP-initiated path for production.

3. **Q-LOGOUT-3: Token-A revocation — `/oauth2/revoke` or rely on session-cookie clear?**
   Token-A is the orchestrator's own session token (Pattern C). The orchestrator session is session-cookie-gated, so cookie-clear is enough for "user can't keep using the SPA". But token-A may still introspect as `active=true` until natural expiry (1 h). For an attacker who stole the cookie + token-A, only `/oauth2/revoke` actually kills the token. **Recommendation:** call `/oauth2/revoke` AND clear cookie.

4. **Q-LOGOUT-4: Cancellation of in-flight CIBA on logout — is C10 (`auth_req_id` revoke at IS) needed?**
   We *already* have local cancel via `pending.cancel_event.set()` from Sprint 2.2's SSE-disconnect hook. The local cancel is enough for our polling loop. But the auth_req_id is still alive at IS until natural expiry (300 s). If the user clicks Approve at IS *after* logging out of the orchestrator, IS would happily issue a token that no one is listening for. Mostly harmless — the token has no aud-matching consumer — but the audit log shows a "successful" CIBA after logout, which reads weird. **Recommendation:** if C10 capability test passes, call IS to invalidate `auth_req_id` on logout. If not, document the "ghost approval" caveat and move on.

5. **Q-LOGOUT-5: Specialist denylist — in-memory `set[jti]` or persistent?**
   `set[jti]` per-process matches our Q5 single-process decision. Memory grows unbounded only if we never expire entries — but every jti has an `exp`, so we can sweep on any access. **Recommendation:** in-memory, with periodic sweep; document Redis/persistent store for multi-replica future.

6. **Q-LOGOUT-6: MCP server enforcement — introspection (per-call IS round-trip) or denylist (local)?**
   `milestone-plan.md` S3.4 says introspection with 2 s positive cache, hard fail on `active=false`. Trade-off:
   - Introspection: tells the truth from IS, no local state needed, but a network call on every MCP request.
   - Denylist: zero-latency, but only as good as the cache-bust fan-out.
   - **Hybrid:** introspect the first time we see a jti (cache the answer for `min(exp - now, 60s)`), then trust the cache; clear the cache entry on cache-bust.
   - **Recommendation:** for the demo, denylist + 60 s introspection cache. Cleaner narrative ("the agent learned the token was revoked within X seconds").

7. **Q-LOGOUT-7: Persistent session store (Redis) — pull into Sprint 3 or defer?**
   Carry-over from Sprint 2 retro. Without Redis, every orchestrator restart loses sessions and forces re-login. Not strictly required for D3.1/D3.2 acceptance. **Recommendation:** defer; out of Sprint 3 scope per Q5.

8. **Q-LOGOUT-8: Branch — keep `sprint-1-build` or rename?**
   Cosmetic. Decide at Sprint 3 kickoff. **Recommendation:** rebase to `main` after Sprint 2 final review and start `sprint-3-build` clean.

---

## §7. What I want from you at the brainstorm

- Quick-take on each Q-LOGOUT-N — if any of my recommendations are wrong-headed, course-correct now.
- Decide whether to run C12 (the probe + manual recipe) BEFORE or AFTER Stage 1 PM/BA review:
  - **Before:** Stage 1 has a concrete capability answer to lock the design around. ~30 min on the IS Console + listener.
  - **After:** Stage 1 produces an option-A locked design with a flagged dependency on C12; we run C12 in week-1 of implementation.
- Confirm the slice plan shape (mirror Sprint 2A/2B):
  - **3A (demo-critical):** Sign-out endpoint, internal cache-bust fan-out, specialist denylist, MCP introspection, end-to-end UC-09 walkthrough.
  - **3B (defense-in-depth):** Agent-side BCL receivers (gated on C12 outcome), BCL endpoint on orchestrator (receive admin terminate), R-tests R1–R16.

## §7.5. C12 spike runbook (laptop demo + AWS-hosted IS via reverse SSH)

Because WSO2 IS lives on an AWS VM (`13.60.190.47:9443`) and your demo stack runs on the laptop, IS cannot reach `http://localhost:*` on the laptop directly. We use a **reverse SSH tunnel** from the laptop to the IS VM — IS calls its own loopback (`http://localhost:8123/bcl`), and that traffic forwards over the tunnel to the laptop's docker-compose `bcl-listener`. No public exposure, no third-party tunnel service, persistent across laptop sleep/wake via `autossh`.

Concrete first-time setup walkthrough lives in [`c12-bcl-spike-setup.md`](c12-bcl-spike-setup.md). Summary below.

### Architecture

```
   AWS VM (where IS runs)                       laptop
 ┌──────────────────────────┐               ┌────────────────────────┐
 │ WSO2 IS                   │               │  bcl-listener          │
 │   POST localhost:8123/bcl ─────────────►  │  docker container,     │
 │        ▲                   │               │  bound to              │
 │        │ tunnel forwards   │               │  127.0.0.1:8123        │
 │        ▼ via reverse SSH   │               │                        │
 │ sshd (loopback bind)       │  ◄── autossh ────  laptop initiates SSH
 └──────────────────────────┘               └────────────────────────┘
```

### Steps

1. **One-time setup (macOS):**

   ```
   ./scripts/spike-bcl-prep-mac.sh
   ```

   Installs `autossh`, captures `AWS_VM_HOST`/`AWS_VM_USER` to `.env`, verifies SSH connectivity, pre-pulls the listener image. Idempotent.

2. **Bring up the rig:**

   ```
   ./scripts/spike-bcl-up.sh
   ```

   Starts `bcl-listener` (bound to `127.0.0.1:8123`), spawns `autossh -f -N -R 8123:127.0.0.1:8123 …`, and smoke-tests by `ssh`-ing into the VM and `curl`-ing the forwarded port.

3. **Register in WSO2 IS Console** for each agent application AND the orchestrator (control comparison): `Application → Protocol → Logout URLs → Back channel logout URL = http://localhost:8123/bcl`.

4. **Run the auto-probe** (decodes CIBA-issued tokens for `sid` / `sub` / `aud`):

   ```
   cd idp_capability_test
   PROBE_USER_SUB=<employee_user_sub> python3 c12_logout_capability.py
   ```

5. **Trigger logout** (compare both paths):

   - **RP-initiated:** `https://13.60.190.47:9443/oidc/logout?id_token_hint=<orch-id-token>&post_logout_redirect_uri=http://localhost:8090/&client_id=<orch-client-id>`
   - **Admin terminate:** IS Console → User Management → probe.user → Active Sessions → Terminate.

6. **Read the capture:**

   ```
   docker compose --profile spike-bcl logs -f bcl-listener
   cat tools/_bcl_log/bcl_received.log
   ```

7. **Tear down:**

   ```
   ./scripts/spike-bcl-down.sh
   ```

### Verdict matrix (drives Sprint 3 Stage 1)

| Listener saw POST for… | Conclusion | Implication |
|---|---|---|
| **orchestrator URL only** | IS treats agent apps as machine-clients without sessions for BCL. | Option C **degrades to A**. Skip agent-side BCL receivers. |
| **orchestrator + agent URLs** | IS fans BCL to agent apps with CIBA-issued tokens. | Option C viable. Wire agent BCL receivers in Sprint 3B (defense-in-depth). |
| **neither** | Misconfig somewhere (BCL URL save, IS audit log, sshd `AllowTcpForwarding`). | See troubleshooting in `c12-bcl-spike-setup.md`. |

### Files added for the spike (commit-worthy)

| File | Role |
|---|---|
| `scripts/spike-bcl-prep-mac.sh` | macOS first-time setup: brew, autossh, .env, SSH connectivity test |
| `scripts/spike-bcl-up.sh` | Brings up `bcl-listener` + `autossh -R`; smoke-tests the tunnel |
| `scripts/spike-bcl-down.sh` | Tears down autossh + container; preserves capture log |
| `tools/bcl_listener.py` | HTTP listener that decodes incoming `logout_token` JWTs |
| `docker-compose.yml` (`bcl-listener` service under profile `spike-bcl`) | Container definition; bound to `127.0.0.1:8123` so SSH-R can reach it |
| `idp_capability_test/c12_logout_capability.py` | Auto-decode probe + manual recipe (`MANUAL_RECIPE` constant printed at end) |
| `docs/spikes/c12-bcl-spike-setup.md` | First-time operator guide (this spike's runbook) |

---

## §8. Files of record

- `idp_capability_test/c12_logout_capability.py` — auto + manual probe (this spike's deliverable).
- `docs/spikes/sprint-3-logout-design-brainstorm.md` — this document.
- TBD: `docs/use-cases/UC-09-logout-cascade.md` (write during Stage 1 once Q-LOGOUT-1..6 answered).

---

## §9. Industry context — this is "the new OAuth problem"

We are not building this from first principles. The agent-revocation problem is being actively wrestled with across vendors and standards bodies. Naming the patterns helps stakeholders trust the design and helps us crib from prior art.

### §9.1 The bite-mark case study

**Salesloft Drift breach, August 2025** — a chat AI agent's OAuth tokens were exfiltrated; the same tokens were valid across 700+ downstream systems because each trust domain validated tokens in isolation, with no shared revocation primitive. The breach unfolded over 10 days. This is the *exact* threat model Sprint 3 D3.1/D3.2 exists to defend against. Worth a single slide in the demo deck.

### §9.2 Vendor patterns — three converge on the same shape

All three large vendors building "production OAuth for AI agents" land on the **gateway pattern**:

| Vendor / pattern | What they call it | What it maps to in our design |
|---|---|---|
| **ScaleKit** "OAuth for AI Agents" | Centralized gateway owns credentials; downstream MCP connections flow through it; SCIM event from IdP triggers gateway revoke = downstream revoke | The orchestrator is our gateway |
| **Okta** "Universal Logout for AI Agents" | "Kill switch": admin disable in Okta → revoke fans out to every federated app and agent | Sprint 3 S3.2 sign-out + cache-bust |
| **AWS Bedrock AgentCore Identity** | Token Vault inside the agent runtime; provider-side revoke + `forceAuthentication: true` re-prompts the user | Our `_CachedToken` + cache-bust + re-CIBA path (UC-06 / D2.5 already built) |

The pattern is consistent: **agents do not own their credential lifecycle directly; a gateway does. Revocation = revoke at gateway + propagate.**

This is exactly our orchestrator's role. Sprint 3 implementation just makes the propagation explicit.

### §9.3 Emerging standards (the menu we're picking from)

| Standard | What it is | Relevance to us |
|---|---|---|
| **OIDC Back-Channel Logout 1.0** | OP→RP signed-JWT POST on session end | Already in our menu (option B/C). C12 probe checks if WSO2 IS fires it for CIBA-issued tokens. |
| **OIDC RP-Initiated Logout 1.0** | Browser redirect to `end_session_endpoint` | The "Sign Out" click in the SPA — we'll wire this. |
| **OpenID CAEP 1.0** (Continuous Access Evaluation Profile) | Standardized event types over Shared Signals Framework: `session-revoked`, `token-revoked`, `risk-incident`. Designed for real-time fan-out between cooperating IdPs/RPs. | Our internal cache-bust RPC is morally equivalent to a CAEP `session-revoked` event. **For the POC**: we don't implement CAEP wire format; for **production roadmap**: this is the upgrade path. Cite it in the demo to anchor the design in standards. |
| **OpenID Shared Signals Framework (SSF)** | Umbrella over CAEP + RISC + custom event types | Same — roadmap reference. |
| **Grantex** (OAuth-inspired, agent-native) | Headline primitive: cascade revocation with sub-second propagation across multi-agent delegation chains. Graph-native delegation: revoke parent → revoke all children atomically. | Validates our "revoke at gateway → fan out" approach as the industry-converging pattern. |
| **draft-klrc-aiagent-auth-00** (IETF) | Early draft codifying agent identity, delegation, scope-limiting, revocation requirements | Reference; cite in tech-arch doc to show standards alignment. |
| **AIP (Agent Identity Protocol)** (arxiv research) | Verifiable delegation across MCP and A2A boundaries via macaroons / biscuit tokens | Out of scope for our POC (constrained-delegation tokens are heavyweight). Roadmap reference. |
| **A2A spec issue #19** | "Delegated User Authorization for Agent2Agent Servers" — open in Google's A2A repo, no consensus yet | Confirms the gap is *not yet solved* even by the protocol's authors. Our POC is contributing to a live debate. |

### §9.4 Practitioner principles we've cribbed from

These quotes shaped our recommendation:

> "Revoked or downgraded authorization MUST be enforced without undue delay. Invalidated tokens and cached authorization decisions MUST NOT continue to be used after a revocation or risk notification is received." — Strata, *2026 Guide to OAuth Token Exchange & Agentic AI*

> "Service applications (bots, agents) that issue tokens without maintaining UI-level sessions face a fundamental challenge: they have no 'session' to terminate. Back-channel logout becomes moot if the app doesn't track session state. These applications should rely on **token expiration and revocation mechanisms rather than logout flows**." — Curity, *How OIDC Single Logout Works*

> "Each hop multiplies trust without attenuation — the fundamental risk." — *The New OAuth Problem Is Agent Delegation*, dev.to

> "When the root authority is withdrawn, dependent delegated grants should not keep drifting alive in queues, workers, or child agents." — same article

> "By invalidating the refresh tokens and killing the session at the server level, you cut off the agent's access to all downstream MCP servers and tools immediately." — practitioner consensus

### §9.5 Implications for our Sprint 3 design

1. **Latency bar = sub-second** (industry). We can drop the milestone-plan's 5 s target to ≤2 s comfortably.
2. **Don't bet on BCL alone** for agent apps that don't host user sessions (Curity). Orchestrator-driven fan-out is the spine.
3. **Cite CAEP** as the production roadmap target so stakeholders see the design ladders cleanly upward from the POC.
4. **Salesloft Drift slide** is the perfect demo motivator — it's recent, well-known, and exactly our threat.
5. **A2A spec gap is real** — frame our work as a reference implementation contributing to the open debate, not as a one-off.

This adds a **Q-LOGOUT-9** to the brainstorm:

9. **Q-LOGOUT-9: Should we wire our cache-bust to look like a (subset of) CAEP `session-revoked` events?**
   Pros: standards-aligned out of the box; future-proofs the wire format if/when we add a second IdP. Cons: extra JWT shape that we ourselves verify; might be ceremony for an internal RPC. **Recommendation:** ship the POC with a simple JSON `{"jti": "...", "user_sub": "...", "reason": "user_signed_out"}` over POST; document the CAEP migration in the tech-arch doc as a Sprint 4+ refinement.

---

## §10. References

### OIDC specs
- [OIDC Back-Channel Logout 1.0](https://openid.net/specs/openid-connect-backchannel-1_0.html)
- [OIDC RP-Initiated Logout 1.0](https://openid.net/specs/openid-connect-rpinitiated-1_0.html)
- [OpenID CAEP 1.0 (final)](https://openid.net/specs/openid-caep-1_0-final.html)
- [OpenID Shared Signals WG specifications](https://openid.net/wg/sharedsignals/specifications/)

### WSO2 IS
- [WSO2 IS 7.0 — Back-Channel Logout configuration](https://is.docs.wso2.com/en/7.0.0/guides/authentication/oidc/add-back-channel-logout/)
- [WSO2 IS 7.0 — Add logout (RP-initiated)](https://is.docs.wso2.com/en/7.0.0/guides/authentication/oidc/add-logout/)

### Practitioner / vendor guidance
- [Curity — How OIDC Single Logout Works](https://curity.io/resources/learn/openid-connect-logout/)
- [ScaleKit — OAuth for AI Agents: Production Architecture](https://www.scalekit.com/blog/oauth-ai-agents-architecture)
- [ScaleKit — When an Employee Leaves, Who Revokes Their AI Agent's Access?](https://www.scalekit.com/blog/revoke-employee-ai-agent-access)
- [Okta — Universal Logout (kill-switch)](https://www.okta.com/blog/company-and-culture/introducing-universal-logout-for-all-adaptive-mfa-customers/)
- [AWS — Secure AI agents with Bedrock AgentCore Identity](https://aws.amazon.com/blogs/machine-learning/secure-ai-agents-with-amazon-bedrock-agentcore-identity-on-amazon-ecs/)
- [Auth0 — Back-Channel Logout (concrete logout_token JSON example)](https://auth0.com/docs/authenticate/login/logout/back-channel-logout)
- [WorkOS — Session revocation explained](https://workos.com/blog/session-revocation-sign-out-everywhere)

### AI-agent OAuth landscape
- [The New OAuth Problem Is Agent Delegation (dev.to)](https://dev.to/maninderpreet_singh/the-new-oauth-problem-is-agent-delegation-44hc)
- [ISACA — The Looming Authorization Crisis: Why Traditional IAM Fails Agentic AI](https://www.isaca.org/resources/news-and-trends/industry-news/2025/the-looming-authorization-crisis-why-traditional-iam-fails-agentic-ai)
- [CSA — AI Security When Your Agent Crosses Multiple Independent Systems: Who Vouches?](https://cloudsecurityalliance.org/blog/2026/03/11/ai-security-when-your-agent-crosses-multiple-independent-systems-who-vouches-for-it)
- [Grantex (OAuth-inspired protocol for AI agents)](https://grantex.dev/report/state-of-agent-security-2026)
- [Strata — 2026 Guide to OAuth Token Exchange & Agentic AI](https://www.strata.io/blog/agentic-identity/why-agentic-ai-demands-more-from-oauth-6a/)

### Standards efforts
- [draft-klrc-aiagent-auth-00 deep-dive (dev.to)](https://dev.to/kanywst/ai-agent-authentication-authorization-deep-dive-reading-draft-klrc-aiagent-auth-00-5d1)
- [AIP — Agent Identity Protocol (arxiv)](https://arxiv.org/html/2603.24775)
- [A2A spec — issue #19 Delegated User Authorization](https://github.com/a2aproject/A2A/issues/19)

### CIBA / OBO context
- [Curity — CIBA explained](https://curity.io/resources/learn/client-initiated-backchannel-authentication/)
- [ceposta — Bridging Agent Autonomy and Human Oversight with OIDC CIBA](https://blog.christianposta.com/ai-agents-and-oidc-ciba/)
- [ScaleKit — On-Behalf-Of authentication for AI agents](https://www.scalekit.com/blog/delegated-agent-access)
