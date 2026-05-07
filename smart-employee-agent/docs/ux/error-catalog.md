# Error Message Catalog — Smart Employee Agent POC

**Sprint:** 1 | **Stage:** 3 (UX) | **Date:** 2026-05-07
**Status:** Authoritative copy deck for Sprint 1/2/3. All literal user-facing strings live here.

---

## Preface

This catalog enumerates every recoverable and unrecoverable error condition surfaced by
the Smart Employee Agent POC (orchestrator + HR/IT specialists + WSO2 IS 7.2). It is
the single source of truth for both the user-facing copy and the operations log line.

**Style guide:**
- **User-facing strings** are friendly, plain-language, and actionable — no jargon, no
  framework names, no IDs longer than a 6-character correlation suffix. Every message
  pairs a problem statement with a suggested next step.
- **Ops log lines** are precise, single-line, structured, and searchable by `error_id`,
  `request_id`, `agent_id`, `session_id`. They name internals freely (client IDs, JWTs
  redacted to `Bearer ***`, internal hostnames, IS endpoints).

**Red lines (per `user-experience.md` §8):**
- Never expose a JWT, bearer token, or stack trace to the user.
- Never leak internal service URLs (`hr_agent:8001`, `https://13.60.190.47:9443`) into
  chat copy. Use friendly labels (`HR Agent`, `your identity provider`).
- Every user-facing error MUST be paired with a log line so ops can reconstruct the
  incident from `X-Request-ID`.

**Severity scale:**
- **Critical** — user must restart the session (sign in again).
- **High** — current action blocked; user can retry or rephrase.
- **Medium** — degraded mode (partial answer); user may continue.
- **Low** — informational; no user action required.

---

## Index

| Range | Category | Origin |
|---|---|---|
| `ERR-AUTH-001..010` | Login, session, token-A lifecycle | UC-01, UC-06 EX-2 |
| `ERR-CIBA-001..012` | Consent flow (initiate, poll, approve/deny, expiry) | UC-02 EX-3..6, UC-04, UC-05, UC-06 |
| `ERR-AGENT-001..006` | A2A, peer-trust, agent-card, routing | UC-02 EX-1, EX-2; UC-03 EX-4..5 |
| `ERR-MCP-001..005` | Backend tool failures, audience mismatch | UC-02 EX-7, EX-8 |
| `ERR-INFRA-001..005` | IS down, network, process death | UC-02 EX-3, UC-05 EX-4 |
| `ERR-LLM-001..004` | Hallucinated tool, routing failure | UC-02 EX-1 |

---

## ERR-AUTH-NNN — Login, session, token-A

| Error ID | Trigger | UC.EX | Severity | User-facing message | Suggested action | Log line | HTTP |
|---|---|---|---|---|---|---|---|
| `ERR-AUTH-001` | User clicks **Deny** on IS consent at login (`error=access_denied` on callback). | UC-01 EX-1 | High | "You did not approve the delegation. Sign in again to continue, or contact your administrator." | Click **Sign in** to retry. | `error_id=ERR-AUTH-001 phase=login outcome=user_denied state=<state> request_id=<rid>` | — |
| `ERR-AUTH-002` | `state` parameter missing or mismatched on `/auth/exchange`. | UC-01 EX-2 | High | "Sign-in flow could not be verified. Please try signing in again." | Click **Sign in** to retry. | `error_id=ERR-AUTH-002 phase=login outcome=state_mismatch session_pending=<bool> request_id=<rid>` | 400 |
| `ERR-AUTH-003` | IS returns `invalid_grant` (code reused / expired) on token exchange. | UC-01 EX-3 | High | "Your sign-in took too long. Please try again." | Click **Sign in** to retry. | `error_id=ERR-AUTH-003 phase=login outcome=invalid_grant retry_attempt=<n> request_id=<rid>` | 400 |
| `ERR-AUTH-004` | IS returns `unauthorized_client` for `orchestrator-mcp-client` (bad backend creds). | UC-01 EX-3 | Critical | "We're having trouble signing you in. Please contact your administrator." | Contact admin; do not retry. | `error_id=ERR-AUTH-004 phase=login outcome=unauthorized_client client_id=<id> remediation=check_secret request_id=<rid>` | 500 |
| `ERR-AUTH-005` | Orchestrator's `actor_token` (orchestrator-agent's I4 token) cannot be re-minted via App-Native Auth. | UC-01 EX-4 (failure path) | Critical | "We're having trouble signing you in. Please contact your administrator." | Contact admin. | `error_id=ERR-AUTH-005 phase=login outcome=actor_token_mint_failed agent=orchestrator-agent attempt=<n> request_id=<rid>` | 500 |
| `ERR-AUTH-006` | token-A signature fails JWKS validation (key rotation, tampered token). | UC-02 EX-2 (signature subset) | Critical | "Your session is no longer valid. Please sign in again." | Click **Sign in**. | `error_id=ERR-AUTH-006 phase=token_validate outcome=bad_signature jti=<jti> kid=<kid> request_id=<rid>` | 401 |
| `ERR-AUTH-007` | token-A `iss` does not match configured IS issuer. | UC-02 EX-2 | Critical | "Your session is no longer valid. Please sign in again." | Click **Sign in**. | `error_id=ERR-AUTH-007 phase=token_validate outcome=bad_issuer expected=<iss> got=<iss> request_id=<rid>` | 401 |
| `ERR-AUTH-008` | token-A is expired (`exp < now`) when reused mid-conversation. | UC-06 EX-2 | High | "Your session has expired. Please sign in again." | Click **Sign in**. | `error_id=ERR-AUTH-008 phase=token_validate outcome=expired session_id=<sid> exp=<ts> request_id=<rid>` | 401 |
| `ERR-AUTH-009` | Session cookie present but no matching session record (process restart, eviction). | UC-05 EX-4 | Critical | "Your session was reset. Please sign in again." | Click **Sign in**. | `error_id=ERR-AUTH-009 phase=session_lookup outcome=not_found cookie=<sid> request_id=<rid>` | 401 |
| `ERR-AUTH-010` | Sign-out invoked but `/oauth2/revoke` to IS fails (Sprint 3). | UC-06 / Sprint 3 D3.1 | Medium | "You're signed out. Some background cleanup is still in progress." | None — reload safe. | `error_id=ERR-AUTH-010 phase=sign_out outcome=revoke_failed token=token_a status=<http> request_id=<rid>` | 200 |

---

## ERR-CIBA-NNN — Consent flow

| Error ID | Trigger | UC.EX | Severity | User-facing message | Suggested action | Log line | HTTP |
|---|---|---|---|---|---|---|---|
| `ERR-CIBA-001` | `/oauth2/ciba` returns `unauthorized_client` (CIBA grant not enabled on Agent App). | UC-02 EX-3 | Critical | "HR Agent isn't fully set up yet. Please contact your administrator." (substitute IT Agent / agent_label) | Contact admin. | `error_id=ERR-CIBA-001 phase=ciba_initiate outcome=unauthorized_client agent=<id> remediation=enable_ciba_grant request_id=<rid>` | 500 |
| `ERR-CIBA-002` | `/oauth2/ciba` returns `invalid_request` citing notification channel. | UC-02 EX-3 | Critical | "HR Agent isn't fully set up yet. Please contact your administrator." | Contact admin. | `error_id=ERR-CIBA-002 phase=ciba_initiate outcome=invalid_request hint=notification_channel agent=<id> remediation=set_external request_id=<rid>` | 500 |
| `ERR-CIBA-003` | `/oauth2/ciba` returns `invalid_scope` for the requested scope set. | UC-02 EX-3 | High | "HR Agent can't request the access it needs right now. Please try a different question or contact your administrator." | Rephrase or contact admin. | `error_id=ERR-CIBA-003 phase=ciba_initiate outcome=invalid_scope scope=<set> agent=<id> request_id=<rid>` | 500 |
| `ERR-CIBA-004` | `/oauth2/ciba` returns `invalid_request` for malformed `login_hint` (user UUID could not be extracted from token-A). | UC-02 EX-3 | Critical | "We couldn't identify you to HR Agent. Please sign in again." | Click **Sign in**. | `error_id=ERR-CIBA-004 phase=ciba_initiate outcome=bad_login_hint sub=<redacted> agent=<id> request_id=<rid>` | 500 |
| `ERR-CIBA-005` | User clicks **Deny** on IS consent screen (single specialist). | UC-04 EX-1 / UC-02 EX-4 | Medium | "I couldn't access HR information for you (you declined the authorization). Ask again if you'd like to retry." | Ask again or rephrase. | `error_id=ERR-CIBA-005 phase=ciba_poll outcome=user_denied agent=<id> auth_req_id=<id> request_id=<rid>` | — |
| `ERR-CIBA-006` | User clicks **Deny** on FIRST specialist of a two-specialist serial query. Whole request aborted. | UC-04 EX-2 / UC-03 EX-1 | Medium | "Request cancelled — you declined HR access. Ask again if you'd like to retry." | Ask again. | `error_id=ERR-CIBA-006 phase=ciba_poll outcome=user_denied_first agent=<id> aborted_remaining=<list> request_id=<rid>` | — |
| `ERR-CIBA-007` | User approves HR, denies IT. Partial answer composed. | UC-04 EX-3 / UC-03 EX-2 | Low | "You have 12 days of leave. I couldn't pull asset info — you declined IT access." (LLM-composed; template controls the suffix) | None. Retry IT separately if needed. | `error_id=ERR-CIBA-007 phase=ciba_poll outcome=user_denied_partial granted=<agent> denied=<agent> request_id=<rid>` | — |
| `ERR-CIBA-008` | All specialists denied in a multi-step request. | UC-04 / UC-03 EX-3 / UC-03 EX (all-deny) | Medium | "Request cancelled — you declined the requested access." | Ask again. | `error_id=ERR-CIBA-008 phase=ciba_poll outcome=user_denied_all denied=<list> request_id=<rid>` | — |
| `ERR-CIBA-009` | `auth_req_id` expires before user clicks (>300s). IS returns `expired_token`. | UC-02 EX-6 / UC-05 main flow B (timeout) | Medium | "You took too long to approve. Please ask again." | Ask again. | `error_id=ERR-CIBA-009 phase=ciba_poll outcome=expired auth_req_id=<id> elapsed_s=<n> agent=<id> request_id=<rid>` | — |
| `ERR-CIBA-010` | User cancels via SPA Consent Widget before clicking Approve at IS (`/api/ciba/cancel`). | UC-04 Variant B | Medium | "Request cancelled. Ask again if you'd like to retry." | Ask again. | `error_id=ERR-CIBA-010 phase=ciba_cancel outcome=user_cancelled_widget agent=<id> auth_req_id=<id> request_id=<rid>` | 200 |
| `ERR-CIBA-011` | SSE channel drops while CIBA in flight; orchestrator cancels polling. | UC-05 Variant A | Low | (no user-facing message; user has closed browser) | (none) | `error_id=ERR-CIBA-011 phase=ciba_cancel outcome=session_disconnected agent=<id> auth_req_id=<id> elapsed_s=<n> request_id=<rid>` | — |
| `ERR-CIBA-012` | User clicks **Skip** on Session Refresh widget (UC-06). | UC-06 EX-1 | Medium | "Request cancelled — your access expired and you chose not to refresh." | Ask again to re-approve. | `error_id=ERR-CIBA-012 phase=ciba_cancel outcome=user_skipped_refresh agent=<id> prior_jti=<jti> request_id=<rid>` | 200 |

---

## ERR-AGENT-NNN — A2A, peer-trust, routing

| Error ID | Trigger | UC.EX | Severity | User-facing message | Suggested action | Log line | HTTP |
|---|---|---|---|---|---|---|---|
| `ERR-AGENT-001` | Inbound A2A request to specialist has malformed/missing token-A. | UC-02 EX-2 / N6 | Critical | "Authorization failed at HR Agent. Please contact your administrator." | Contact admin. | `error_id=ERR-AGENT-001 phase=a2a_inbound outcome=malformed_token agent=<id> request_id=<rid>` | 401 |
| `ERR-AGENT-002` | Inbound token-A's `act.sub` is not in the specialist's trusted-peer allowlist. | UC-02 EX-2 / N7 | Critical | "Authorization failed at HR Agent. Please contact your administrator." | Contact admin. | `error_id=ERR-AGENT-002 phase=a2a_inbound outcome=untrusted_peer act_sub=<id> allowlist=<list> agent=<id> request_id=<rid>` | 401 |
| `ERR-AGENT-003` | Inbound A2A request missing `X-Request-ID` header (Sprint 2 enforcement). | UC-03 / T6 / N26 | Medium | (transparent; orchestrator generates and warns in logs OR specialist refuses depending on Sprint 2 policy) | None. | `error_id=ERR-AGENT-003 phase=a2a_inbound outcome=missing_request_id agent=<id> remediation=generated_or_refused` | 400 (if refused) |
| `ERR-AGENT-004` | Specialist not found in agent-card registry (LLM picked an unknown specialist). | UC-02 EX-1 | High | "I'm sorry, I don't have a way to look that up." | Rephrase your question. | `error_id=ERR-AGENT-004 phase=routing outcome=unknown_agent agent_requested=<id> registry=<list> request_id=<rid>` | — |
| `ERR-AGENT-005` | Tool not declared in chosen specialist's agent-card. | UC-02 EX-1 | High | "I'm sorry, I don't have a way to look that up." | Rephrase. | `error_id=ERR-AGENT-005 phase=routing outcome=unknown_tool agent=<id> tool=<name> request_id=<rid>` | — |
| `ERR-AGENT-006` | User submits a second query while the first is still in flight. | UC-03 EX-5 / N24 | Low | "I'm still working on your previous request. Please wait a moment." | Wait. | `error_id=ERR-AGENT-006 phase=chat_input outcome=request_in_flight session_id=<sid> queued=<bool> request_id=<rid>` | 429 |

---

## ERR-MCP-NNN — Backend tool failures

| Error ID | Trigger | UC.EX | Severity | User-facing message | Suggested action | Log line | HTTP |
|---|---|---|---|---|---|---|---|
| `ERR-MCP-001` | MCP server rejects token-B with wrong `aud` (cross-specialist replay or config drift). | UC-02 EX-7 / N16, N21, N25, N28 | Critical | "HR Agent couldn't access the HR system. Please contact your administrator." | Contact admin. | `error_id=ERR-MCP-001 phase=mcp_call outcome=aud_mismatch expected_aud=<id> token_aud=<id> jti=<jti> request_id=<rid>` | 401 |
| `ERR-MCP-002` | MCP server rejects token-B because `act.sub` is not the paired agent. | UC-02 EX-7 / T5 | Critical | "HR Agent couldn't access the HR system. Please contact your administrator." | Contact admin. | `error_id=ERR-MCP-002 phase=mcp_call outcome=act_sub_mismatch expected=<id> got=<id> jti=<jti> request_id=<rid>` | 401 |
| `ERR-MCP-003` | MCP server rejects token-B because required scope is missing. | UC-02 EX-7 (scope subset) | High | "HR Agent doesn't have permission to do that yet. Try a different question." | Rephrase. | `error_id=ERR-MCP-003 phase=mcp_call outcome=insufficient_scope required=<scope> got=<scopes> jti=<jti> request_id=<rid>` | 403 |
| `ERR-MCP-004` | MCP server returns 5xx (backend bug, DB down). | UC-02 EX-8 | High | "HR system is unavailable. Try again in a moment." | Retry. | `error_id=ERR-MCP-004 phase=mcp_call outcome=backend_error agent=<id> mcp=<host> status=<http> request_id=<rid>` | 502 |
| `ERR-MCP-005` | MCP server unreachable (TCP refused / DNS / timeout). | UC-02 EX-8 | High | "HR system is unavailable. Try again in a moment." | Retry. | `error_id=ERR-MCP-005 phase=mcp_call outcome=unreachable agent=<id> mcp=<host> reason=<conn_refused\|timeout\|dns> request_id=<rid>` | 503 |

---

## ERR-INFRA-NNN — IS down, network, process

| Error ID | Trigger | UC.EX | Severity | User-facing message | Suggested action | Log line | HTTP |
|---|---|---|---|---|---|---|---|
| `ERR-INFRA-001` | WSO2 IS unreachable (TCP refused / DNS / timeout) at any phase. | UC-02 EX-3 / general | Critical | "We can't reach your identity provider right now. Please try again in a few minutes." | Retry later; contact admin if persistent. | `error_id=ERR-INFRA-001 phase=<phase> outcome=is_unreachable endpoint=<path> reason=<conn_refused\|timeout\|dns> request_id=<rid>` | 503 |
| `ERR-INFRA-002` | IS returns HTTP 5xx on `/oauth2/*` endpoints. | UC-02 EX-3 / general | Critical | "Your identity provider returned an error. Please try again in a few minutes." | Retry; contact admin if persistent. | `error_id=ERR-INFRA-002 phase=<phase> outcome=is_5xx endpoint=<path> status=<http> body_excerpt=<redacted> request_id=<rid>` | 502 |
| `ERR-INFRA-003` | TLS verification fails to IS (bad cert in non-dev env). | INFRA / T2 | Critical | "We couldn't securely connect to your identity provider. Please contact your administrator." | Contact admin. | `error_id=ERR-INFRA-003 phase=<phase> outcome=tls_verify_failed endpoint=<host> reason=<cert_expired\|hostname\|chain> request_id=<rid>` | 525 |
| `ERR-INFRA-004` | Orchestrator process restarted; in-memory session map lost (single-process per Q5). | UC-05 EX-4 | Critical | "Your session was reset. Please sign in again." | Click **Sign in**. | `error_id=ERR-INFRA-004 phase=session_lookup outcome=process_restart cookie=<sid> uptime_s=<n>` | 401 |
| `ERR-INFRA-005` | JWKS fetch fails or returns no keys when validating token. | INFRA | Critical | "We're having trouble verifying your sign-in. Please try again." | Retry; contact admin. | `error_id=ERR-INFRA-005 phase=token_validate outcome=jwks_failed endpoint=<jwks_url> reason=<http\|empty\|parse> request_id=<rid>` | 503 |

---

## ERR-LLM-NNN — LLM routing failures

| Error ID | Trigger | UC.EX | Severity | User-facing message | Suggested action | Log line | HTTP |
|---|---|---|---|---|---|---|---|
| `ERR-LLM-001` | LLM returns a tool call for a specialist that doesn't exist. Caught before A2A. | UC-02 EX-1 | High | "I'm sorry, I don't have a way to look that up." | Rephrase. | `error_id=ERR-LLM-001 phase=llm_route outcome=hallucinated_agent llm_output=<id> registry=<list> request_id=<rid>` | — |
| `ERR-LLM-002` | LLM returns a tool that exists but with wrong/missing arguments. | UC-02 EX-1 (arg subset) | High | "I had trouble understanding your request. Could you rephrase it?" | Rephrase. | `error_id=ERR-LLM-002 phase=llm_route outcome=bad_args agent=<id> tool=<name> validation=<msg> request_id=<rid>` | — |
| `ERR-LLM-003` | Gemini API itself returns 5xx / quota / network error; keyword fallback also disabled or doesn't match. | INFRA + LLM | High | "I'm having trouble thinking right now. Please try again in a moment." | Retry. | `error_id=ERR-LLM-003 phase=llm_route outcome=llm_unavailable provider=gemini status=<http> fallback_attempted=<bool> request_id=<rid>` | 503 |
| `ERR-LLM-004` | LLM produces output that is not a valid tool call (free-text when tool was expected, malformed JSON). | LLM | High | "I had trouble understanding your request. Could you rephrase it?" | Rephrase. | `error_id=ERR-LLM-004 phase=llm_route outcome=unparseable_output excerpt=<first_120_chars> request_id=<rid>` | — |

---

## Cross-cutting rules

### Logging contract (every error)

Every error in this catalog, when raised, MUST emit:

```
ts=<iso8601> level=ERROR error_id=<ERR-XXX-NNN> request_id=<X-Request-ID>
session_id=<sid> phase=<phase> outcome=<outcome> [agent_id=<id>]
[remediation=<hint>] msg="<short human description>"
```

Plus the category-specific fields named in each row. JWTs and `auth_req_id` values are
redacted to `***` per S1.11(c) baseline; only their `jti` (JWT ID) is logged for
correlation.

### User-facing message contract

- Begin with the problem in user terms ("HR Agent couldn't access…", "Your session has
  expired…").
- End with the next step ("Click **Sign in**.", "Ask again.", "Contact your
  administrator.").
- Use the agent's display label (`HR Agent`, `IT Agent`), never its internal ID
  (`hr_agent`, UUID).
- Use the phrase **your identity provider** (never "WSO2 IS", never an IP/hostname).
- Never include the `error_id`, `jti`, `auth_req_id`, or `request_id` short-form unless
  explicitly requested by the support flow (Sprint 2: a "show details" disclosure may
  expose the 6-character correlation suffix).

### Mapping to the consent widget visual states

| Widget state | Trigger error IDs |
|---|---|
| `AWAITING` (default) | — |
| `VERIFYING` (post-Approve) | — |
| `WORKING` (token received, MCP in flight) | — |
| `DONE` (success) | — |
| `DENIED` | `ERR-CIBA-005`, `-006`, `-007`, `-008` |
| `EXPIRED` | `ERR-CIBA-009` |
| `CANCELLED` | `ERR-CIBA-010`, `-011`, `-012` |
| `ERROR` (red banner, generic copy) | `ERR-CIBA-001..004`, `ERR-AGENT-001..002`, `ERR-MCP-001..005`, `ERR-INFRA-001..005` |

### Severity-to-channel mapping

| Severity | Where it surfaces |
|---|---|
| Critical | Replace chat with full-page friendly error; offer **Sign in** CTA where applicable. |
| High | Inline assistant message in chat; widget transitions to terminal state. |
| Medium | Inline assistant message; chat remains usable for a follow-up. |
| Low | Toast or transcript-line note; no interruption. |

---

## Coverage check

| Use case | Exception flows covered |
|---|---|
| UC-01 | EX-1 → ERR-AUTH-001; EX-2 → -002; EX-3 → -003/-004; EX-4 (failure) → -005 |
| UC-02 | EX-1 → ERR-LLM-001/-002, ERR-AGENT-004/-005; EX-2 → ERR-AGENT-001/-002, ERR-AUTH-006/-007; EX-3 → ERR-CIBA-001..004, ERR-INFRA-001/-002; EX-6 → ERR-CIBA-009; EX-7 → ERR-MCP-001/-002/-003; EX-8 → ERR-MCP-004/-005 |
| UC-03 | EX-1/-2/-3 → ERR-CIBA-005..008; EX-4 → ERR-MCP-004/-005; EX-5 → ERR-AGENT-006 |
| UC-04 | Variants A/B + EX-1..5 → ERR-CIBA-005..008, -010, -011 |
| UC-05 | Variants A/B + EX-1..4 → ERR-CIBA-009, -011; ERR-INFRA-004 |
| UC-06 | Main flow (refresh) → no error; EX-1 → ERR-CIBA-012; EX-2 → ERR-AUTH-008 |
| Threat model | T1/T8 (actor_token theft) — out-of-band; T2 → ERR-INFRA-003; T3 → preventive (no error); T4 → ERR-AGENT-002; T5 → ERR-MCP-001/-002; T6 → ERR-AGENT-003; T7 → ERR-AUTH-008 + UC-06; T9 → ERR-MCP-001 (N28) |

Total distinct error conditions: **42** (10 AUTH + 12 CIBA + 6 AGENT + 5 MCP + 5 INFRA + 4 LLM).

---

## Change log

- **2026-05-07** — v1 initial catalog, Sprint 1 Stage 3 deliverable. Aligned with
  milestone-plan v4 (CIBA architecture), UC-01..06, capability-memo F1..F7, threat model
  T1..T9.
