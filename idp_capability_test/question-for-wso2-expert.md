# Question for WSO2 Identity Server expert — RFC 8693 chained delegation on IS 7.2.0

## Context — what we're building

A POC for **identity-first AI agent governance**: a multi-agent system where a user's request flows through a chain of agents (orchestrator → specialist agents → backend MCP servers), with the user's identity preserved end-to-end via OAuth delegation. Each hop should produce a token whose audit chain captures all prior actors.

The architecture mirrors the "Multi-Agent Authorization" pattern from your "Scenario 05: Token Exchange with delegation chain" slide — specifically the depth-2 nested `act` claim where `act.sub` is the most recent actor (Grading Agent in your example) and `act.act.sub` is the previous delegate (Study Assistant). That nested-`act` token was the key artifact we're trying to reproduce.

Original target was Asgardeo SaaS. After confirming Pattern C (`requested_actor`) works there but RFC 8693 token-exchange grant is unavailable on the SaaS UI (the `urn:ietf:params:oauth:grant-type:token-exchange` checkbox isn't exposed even after registering a self-trust Trusted Token Issuer), we migrated to **on-prem WSO2 IS 7.2.0** (running on a remote VM at `https://13.60.190.47:9443`, default tenant `carbon.super`, hostname configured to match the VM IP, JWKS/issuer URLs verified consistent).

---

## Substrate (minimal capability test setup)

Created in the IS Console:

| Artifact | Type | Purpose |
|---|---|---|
| `probe.user` | Local user | The end-user whose identity we're delegating |
| `probe-client-a` | Standard-Based Application (OIDC) | Front-door confidential client. Token Exchange + Authorization Code + Password + Client Credentials grants enabled. Authenticates the Pattern C exchange. |
| `probe-client-b` | Standard-Based Application (OIDC) | Identical config to `-a`. Used as the second-hop TX authenticator. |
| `probe-agent-a` | **Agent** (Interactive, "Allow users to log in" ON) | First delegate (would be `orchestrator-agent` in the real architecture) |
| `probe-agent-b` | **Agent** (Interactive, "Allow users to log in" ON) | Second delegate (would be `hr_agent` in the real architecture) |
| `Probe API` (`urn:probe:api`) | API Resource | Trivial scoped resource (`probe.read`, `probe.write`); all 4 apps subscribed |

The auto-created Agent Applications backing each agent were configured per their default templates. We discovered (Finding F3 below) that those Agent Apps **only expose grant types**: `Refresh Token`, `Code`, `CIBA`. Token Exchange is **not** in their allowed grant list — so we cannot use the Agent App as the TX authenticator.

---

## What we successfully validated (works as expected)

### ✅ I4 — App-Native Authentication for agents

Standard 3-step flow against `probe-agent-a`'s auto-created Agent App:

1. `POST /oauth2/authorize` with `response_mode=direct, scope=openid internal_login, response_type=code` + PKCE, Basic auth = OAuth Client ID/Secret of the Agent App. → returns `flowId` + `authenticatorId`.
2. `POST /oauth2/authn` with the flowId and `username=<agent-id>, password=<agent-secret>`. → returns auth code.
3. `POST /oauth2/token` with the code + PKCE verifier. → returns access token.

Resulting token decoded:
```json
{
  "sub": "e7a5367d-ba4f-444c-a4d5-51eba153426d",   ← Agent ID
  "aut": "AGENT",
  "iss": "https://13.60.190.47:9443/oauth2/token",
  "client_id": "<agent OAuth App client_id>",
  "aud": "<agent OAuth App client_id>",
  "scope": "internal_login openid",
  "org_handle": "carbon.super"
}
```

This works perfectly. We use this token as the `actor_token` in subsequent flows.

### ✅ I1 — Pattern C (depth-1 `act`)

`probe-client-a` initiates browser-based authorization with `requested_actor=<probe-agent-a's Agent ID>`. After `probe.user` authenticates and consents, the auth code is exchanged at `/oauth2/token` along with `actor_token=<probe-agent-a's token from I4>` in the request body.

Resulting token (`/tmp/c1_pattern_c_token.txt`):
```json
{
  "sub": "2c16b987-4e01-40c1-b706-5f8550ebec0e",   ← probe.user UUID
  "aut": "APPLICATION_USER",
  "iss": "https://13.60.190.47:9443/oauth2/token",
  "client_id": "<probe-client-a>",
  "aud": "<probe-client-a>",
  "act": { "sub": "e7a5367d-ba4f-444c-a4d5-51eba153426d" },   ← probe-agent-a
  "scope": "openid",
  "org_handle": "carbon.super"
}
```

This **exactly** matches the OBO token shape from your "Audit with delegation chains" slide — `sub=user`, `aut=APPLICATION_USER`, `act.sub=agent`. So Pattern C works as documented. 🎉

(Worth noting: we did NOT need to configure an "Authorized Actors" allowlist on `probe-client-a` — there's no such tab in the IS 7.2 Console, and Pattern C works without it. This is different from Asgardeo's gotcha #C and is a useful finding.)

---

## What we cannot get to work (the question)

### ❌ I2 — Chaining the depth-1 token through RFC 8693 to produce depth-2 nested `act`

Per your slide's flow: take the depth-1 token from I1 as `subject_token`, take a fresh agent token from I4 (`probe-agent-b`) as `actor_token`, exchange them at `/oauth2/token` with `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`. Expected result: token with `sub=probe.user, act.sub=probe-agent-b, act.act.sub=probe-agent-a`.

### Exact request

```
POST https://13.60.190.47:9443/oauth2/token
Authorization: Basic <base64(probe-client-b client_id : probe-client-b client_secret)>
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&subject_token=<C1 output token (Pattern C result with depth-1 act)>
&subject_token_type=urn:ietf:params:oauth:token-type:access_token
&actor_token=<probe-agent-b's I4 token>
&actor_token_type=urn:ietf:params:oauth:token-type:access_token
&scope=openid
```

We chose `probe-client-b` (a Standard-Based App) as the authenticator because Agent Apps lack TX grant (Finding F3). RFC 8693 §1.2 allows the authenticating client to be different from the actor — the actor identity comes from `actor_token`. We also tried with `probe-client-a` as authenticator (matching subject_token's `aud`) — same outcome.

### Exact response

```
HTTP/1.1 400 Bad Request
Content-Type: application/json

{
  "error": "invalid_request",
  "error_description": "Impersonator is not found in subject token."
}
```

### What we've verified rules out

- The TX grant **is** enabled and exposed on `probe-client-b` (and `-a`). The OIDC discovery doc (`/.well-known/openid-configuration`) lists `urn:ietf:params:oauth:grant-type:token-exchange` under `grant_types_supported`.
- A simpler test using a **vanilla user token** (Resource Owner Password Credentials grant — no `act` claim at all) as `subject_token` produces the **same exact error**. So this isn't a case of TX rejecting a chained token specifically — it rejects any subject_token that lacks an "impersonator".
- We decoded the C1 token and confirmed it has `act.sub` but **no claim named `impersonator`** anywhere. Full claim list:
  ```
  ['act', 'aud', 'aut', 'azp', 'client_id', 'exp', 'iat', 'iss',
   'jti', 'nbf', 'org_handle', 'org_id', 'org_name', 'scope', 'sub']
  ```

### The crux

**WSO2 IS 7.2's RFC 8693 implementation appears to require a claim named `impersonator` in the `subject_token`.** Pattern C (`requested_actor` parameter on `/oauth2/authorize`) populates `act` but **not** `impersonator`. So the two flows are not composable.

The slide demonstrates a token whose `act` chain has depth 2 — which can only come from a TX. So either:

1. There's a deployment.toml setting we're missing that makes Pattern C populate `impersonator` (in addition to or instead of `act`), or
2. There's a different `/authorize` parameter (beyond `requested_actor`) that triggers WSO2 IS's separate "Impersonation" feature (which does populate `impersonator`), or
3. The slide was generated against a different IS version / patch / extension where `act` and `impersonator` were unified, or
4. There's a fundamentally different flow we haven't discovered (perhaps a separate `/oauth2/impersonate` endpoint, or a specific scope like `internal_impersonate`, or a custom token type).

---

## Specific questions

In rough priority order:

1. **Is the slide's depth-2 nested-`act` token achievable on a stock WSO2 IS 7.2.0 install?** If yes, what specific config / flow / parameter did you use?
2. **What populates the `impersonator` claim?** Is it a different `/authorize` parameter (beyond `requested_actor`), a different scope, a different application template, or a deployment.toml setting?
3. **Is there documentation distinguishing "Impersonation" (which produces `impersonator`) from "Pattern C / requested_actor delegation" (which produces `act`)?** We've been treating them as the same thing — perhaps incorrectly.
4. **If `impersonator` and `act` are intentionally separate concepts**, what's the recommended way to compose user → agent-1 → agent-2 → backend with full audit preservation? Is the `act` claim chain even meant to grow via TX, or is it a single-level audit field?
5. **Finding F3 implication:** Agent Apps don't expose Token Exchange grant. Is this intentional? Should agents always use a separate confidential client (Standard-Based or MCP Client App) for TX, or are agents supposed to perform TX themselves through some other mechanism we haven't found?

## Findings ledger (so we can talk about specifics)

| F# | Finding |
|---|---|
| F1 | RFC 8693 with vanilla user `subject_token` (no `act`) is rejected with `"Impersonator is not found in subject token"`. |
| F2 | `urn:ietf:params:oauth:grant-type:token-exchange` is **not** auto-enabled on Standard-Based Apps; must be manually ticked. |
| F3 | Agent Apps' Protocol tab only allows `Refresh Token, Code, CIBA` — Token Exchange is unavailable. |
| F4 | Pattern C populates `act` but not `impersonator`. WSO2 IS's TX requires `impersonator`. The two appear to be different concepts in 7.2. |

## Tooling

We have a Python capability test harness (`idp_capability_test/`) with 6 standalone scripts that reproduce each step in isolation, with verbose request/response logging. Happy to share the full output of any specific test or to demo over screen-share. Each test prints the exact HTTP request and response so we can iterate quickly on suggestions.

The end-to-end goal is a green PASS on a probe that produces a token with:
```json
{
  "sub": "<user>",
  "aut": "APPLICATION_USER",
  "act": {
    "sub": "<agent-2>",
    "act": { "sub": "<agent-1>" }
  }
}
```
— exactly the artifact your slide shows. If we can produce that, the entire architecture is greenlit and we can move on to the substantive POC build.
