# Sprint 4 — Stage 8: Security Audit

**Stage:** 8 (security audit — pre-implementation team review)
**Date:** 2026-05-10
**Auditor:** security-auditor (independent)
**Branch (entry):** `sprint-3-build` @ `b497616`
**Inputs:** Stage 3 binding plan, Stage 4 UX, Stage 5 API, Stage 6 tech-arch, Stage 7 slice plan, UC-11..UC-16; Sprint 3 close + hardening (signoff, validators, redaction, scope-policy).

---

## 1. Verdict

**GO-WITH-CONDITIONS.**

Three P1 findings must be fixed in S4.0 / S4.4 before they can be marked done. No P0 was identified. The OQ-3 audience-list relaxation is sound under the locked design provided one defensive guard is added (see Finding F-01). The drop of the `groups` claim and `roles` field is safe because the server-side scope check has always been authoritative; the SPA's `is HR Admin` derivation from `scopes.includes("hr_approve_rest")` is defensible per `docs/scope-policy.md` §2 Rule 7.

---

## 2. Summary

Sprint 4 expands the surface materially: 11 new REST endpoints, 6 new MCP tools, browser-cookie-only access to admin reporting, and two new write scopes (`hr_assets_write_rest`, `it_assets_self_rest`). The architecture preserves the Sprint 3 trust model (token-A never leaves the orchestrator; SameSite=Strict cookie; per-tool scope check at the MCP boundary; F-04 validator + denylist). The most consequential design change is OQ-3 — the HR/IT REST validators accept a configurable audience list including the orchestrator's MCP Client ID, while the MCP-tool validator stays strict. The audit walked four hostile paths against this boundary and found no privilege escalation, but did identify three risks that must be addressed before S4.0 ships: an audience misconfiguration could broaden the allow-set silently; the new approve/reject POST endpoints inherit the chat path's cookie-only CSRF posture rather than the logout path's stricter `X-Request-ID` header guard; and `JWTClaims.username`/`email` flowing into `_AuthContext` and copy strings warrant the same redaction treatment as user-controlled input. None of these is structural; all are bounded fixes landing in S4.0 or S4.4.

---

## 3. Threat model deltas vs Sprint 3

Sprint 3 hardened the revocation surface: SameSite=Strict cookie, `X-Request-ID` CSRF header on `/auth/logout`, in-memory denylist with F-04 Step 7, JWKS prewarm, and the `RedactionFilter` extension (commit `f1515b5`). Sprint 4 introduces the following new attack surfaces:

- **D1. REST reporting endpoints with relaxed audience handling** (`hr_server/rest_api/server.py:42-51`, `it_server/rest_api/server.py` analogous). The REST validator `JWTValidator.validate_token` (`hr_server/auth/jwt_validator.py:88-147`) is constructed with a `list` audience via `pyjwt`'s `audience=list`. Sprint 4 adds an env var (`HR_SERVER_ACCEPT_ORCH_MCP_AUD` / `IT_SERVER_ACCEPT_ORCH_MCP_AUD`) that appends the orchestrator MCP Client ID to that list. The MCP tool validator (`hr_server/auth/validators.py:214` and the IT mirror) is unchanged — strict single-aud.
- **D2. Cookie-only browser auth on admin write paths.** A6/A7 (`POST /api/reports/leave-requests/{id}/approve|reject`) authenticate with the `orch_sid` cookie alone — no token presented by the browser, by design. The chat path already uses this pattern, but `/api/chat` does NOT enforce `X-Request-ID` (only `/auth/logout` does, see `orchestrator/auth/routes.py:573-576`). The new approve/reject path is therefore CSRF-defended only by SameSite=Strict on the session cookie + JSON content-type expectations. This is a defense-in-depth gap relative to the logout path.
- **D3. New write scope `hr_assets_write_rest`** with admin delegation semantics. CIBA initiation occurs at HR Agent; binding message + `action_text` are constructed agent-side from `lookup_employee` results — meaning `action_text` may include user-controlled (admin-typed) cubicle IDs and target usernames. This is a content-injection surface for the SPA copy (Finding F-08).
- **D4. Multi-turn cubicle chat carries human-typed strings (cubicle_id, employee_username, employee_email) through the orchestrator → agent → IS → consent widget → SPA pipeline.** The keyword fallback uses regex extraction (`orchestrator/chat/keyword_fallback.py:97`); the LLM path uses Gemini's tool-call JSON. Both paths must validate the extracted args before they become CIBA `binding_message` / `action_text`.
- **D5. `username` and `email` claims plumbed into `_AuthContext` and into chat-message copy** (e.g. "Approve jane.doe's leave from 2026-06-10"). If IS surface honours user-controlled SAML/JIT-provisioned attributes in the `username` claim, an attacker who controls a username could attempt UI / log injection. Confirmed below in §6 that this is bounded by IS' attribute-mapping behaviour but warrants a finding.
- **D6. SPA conditionally renders Reports admin nav based on `scopes.includes("hr_approve_rest")`.** Server-side authority is unchanged, but the SPA caches scopes from `/auth/exchange` for the session lifetime — it will not detect mid-session scope change (Finding F-09).

The data-at-rest surface is unchanged (in-memory only, no Redis ever).

---

## 4. Findings table

| ID | Sev | Finding | Recommendation | Landing slice |
|---|---|---|---|---|
| F-01 | P1 | OQ-3 audience-list relaxation has no defensive cap. `_audiences = [config.CLIENT_ID]` + `config.SPA_CLIENT_ID` + (`HR_SERVER_ACCEPT_ORCH_MCP_AUD` if set) at `hr_server/rest_api/server.py:42-51`. A misconfigured env var (e.g. left from another tenant; copy-paste error including extra IDs) silently broadens the trust set. The validator accepts the FIRST match against any list element (pyjwt semantics). | Add startup assertion: REST validator MUST log every accepted audience at INFO and MUST reject configurations where the list contains > 3 entries (the natural cap: own CLIENT_ID, optional SPA_CLIENT_ID, optional ORCH_MCP). Fail-closed if the cap is exceeded. Mirror `log_startup_assertion()` pattern from `hr_server/auth/validators.py:331-368`. | S4.0 |
| F-02 | P1 | Approve/Reject POST endpoints (A6/A7) inherit cookie-only auth without `X-Request-ID` requirement. `/auth/logout` enforces this (`orchestrator/auth/routes.py:573-576`); `/api/chat` does not. A6/A7 are admin-write surfaces that initiate CIBA — a CSRF that gets the admin to load a malicious page could fire approve/reject (subject to SameSite=Strict). SameSite=Strict is the primary defense, but defense-in-depth was the Sprint 3 standard for write paths. | Add `X-Request-ID` header requirement to A6/A7 mirroring `/auth/logout`. SPA already generates rids per request. Reject 400 if absent. Defense-in-depth against future browser vendor changes to SameSite enforcement. | S4.4 |
| F-03 | P1 | `username`/`email` claims flow from token into `_AuthContext.full_name` and into chat copy strings without server-side normalisation. `hr_server/rest_api/server.py:64-74` already does the `name`/`preferred_username` fallback. Sprint 4 adds `username` / `email` from new claims. If IS user attribute mapping honours user-edited profile fields (or JIT-provisioned SAML attrs), a malicious admin could set their username / email to a string containing line breaks, ANSI escapes, or ` `/` ` and have it land in (a) audit log lines, (b) `binding_message`, (c) `action_text` rendered into other admins' SPAs. SPA uses `textContent` (verified at `client/app.js:874`) so XSS is blocked, but log poisoning is not. | (a) Strip/replace control chars (`\x00-\x1F` except `\t`) and Unicode line separators in `_AuthContext.username`/`email`/`full_name` before they reach `logger.info(...)` or any `binding_message` constructor. (b) Cap length at 64 chars for username, 256 for email. (c) Document in `docs/wso2-is-setup.md` that operator-side IS attribute-mapping should not surface unverified user-edited fields as `username`. | S4.0 |
| F-04 | P2 | `assign_cubicle` idempotency rule has no per-request-ID dedupe. Same `(cubicle_id, employee_username)` returns existing record (Stage 5 §D3). Single-uvicorn-worker (BLOCK-I) makes the in-process dict access serial, so within one process there is no TOCTOU. But two concurrent admin browsers can both initiate CIBA for the same cubicle — IS issues two token-Cs, both reach `assign_cubicle`. The "different user" branch is the protective rejection. The "same user" branch is silently fine. Risk: two HR Admins racing for `(C-027, jane.doe)` will both observe success, but the second admin's CIBA was technically wasted. | Acceptable for Sprint 4 (in-memory, single worker, demo). Document the TOCTOU in code comment alongside the idempotency block. Stage 10 test R-CUBICLE-IDEM-CONCURRENT (already implied by Stage 5 §9 hook 3) should cover the racing case. | S4.1 (test only) |
| F-05 | P2 | `_derive_roles_and_scopes` decodes id_token + access_token without signature verification (`orchestrator/auth/routes.py:246-247, 266-267` already do this for display name; Stage 6 §8.2 extends the pattern). Justification at line 261-262: "the id_token arrived in the same /token response as the already-validated access token." Acceptable, but the post-amendment plan drops `groups`/`roles` derivation entirely, so the unverified-decode helper for groups is no longer needed. | Per the amendment, `_derive_roles_and_scopes` simplifies to `_derive_scopes` returning `token_a.scope.split()`. No JWT decode of id_token / access_token is needed for the scope list — IS' /token response already returned `scope` as a top-level field on the token-A response. Audit the Stage 6 implementation to confirm the scope list is read from the OAuth response field, NOT decoded from the JWT body. | S4.0 |
| F-06 | P2 | The "fallback derivation" at Stage 6 §8.4 (`if "hr_read_rest" in scopes or "hr_approve_rest" in scopes: roles = ["HR Admin"]`) is now obsolete after the amendment, but the comment still encodes the rule. Risk is operator confusion: a future engineer might re-add the `groups` claim path and find the fallback misleading. | Delete §8.4 from the spec or strike-through. SPA `isHrAdmin` derivation is `scopes.includes("hr_approve_rest")` per the amendment (the canonical HR-Admin-exclusive scope per `docs/scope-policy.md` §2). Update Stage 6 to match the amendment textually before S4.0 starts. | S4.0 |
| F-07 | P2 | `RedactionFilter` (`common/logging/redaction.py`) covers JWT, Bearer, auth_req_id, actor_token, client_secret, password, x-internal-auth. Sprint 4 adds new key=value pairs in logs: `username=jane.doe`, `email=jane.doe@example.com`, `cubicle_id=C-027`. None of these are secrets, but if a future log line emits a full IS user record (e.g. for a debug-level dump of `_SEED_USERS`), `sub` (a UUID) might leak. Sprint 3 hardening intentionally kept `sub` out of all surfaces (`docs/architecture/sprint-4.md` §7: "never displayed"). | Add a regex pattern to `RedactionFilter` for stray `sub=<uuid4>` values and `assigned_to_sub` keys. Cheap. Pre-empts a slip-up in S4.1 cubicle store debug logging. | S4.0 |
| F-08 | P2 | `action_text` is constructed by HR Agent and propagated through A2A → orchestrator → SSE → SPA. The string contains user-typed substrings (cubicle_id from regex extraction; employee_username from chat). If the LLM path produces `action_text="Assign cubicle C-027</p><script>alert(1)</script> to jane.doe"`, the SPA's `cw-action-text` element renders it via `textContent` (`client/app.js:874`) — so DOM injection is blocked. But log lines (e.g. `logger.info("ciba_url_pushed action=%s", action_text)`) would carry the payload unsanitised. | Server-side: when constructing `action_text` agent-side, restrict to the allowed character set (alphanum, dash, underscore, dot, space, apostrophe, comma) and reject otherwise. Reasonable upper bound: 256 chars. Same surface as F-03 control-char hardening. | S4.1 |
| F-09 | P2 | SPA caches `scopes` from `/auth/exchange` for session lifetime. If an admin's IS role is revoked mid-session (operator removes HR Admin role), the SPA still shows the Reports button + still permits A6/A7 clicks. Server-side enforcement catches it (token-A scope check), so the user gets 403 with the access-denied panel — but the click happens. | Acceptable for Sprint 4 demo scope. Document that mid-session role change requires sign-out / sign-in to update the SPA UI state. Server-side authority remains. | Document in `sprint-4-signoff.md` |
| F-10 | P2 | `scripts/check-is-config.sh` is operator-run at Stage 9 Day 1 + pre-Stage-11 (Stage 6 §9.3). It is NOT run in CI (depends on live IS). If skipped, the symptoms are: Reports nav doesn't appear (per amendment, scope-based gating); cubicle assign fails with 401 (`hr_assets_write_rest` not registered); `username` claim absent → 401 from D4/E1. All fail-closed; no silent privilege escalation. | Acceptable. Ensure runbook explicitly calls out the "operator skipped check" failure modes so operators recognise them. Already covered in Stage 6 risk AR-1/AR-2. | n/a (runbook) |
| F-11 | P2 | Multi-turn cubicle chat: the orchestrator does NOT track turn state (Stage 6 §6.5 — "intentionally simple"). A malicious admin could try to short-circuit by typing "assign C-027 to jane.doe" without the read-side dialogue. The CIBA requires `hr_assets_write_rest` regardless, so the security boundary holds; the worst case is the admin assigns a cubicle they didn't visually verify as vacant (in-memory race already covered by F-04). | Acceptable. Document in UC-11 that "skipping turns 1+2" is permitted from a security standpoint — the CIBA + scope check is the gate, not the chat dialogue. | n/a (doc note) |
| F-12 | P2 | `lookup_employee` MCP tool (D5) returns `sub` (the IS UUID) to the HR Agent when called via OBO with `hr_read_rest`. Per `sprint-4.md` §7, `sub` is "never surfaced to chat or UI." Risk: an HR Agent log line that echoes the lookup result (for debugging) could leak `sub`. | Code review checklist for S4.1: `hr_agent` logs of `lookup_employee` results MUST NOT include `sub`. F-07's redaction enhancement covers the stray-leak case. | S4.1 |

---

## 5. OQ-3 audit — full page

**Stage 6 lock (§2.3):** REST validator on HR + IT servers accepts a list of audiences (`config.CLIENT_ID`, optional `SPA_CLIENT_ID`, optional `HR_SERVER_ACCEPT_ORCH_MCP_AUD`/`IT_SERVER_ACCEPT_ORCH_MCP_AUD`). MCP-tool validator stays strict (single `expected_aud` = the agent's OAuth Client ID).

**Walking four hostile paths:**

### Path A — Token-A from orchestrator reaches an MCP tool

Token-A is issued at Pattern C login. Its `aud` is the orchestrator's MCP Client ID. The orchestrator only ever forwards token-A to REST endpoints (the new reporting paths). It does NOT forward token-A to MCP tool endpoints — MCP calls go through the agent CIBA path that mints token-C with `aud == hr_agent_client_id`.

If an attacker captured token-A and presented it to `POST /mcp/tools/assign_cubicle`, the strict MCP validator (`hr_server/auth/validators.py:214`) would reject at Step 4 (`aud != hr_agent_client_id`). **Verdict: blocked.** No additional defence needed.

### Path B — Token-C from an agent reaches a REST reporting endpoint

Token-C is issued at agent CIBA. Its `aud` is the agent's OAuth Client ID (e.g. `hr_agent_client_id`). The new REST validator's audience list is `[hr_server_own_client_id, optional spa_client_id, optional orch_mcp_client_id]` — **does NOT include `hr_agent_client_id`**.

If an attacker captured a token-C and presented it to `GET /api/reports/leave-requests`, pyjwt's audience check would fail. **Verdict: blocked.** Defence-in-depth: even if it passed, the scope on token-C is the tool-specific scope (e.g. `hr_assets_write_rest` for `assign_cubicle`), not `hr_read_rest`, so the REST handler's scope check would also fail.

The Stage 6 §2.3 test `tests/hr_server/rest_api/test_audience_segregation.py` codifies exactly this assertion (assertion 3). **Sign-off conditional on this test landing in S4.0.**

### Path C — Token from a different orchestrator (replay)

Suppose another deployment of the orchestrator MCP client (different tenant, different `client_id`) issues a token-A. The audience claim names that other client_id, which is NOT in the REST validator's list. **Verdict: blocked.** pyjwt audience check fails.

### Path D — Env-var misconfiguration broadens the audience list

This is the realistic attack: operator copies `HR_SERVER_ACCEPT_ORCH_MCP_AUD` from a stale environment, sets it to a wildcard, or appends multiple values. The audience list silently includes a foreign client_id. Now any token issued for that foreign client + bearing `hr_read_rest` reaches REST endpoints.

**Verdict: NOT blocked by code today.** The pyjwt validator accepts any of the listed audiences. There is no cap, no allowlist of permitted client_id formats, no startup assertion that flags an unusually large audience list. **Mitigation: F-01.** Cap at ≤ 3 entries; log every accepted audience at INFO; fail-closed if cap exceeded.

### Other OQ-3 considerations

- **Why NOT use a dedicated REST audience that's separate from the orchestrator MCP client?** This was rejected as OQ-3 option (b) Stage 5 §B. Stage 6 chose to reuse the orchestrator's MCP Client ID for minimum-diff. This is acceptable provided F-01 lands — the cap + log line provides operator-visibility into what the validator is accepting.
- **Does the REST validator enforce the same denylist (Step 7) as the MCP validator?** Inspecting `hr_server/auth/jwt_validator.py:88-147`: no. The REST validator does NOT consult the revocation state. This is by design — token-A revocation is handled at the orchestrator (the orchestrator's `LogoutHandler` calls `/oauth2/revoke` on token-A at logout) and the orchestrator stops forwarding token-A once the session is terminating (BLOCK-G). So a "revoked token-A" path only matters if an attacker captured token-A AND bypasses the orchestrator. This is the same caveat as Sprint 3's REST path; acceptable.

**OQ-3 verdict:** Sound boundary, conditional on F-01 (audience-list cap + startup log).

---

## 6. `username` trust audit

**Question:** Is the `username` claim trusted (signed by IS) or could it be spoofed?

**Findings:**

1. The `username` claim travels in the access token / id token. Signature verification (RS256, JWKS-backed) is enforced at every validator entry-point (`common/auth/jwt_validator.py:288-306` Step 2). An attacker without IS' private key cannot forge a token bearing arbitrary `username`. **Conclusion: cryptographically signed.**

2. **However**, signing only ensures IS issued the claim — it does NOT ensure the value is well-formed or unique. The risk is **upstream**: how does IS populate `username`? Reviewing `docs/scope-policy.md`, Stage 6 ask A3 ("`username` claim mapped into the access token claim set"), and `docs/wso2-is-setup.md` (referenced):
   - WSO2 IS 7.x sources `username` from the user store. For local users in IS, `username` is the login identifier (set at user creation, immutable in IS 7.x by default).
   - For JIT-provisioned users (SAML, federated IdP), `username` may be derived from a SAML assertion attribute. **This is the spoof surface.** A federated user can theoretically register with a `username` chosen at the SAML side.

3. For Sprint 4's demo: WSO2 IS 7.x at `13.60.190.47:9443` is configured with local users (`employee_user`, `hr_admin_user`, plus seed accounts). No federation. **Conclusion: in the demo deployment, `username` is operator-set and trustworthy.**

4. Per the amendment, the SPA derives `isHrAdmin` from `scopes.includes("hr_approve_rest")`, NOT from `username`. So even if `username` were spoofable, it does NOT affect role gating. **The trust dependency on `username` is bounded to:**
   - Display in chat copy ("Approve jane.doe's leave...")
   - Lookup key in `_SEED_USERS` (cubicle / asset assignment target)
   - Audit log lines

5. F-03 covers the residual risk (control-char / length sanitisation before logging or rendering).

**Conclusion: trust is sound for the demo deployment provided F-03 lands.** A note in `docs/wso2-is-setup.md` advising operators against federating user stores until the username sanitisation hardening is in place would be valuable; current copy already documents this as a Stage 9 ask.

---

## 7. Cross-cutting concerns

### 7.1 Token redaction in logs

`common/logging/redaction.py` covers JWT, Bearer, auth_req_id, actor_token, client_secret, password, x-internal-auth. Sprint 4's new fields:

- `username` — not a secret, not redacted. Safe.
- `email` — not a secret per project posture (already in pre-IdP audit logs); not redacted. Safe.
- `action_text` — composed from username + cubicle_id + dates. Not a secret, but contains user-controlled substrings. F-08 recommends server-side sanitisation; the redaction filter is not the right place.
- `cubicle_id` — public.
- `sub` (in `lookup_employee` results) — UUID; not a secret per OAuth conventions, but `sprint-4.md` §7 explicitly says "never surfaced." F-07 recommends adding a redaction pattern as defence-in-depth.

**Verdict: acceptable. Land F-07 to pre-empt slip-ups in S4.1.**

### 7.2 CSRF

- Session cookie: `SameSite=Strict, HttpOnly, Secure` (verified at `orchestrator/auth/routes.py:522-528`).
- `/auth/logout`: requires `X-Request-ID` header (verified at `orchestrator/auth/routes.py:572-576`).
- `/api/chat`: cookie-only; no `X-Request-ID` requirement; relies on JSON body + SameSite=Strict.
- `/auth/exchange`: SPA generates and sends `X-Request-ID` (`orchestrator/auth/routes.py:673` per the embedded relay HTML).

**New endpoints (A6, A7):** Stage 5 / 6 do not specify CSRF posture. They inherit cookie-only. **F-02 recommends matching `/auth/logout`'s `X-Request-ID` header guard** for write paths.

### 7.3 XSS via `action_text`

`client/app.js:874` (`$("cw-action-text").textContent = actionText;`) uses `textContent`, which prevents HTML/script injection via DOM. Other `action_text` consumers (verified by Grep on `action_text|innerHTML`) are all `textContent`-based. **Verdict: blocked.** No SPA changes required for XSS, only F-08 server-side sanitisation for log hygiene + binding-message cleanliness.

The amber-tint class toggle in Stage 4 §6 uses `toggleAttribute` and `data-write-action` — also DOM-safe.

### 7.4 SSE event injection

The `ciba_url` event payload is constructed server-side from validated A2A `ConsentRequiredPayload` (`common/a2a/models.py`). The agent constructs `action_text` from the validated tool arguments. The SSE channel serialises as JSON via Pydantic (`orchestrator/events/sse.py`). No raw user input is concatenated into the SSE wire format outside of the JSON-encoded payload field. **Verdict: blocked at the wire format layer.** F-08 covers the action_text content sanitisation.

The `/events/{session_id}` route enforces `path_session_id == cookie.orch_sid` (Sprint 1 F-06, referenced at `orchestrator/events/sse.py:13-14`). A cross-tenant SSE subscription is therefore not possible.

### 7.5 My Leaves panel cross-user data leak

Stage 5 §B1 + §A1: orchestrator extracts `sub` from `session.token_a` claims and forwards token-A to HR Server. HR Server B1 calls `hr_service.get_my_leave_requests(claims.sub)` which filters at `hr_server/service/hr_service.py:67-68` (`if req["user_sub"] == sub`). **Conclusion: per-user filtering enforced server-side; cookie capture would only expose the owning user's leaves.** No cross-user leak surface.

### 7.6 Reports endpoint scope boundary (defence-in-depth)

Stage 5 §A3-A5 specify orchestrator pre-flight scope check (`hr_read_rest` / `it_assets_read_rest`) plus HR/IT Server scope check on token-A (`_require_scope` at `hr_server/rest_api/server.py:118-134`). **Verified: defence-in-depth enforced at both layers.** Stage 5 §9 hook 2 mandates a test that asserts orchestrator pre-flight blocks before backend contact.

---

## 8. Recommended security tests for Stage 10

1. **R-AUD-1 — Audience-list cap.** Construct an HR Server config with 4+ audiences. Assert startup fails (or logs an alarm and refuses to start). Mirror for IT Server. Ties to F-01.
2. **R-AUD-2 — Audience startup log line.** Assert `validator.startup expected_audiences=[a, b, c]` (or equivalent) appears at INFO level on every cold start. Operators grep for it during Stage 9 Day-1 verification.
3. **R-CSRF-1 — Approve/Reject without `X-Request-ID`.** Assert A6/A7 return 400 when the header is absent. Ties to F-02.
4. **R-USERNAME-1 — Control-char sanitisation.** Construct a synthetic token whose `username` claim is `"jane\ndoe \x07"`. Assert `_AuthContext.username` is the cleaned form, and that `logger.info("...username=%s...", ctx.username)` produces a single-line log entry. Ties to F-03.
5. **R-USERNAME-2 — `username` length cap.** Token with `username` = 1000 chars. Assert `_AuthContext.username` is truncated to 64 chars. Ties to F-03.
6. **R-AUDIT-1 — Audience segregation.** Token-A presented to `/api/reports/leave-requests` → 200. Same token presented to `/mcp/tools/assign_cubicle` → 401. Token-C presented to `/api/reports/leave-requests` → 401. Ties to OQ-3.
7. **R-CUBICLE-CONCURRENT — TOCTOU race.** Two concurrent `assign_cubicle` calls for the same cubicle, different employees, must result in exactly one success and one `cubicle_already_occupied` error. Ties to F-04.
8. **R-ACTIONTEXT-1 — Action_text injection.** Construct a `lookup_employee` response with `username = "<script>alert(1)</script>"`. Assert HR Agent's `action_text` either rejects the result or sanitises it. Assert SPA `cw-action-text` rendering uses `textContent`. Ties to F-08.
9. **R-REDACTION-1 — `sub` redaction in logs.** Assert that a log line containing `sub=<uuid4>` is redacted to `sub=<REDACTED>` after the filter runs. Ties to F-07.
10. **R-AUTHCLAIM-1 — Missing `username` fail-closed.** Synthetic token without `username` to D4 / E1 → 401, NOT 200 with sub-fallback.

---

## 9. Sign-off conditions

### Must-fix-before-S4.0 kickoff

- **F-01** — Cap audience list at ≤ 3 entries; emit startup log enumerating each accepted audience; fail-closed on cap breach. Test: R-AUD-1, R-AUD-2.
- **F-03** — Sanitise `username` / `email` / derived `full_name` for control chars + Unicode line separators + length cap (64 / 256). Test: R-USERNAME-1, R-USERNAME-2.
- **F-05 / F-06** — Update Stage 6 doc to reflect the `groups` drop amendment textually. Cosmetic but eliminates operator confusion.

### Must-fix-before-S4.4 kickoff

- **F-02** — Add `X-Request-ID` header guard to A6/A7 mirroring `/auth/logout`. Test: R-CSRF-1.
- **F-08** — Sanitise `action_text` server-side: charset whitelist + length cap before A2A propagation. Test: R-ACTIONTEXT-1.

### Nice-to-have (recommended but not blocking)

- **F-07** — Add `sub` UUID + `assigned_to_sub` patterns to `RedactionFilter`. Lands cleanly in S4.0.
- **F-09** — Document mid-session role-change behaviour in `sprint-4-signoff.md`. No code change.
- **F-11** — Document "skipping turns 1+2 is OK" in UC-11 security note. No code change.
- **F-12** — Code-review checklist item for S4.1 `lookup_employee` log lines. No code change.

### Not blocking

- **F-04** — TOCTOU on `assign_cubicle` (acceptable for Sprint 4 demo scope; documented).
- **F-10** — Pre-flight script skipped (fail-closed symptoms; documented).
