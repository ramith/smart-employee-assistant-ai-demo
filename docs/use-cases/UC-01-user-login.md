# UC-01 — User logs in (Pattern C)

**Sprint:** 1
**Priority:** Critical (gates everything else)
**Maps to N-tests:** N1 (Pattern C produces token-A with depth-1 act), N12 (denial path)
**Maps to scenarios:** [user-experience.md](../user-experience.md) Scenario A

## Actors
- **Primary:** User (employee, in browser)
- **Secondary:** SPA, Orchestrator backend, WSO2 IS, `orchestrator-app` (SPA OAuth client), `orchestrator-mcp-client` (confidential code-exchange client), `orchestrator-agent` (agent identity)

## Preconditions
- WSO2 IS 7.2 reachable at the configured base URL
- `orchestrator-app`, `orchestrator-mcp-client`, `orchestrator-agent` registered per [`wso2-is-setup.md`](../wso2-is-setup.md)
- User has an account in the IS (`probe.user` in dev)
- SPA running at `localhost:3001`, orchestrator backend at `localhost:8090`

## Trigger
User opens the SPA and clicks **Sign in**.

## Main flow
1. SPA redirects browser to `<IS>/oauth2/authorize?client_id=<orchestrator-app>&response_type=code&redirect_uri=<spa>/callback&scope=openid orchestrate&state=<random>&code_challenge=<S256>&code_challenge_method=S256&requested_actor=<orchestrator-agent-id>`.
2. IS shows its login page; user enters username + password.
3. IS shows a consent screen: *"Orchestrator Agent wants to act on your behalf. Approve / Deny."*
4. User clicks **Approve**.
5. IS redirects browser to `<spa>/callback?code=<code>&state=<state>`.
6. SPA calls `POST <orch>/auth/exchange` with `{code, state, code_verifier}`.
7. Orchestrator backend validates `state`, then calls `<IS>/oauth2/token` authenticated as `orchestrator-mcp-client` (Basic auth) with body: `grant_type=authorization_code, code, code_verifier, redirect_uri, actor_token=<orchestrator-agent's I4 token>`.
8. IS returns **token-A**: `{sub=user-uuid, aut=APPLICATION_USER, act:{sub:orchestrator-agent-id}, aud=<orchestrator-app>, scope=openid orchestrate, exp=<now+3600>}`.
9. Orchestrator backend creates a session record `{session_id (cookie), user_sub, token_a, expires_at}` and sets a `Secure HttpOnly SameSite=Lax` session cookie on the SPA.
10. SPA receives 200 OK, redirects to chat view, displays user's name in header.

## Exception flows

### EX-1 — User denies the consent screen (step 4)
1. IS redirects to `<spa>/callback?error=access_denied&state=<state>`.
2. SPA renders a friendly error page: *"You did not approve the delegation. Please try again or contact admin."*
3. **No** session is created.
4. **N12** verifies this path.

### EX-2 — `state` mismatch (step 6)
1. Orchestrator backend rejects `POST /auth/exchange` with 400 if state is missing or doesn't match a pending login.
2. SPA shows: *"Login flow corrupted — please retry."*

### EX-3 — IS rejects the code exchange (step 7)
- `invalid_grant` (code already used / expired): SPA retry once; on second failure, surface error.
- `unauthorized_client` (bad credentials on `orchestrator-mcp-client`): orchestrator logs ERROR and returns 500. Operations issue, not user-facing recoverable.

### EX-4 — Actor token (orchestrator-agent's I4 token) expired
1. Orchestrator's actor-token cache returns expired token.
2. Orchestrator re-mints via App-Native Auth 3-step (transparent to the user).
3. Resumes step 7.

## Postconditions
- **Success:** orchestrator session exists, contains token-A; SPA can `EventSource` to `<orch>/events/<session_id>` for SSE; user lands in empty chat.
- **Failure:** no session record; user is at sign-in page with a friendly error; no token leaked to the SPA's storage.

## Design notes for downstream stages

### UX (Stage 3)
- **Sign-in screen:** standard "Sign in" button. Brand panel optional.
- **Cert warning passthrough:** dev environment uses self-signed cert on the IS; the user will see a browser warning the first time. Document this in onboarding, do not try to suppress it.
- **Error page:** distinct visual treatment for `access_denied` (gentle, recoverable) vs config errors (escalation prompt).
- **Returning user:** if IS still has an SSO cookie, the username/password step may be skipped; the consent screen should still appear.

### Architecture (Stage 4)
- Module: `orchestrator/auth/` — three routes: `GET /auth/login` (sets PKCE state, redirects to IS), `GET /auth/callback` (lands code, redirects to `/auth/exchange`), `POST /auth/exchange` (does the IS code-exchange, creates session).
- Reuse `_archive/agent.before-v3/session.py:SessionStore` as anchor; rename `obo_pkce_state` to `pkce_state`, drop `obo_code_verifier` (still needed actually for PKCE — keep but rename to `code_verifier`).
- **Actor token caching:** `common/auth/actor_token_provider.py` — adapt the archive's `agent_auth.py:AgentAuth.ensure_valid_token()` pattern. 30-second buffer before TTL.
- **Cookie:** `Secure HttpOnly SameSite=Lax`. Cookie name `orch_sid`. Cookie value is opaque session ID; token-A never leaves the orchestrator.

### Testing (Stages 7–8)
- **N1 automated:** mock the IS at the integration boundary (respond to `/oauth2/token` with a signed-with-test-key fake token-A); assert session is created with right `sub` and `act.sub`.
- **N12 automated:** simulate IS returning `error=access_denied` to the callback; assert no session created and SPA receives the friendly error.
- **Manual:** walk Scenario A in a real browser against the live IS. Take a screenshot of the consent screen as evidence. Confirm cookie is set with right flags (DevTools → Application → Cookies).
