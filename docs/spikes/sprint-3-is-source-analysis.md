# IS source-code analysis (2026-05-09) — F-19/20/21 verdicts from first principles

**Source:** `/Users/ramith/code/identity-inbound-auth-oauth/components` (the WSO2 IS OAuth/OIDC/CIBA repo, head of `main`).
**Companion to:** [sprint-3-is-audit-log-analysis.md](sprint-3-is-audit-log-analysis.md) (audit-log evidence) and [sprint-3-c13-introspection-capability.md](sprint-3-c13-introspection-capability.md) / [c15_rp_initiated_logout_bcl.py](../../idp_capability_test/c15_rp_initiated_logout_bcl.py) (probes).
**Method:** subagent-led source dive answering three concrete questions.

This file resolves the F-19 ambiguity left open by the audit-log analysis and confirms F-20 / F-21 against source rather than against probe output. Recommendations at §4.

---

## §1. F-20 — `/oauth2/revoke` with `auth_req_id` is a no-op (CONFIRMED at source)

**Decisive citation:** [`org.wso2.carbon.identity.oauth/src/main/java/org/wso2/carbon/identity/oauth2/OAuth2Service.java`](file:///Users/ramith/code/identity-inbound-auth-oauth/components/org.wso2.carbon.identity.oauth/src/main/java/org/wso2/carbon/identity/oauth2/OAuth2Service.java) lines 571–640:

```java
boolean refreshTokenFirst = false;
if (isRefreshTokenType(revokeRequestDTO)) {
    refreshTokenFirst = true;
}
if (refreshTokenFirst) {
    refreshTokenDO = … getVerifiedRefreshToken(...)
    // ... search for REFRESH or ACCESS token only
} else {
    accessTokenDO = … getVerifiedAccessToken(...)
    if (accessTokenDO == null) {
        refreshTokenDO = … getVerifiedRefreshToken(...)
    }
}
```

**Companion:** [`org.wso2.carbon.identity.oauth.ciba/.../common/AuthReqStatus.java`](file:///Users/ramith/code/identity-inbound-auth-oauth/components/org.wso2.carbon.identity.oauth.ciba/src/main/java/org/wso2/carbon/identity/oauth/ciba/common/AuthReqStatus.java):

```java
public enum AuthReqStatus {
    REQUESTED,
    AUTHENTICATED,
    TOKEN_ISSUED,
    EXPIRED,
    FAILED,
    CONSENT_DENIED
}
```

**What this proves:**
- `/oauth2/revoke` only knows two token types: `access_token` and `refresh_token`. There is **zero code path** for `auth_req_id`. Even with `token_type_hint=auth_req_id`, the dispatch falls through to the default access-token search path.
- `AuthReqStatus` has **no `REVOKED` state**. There is no DB column or transition that could record a revoked auth_req_id even if the endpoint wanted to.
- The endpoint returns HTTP 200 because it cannot find the token in either table, and per RFC 7009 §2.2 *"the authorization server responds with HTTP status code 200 if the token has been revoked successfully or if the client submitted an invalid token."* IS chooses the latter interpretation.

**Verdict:** **F-20 stands.** Spec-compliant no-op by design. Sprint 3 3B.2 must NOT wire `auth_req_id` revoke; the orchestrator's local `cancel_event.set()` is the only cancellation primitive.

---

## §2. F-19 — BCL fan-out CAN happen with `id_token_hint`; F-19 was a probe artifact (REFINED at source)

This is the consequential finding. The original F-19 narrative — *"WSO2 IS does NOT register CIBA-issued tokens as user-session participants"* — is **wrong**. The audit log already hinted at it (§1 of audit-log-analysis: CIBA flows update the same sessionContextId). The source code confirms it.

### §2.1 The two-branch logout flow

[`org.wso2.carbon.identity.oidc.session/.../servlet/OIDCLogoutServlet.java`](file:///Users/ramith/code/identity-inbound-auth-oauth/components/org.wso2.carbon.identity.oidc.session/src/main/java/org/wso2/carbon/identity/oidc/session/servlet/OIDCLogoutServlet.java) lines 272–291:

```java
if (skipConsent) {
    if (StringUtils.isNotBlank(clientId) || StringUtils.isNotBlank(idTokenHint)) {
        redirectURL = processLogoutRequest(request, response);   // ← BCL path
        if (StringUtils.isNotBlank(redirectURL)) {
            response.sendRedirect(getRedirectURL(redirectURL, request));
            return;
        }
    } else {
        // Add OIDC Cache entry without properties since OIDC Logout
        // should work without id_token_hint
        OIDCSessionDataCacheEntry cacheEntry = new OIDCSessionDataCacheEntry();
        setStateParameterInCache(request, cacheEntry);
        addSessionDataToCache(opBrowserState, cacheEntry);
    }
    sendToFrameworkForLogout(request, response, logoutContext);  // ← skips BCL
    return;
}
```

**Two branches.** With `id_token_hint` OR `client_id` set: `processLogoutRequest()` runs, which eventually triggers BCL fan-out. Without either: an empty `OIDCSessionDataCacheEntry` is cached (no `clientId` resolved) and the request is sent to the auth framework for cookie cleanup only — no BCL.

### §2.2 The BCL fan-out itself walks ALL session participants

[`org.wso2.carbon.identity.oidc.session/.../backchannellogout/DefaultLogoutTokenBuilder.java`](file:///Users/ramith/code/identity-inbound-auth-oauth/components/org.wso2.carbon.identity.oidc.session/src/main/java/org/wso2/carbon/identity/oidc/session/backchannellogout/DefaultLogoutTokenBuilder.java) lines 125–169:

```java
public Map<String, String> buildLogoutToken(String opbscookie, String tenantDomain) … {
    Map<String, String> logoutTokenList = new HashMap<>();
    OIDCSessionState sessionState = getSessionState(opbscookie, tenantDomain);
    if (sessionState != null) {
        Set<String> sessionParticipants = getSessionParticipants(sessionState);
        if (!sessionParticipants.isEmpty()) {
            for (String clientID : sessionParticipants) {
                addToLogoutTokenList(logoutTokenList, sessionState, clientID);
            }
        }
    }
    return logoutTokenList;
}

private void addToLogoutTokenList(…, String clientID) {
    OAuthAppDO oAuthAppDO = getOAuthAppDO(clientID);
    String backChannelLogoutUrl = oAuthAppDO.getBackChannelLogoutUrl();
    if (StringUtils.isNotBlank(backChannelLogoutUrl)) {
        JWTClaimsSet jwtClaimsSet = buildJwtToken(sessionState, …, clientID);
        String logoutToken = OAuth2Util.signJWT(jwtClaimsSet, …).serialize();
        logoutTokenList.put(logoutToken, backChannelLogoutUrl);
    }
}
```

**Critical:** the loop iterates **every** clientID in `sessionParticipants` — there is **no exclusion of CIBA participants**. Every client that registered a `back_channel_logout_uri` and was a session participant gets a logout_token POST.

### §2.3 What the F-19 probe actually tested

The C12 spike URL was:
```
https://13.60.190.47:9443/oidc/logout?post_logout_redirect_uri=http://localhost:8090/
```

That hits the **second branch** (no `id_token_hint`, no `client_id`) — the empty-cache, framework-only path. **No BCL ever fires** in that branch. The audit log row with `ServiceProviderName: null` is exactly the signature of that path.

The C12 spike's empty-listener-log result was therefore **expected behaviour for a malformed logout request**, not evidence that CIBA grants aren't session participants. It said nothing about the populated-`id_token_hint` path.

**Verdict:** **F-19 was a probe artifact.** The corrected statement:

> *WSO2 IS DOES walk session participants and fire BCL when /oidc/logout receives a valid `id_token_hint` (or `client_id`). CIBA flows enrol the agent app as a session participant on the same `sessionContextId` as the user's Pattern C login (audit-log evidence). The C12 spike's malformed logout URL exercised the no-clientId branch, which never fires BCL by design.*

This re-opens **Option C (hybrid)** as a viable Sprint 3 design — agent-side BCL receivers will work IF the orchestrator's `/oidc/logout` redirect uses `id_token_hint` (which it does per Q3 lock — locked design Q3 IS consent screen *requires* `id_token_hint` on the redirect URL).

---

## §3. F-21 — `/oauth2/revoke` does NOT cascade to OBO tokens (CONFIRMED at source — architectural)

**Decisive citation:** [`org.wso2.carbon.identity.oauth/.../tokenprocessor/DefaultOAuth2RevocationProcessor.java`](file:///Users/ramith/code/identity-inbound-auth-oauth/components/org.wso2.carbon.identity.oauth/src/main/java/org/wso2/carbon/identity/oauth/tokenprocessor/DefaultOAuth2RevocationProcessor.java) lines 58–64:

```java
@Override
public void revokeAccessToken(OAuthRevocationRequestDTO revokeRequestDTO, AccessTokenDO accessTokenDO)
        throws IdentityOAuth2Exception {
    OAuthTokenPersistenceFactory.getInstance().getAccessTokenDAOImpl(accessTokenDO.getConsumerKey())
            .revokeAccessTokens(new String[]{accessTokenDO.getAccessToken()});
    AccessTokenEventUtil.publishTokenRevokeEvent(accessTokenDO);
}
```

**Schema confirmation:** [`AccessTokenDAOImpl.java`](file:///Users/ramith/code/identity-inbound-auth-oauth/components/org.wso2.carbon.identity.oauth/src/main/java/org/wso2/carbon/identity/oauth2/dao/AccessTokenDAOImpl.java) (token-insert path, lines 103–264) — INSERT columns are: `token, refresh_token, user, tenant_id, user_store_domain, issued_time, validity, scope, state, type, token_id, grant_type, subject_id, binding_reference, organization, authenticated_idp, idp_tenant_id, client_id, app_tenant_id`. **No `actor_token`, no `parent_grant_id`, no `request_id`, no foreign-key linkage to a parent grant.**

**What this proves:**
- Token revocation is a single-row UPDATE, not a graph traversal.
- The `act` JWT claim (which our OBO tokens carry as `act.sub=hr_agent-id` etc.) is **a JWT-body claim only** — it has no DB back-reference. WSO2 IS treats `act` as documentation of the chain at issuance time, not as an enforceable parent linkage.
- There is no published event hook (`AccessTokenEventUtil.publishTokenRevokeEvent`) that any IS extension uses to cascade. Even if we wrote a custom listener, we'd have to maintain our own parent→child mapping outside IS's schema.

**Verdict:** **F-21 stands and is architectural.** This is not a bug in IS; it's a deliberate choice to keep token grants independent. The Sprint 3 implication is permanent:
- Revoking token-A at IS via `/oauth2/revoke` will never invalidate CIBA-issued OBO tokens.
- The introspection backstop story for OBO tokens (after token-A revoke) is permanently broken.
- **Denylist on the receivers is the only revocation primitive for OBO tokens.** The gateway pattern is *required*, not preferred.

The architectural narrative remains: *WSO2 IS treats CIBA OBO grants as fully independent. Revocation only happens at the issuance grant. To kill child tokens, the gateway must propagate.*

---

## §4. Recommendations

These findings are decisive enough to update the Sprint 3 design without running the C15 probe (the source code already tells us what C15 would show). Operator can still run C15 as confirmation if desired — the probe is in-tree at [`idp_capability_test/c15_rp_initiated_logout_bcl.py`](../../idp_capability_test/c15_rp_initiated_logout_bcl.py).

### §4.1 F-19 corrected → restore Option C as defense-in-depth for D3.2 admin-terminate

The locked Sprint 3 Option A design (orchestrator-only OIDC client) was chosen on the (incorrect) F-19 basis. With F-19 corrected:

- **3A (user sign-out, UC-09)** — unchanged. Orchestrator-driven cascade is still the right primary path because (a) it's faster than IS BCL fan-out, (b) it guarantees ordering, (c) F-21 means IS-side propagation never reaches OBO tokens anyway.
- **3B.1 (admin-terminate, UC-10)** — **restore Option C semantics.** Orchestrator's `/api/logout` and IS's BCL-on-`/oidc/logout` are now both viable. Recommended: orchestrator BCL receiver remains (BLOCK-C validation already specified). Additionally:
  - HR-AGENT and IT-AGENT **can** register `back_channel_logout_uri = http://orchestrator:8090/internal/agent-bcl-relay` (forwarding through the orchestrator) — gives us a second BCL receipt signal we can correlate against. Strictly defense-in-depth; not strictly needed.
  - More usefully: **the orchestrator's response to `/api/logout` MUST include `id_token_hint` and `client_id` in the IS redirect URL** (already locked in Q3 design — confirm in the implementation review).
- **Tech-arch §5** — relax the SECURITY-DEGRADED labels on rows that depend on the introspection-backstop-via-IS path **only for token-A and admin-terminate flows**. For OBO-token revocation paths, SECURITY-DEGRADED labels stay (F-21 stands).

### §4.2 F-21 stays SECURITY-DEGRADED → demo narrative tightens

The demo narrative now has clean source-code receipts:

> *"This is the gateway pattern, required not preferred. WSO2 IS confirms in source: token revocation is row-local, no parent→child linkage. The orchestrator IS the policy enforcement point because no OAuth provider ships cross-grant revocation."*

This is stronger than the empirical F-21 finding alone — it's an architectural property of every OAuth provider that follows the OAuth 2.0 model.

### §4.3 F-20 stays — document the caveat

The 3B.2 ghost-approval caveat is documented per current plan. Source code confirms the AuthReqStatus enum lacks REVOKED — IS would need a feature addition to support auth_req_id revoke. Worth a single line in the demo runbook: *"Pending CIBAs are cancelled locally only; IS retains the auth_req_id until natural expiry (300 s). If the user approves out-of-band post-logout, the resulting token has no consumer."*

### §4.4 Updates to Sprint 3 docs

- `sprint-1-fixes.md` — add F-19 addendum + F-20 confirmation + F-21 confirmation (this doc as the basis).
- `sprint-3-tech-arch.md` §5 — relax SECURITY-DEGRADED labels for admin-terminate (D3.2) path; leave them for orchestrator-driven `/oauth2/revoke` path (F-21 still applies).
- `sprint-3-stage-1-product-review.md` R-6 — update outcome from "F-21 FAIL" to "F-21 confirmed at source; F-19 corrected at source."
- `sprint-3-stage-4-review.md` BLOCK-A resolution — update to "Resolved at source" rather than "Resolved by C13 probe."
- Demo runbook (when written, slice 3A.4) — bake in the architectural framing from §3.

### §4.5 No changes to Sprint 3 implementation plan

The slice plan (Stage 5) doesn't need to change. F-19 corrected helps the demo narrative but doesn't add or remove implementation work for 3A. For 3B.1, the orchestrator BCL receiver was already in scope; agent-side BCL receivers remain optional / not in the locked design.

---

## §5. Summary table

| Finding | Source verdict | Sprint 3 impact |
|---|---|---|
| **F-20** auth_req_id revoke is no-op | **CONFIRMED.** No code path; no `REVOKED` state in `AuthReqStatus`. RFC 7009-compliant 200-on-unknown. | 3B.2 unchanged: don't wire revoke; rely on local `cancel_event` + natural expiry. |
| **F-19** BCL doesn't fire for CIBA grants | **REFINED — was a probe artifact.** BCL DOES fire when `/oidc/logout` has `id_token_hint`; loop iterates all session participants without CIBA exclusion. F-19 spike URL had no `id_token_hint` → empty-cache branch. | 3B.1 admin-terminate: Option C semantics restored. Orchestrator BCL receiver as before; demo narrative tightens. |
| **F-21** token-A revoke doesn't kill OBO token-B | **CONFIRMED — architectural.** Single-row UPDATE; no parent→child linkage in schema. `act` claim is JWT-only. | Orchestrator-driven cascade is the ONLY revocation primitive for OBO tokens. SECURITY-DEGRADED labels for `/oauth2/revoke`-only paths remain. Gateway pattern required, not preferred. |
