# Sprint 3 — Technical architecture sketch

**Sprint:** 3 (Stage 3 deliverable)
**Date:** 2026-05-09
**Author:** ai-engineer (drafted), to be reviewed in Stage 4 by architect-reviewer + security-engineer + ux-researcher + ai-engineer.
**Inputs:** [Stage 1 review](sprint-3-stage-1-product-review.md), [UC-09](../use-cases/UC-09-logout-cascade.md), [UC-10](../use-cases/UC-10-admin-terminate.md), [F-19](sprint-1-fixes.md), [brainstorm doc](../spikes/sprint-3-logout-design-brainstorm.md).

This document is the technical specification of the locked Option A design. It feeds Stage 4 (multi-agent review) and Stage 5 (slice plan). Implementation must not start until Stage 4 sign-off.

---

## §1. Sequence diagrams

### §1.1 D3.1 — User sign-out cascade (UC-09 main flow)

**Stage 4 corrections applied:** ordering rewritten per BLOCK-F (cancel before fan-out) and BLOCK-G (`Session.terminating` flag for snapshot atomicity). Per-`user_sub` `asyncio.Lock` per FIX-12.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant SPA as "SPA (3001)"
    participant Orch as "Orchestrator (8090)"
    participant IS as "WSO2 IS (9443)"
    participant HR as "HR-AGENT (8001)"
    participant IT as "IT-AGENT (8002)"
    participant HRMCP as "hr_server (8000)"
    participant ITMCP as "it_server (8004)"

    User->>SPA: click "Sign Out"
    SPA->>Orch: POST /auth/logout (cookie, X-Request-ID required per FIX-9)

    Note right of Orch: Acquire per-user_sub asyncio.Lock<br/>(serialises concurrent UC-09/UC-10 races per FIX-12)

    Orch->>Orch: Session.terminating = True<br/>(rejects new CIBA/chat with 401 — BLOCK-G)
    Orch->>Orch: snapshot: token_a, [(agent_id, jti, exp)…], pending_ciba

    par Cancel pending CIBAs FIRST (BLOCK-F)
        Orch->>Orch: pending[*].cancel_event.set()
        Orch->>Orch: await cancelled_ack barrier (≤100 ms)
    end

    Orch->>IS: POST /oauth2/revoke (token=token_a)
    IS-->>Orch: 200

    par Internal cache-bust fan-out (4 receivers, parallel; inline retry-once @ 200ms per FIX-22)
        Orch->>HR: POST /internal/events {type, subject:{sub,jti}, exp, reason} (FIX-7, FIX-19)
        HR->>HR: revoked_jtis.add(jti, exp) + drop _token_cache_by_jti[jti]
        HR-->>Orch: 200
    and
        Orch->>IT: POST /internal/events
        IT->>IT: revoked_jtis.add(jti, exp) + drop cache
        IT-->>Orch: 200
    and
        Orch->>HRMCP: POST /internal/events
        HRMCP->>HRMCP: revoked_jtis.add(jti, exp)
        HRMCP-->>Orch: 200
    and
        Orch->>ITMCP: POST /internal/events
        ITMCP->>ITMCP: revoked_jtis.add(jti, exp)
        ITMCP-->>Orch: 200
    end

    Orch->>Orch: clear orch_sid cookie + session_store.delete()
    Orch->>Orch: release per-user_sub Lock
    Orch-->>SPA: 200 {redirect_url: "<IS /oidc/logout?id_token_hint=…&state=…>"}

    SPA->>SPA: spinner phase 2: "Redirecting to complete sign-out…" (FIX-14)
    SPA->>SPA: window.location.href = redirect_url
    SPA->>IS: GET /oidc/logout?id_token_hint=…
    IS-->>User: render "Yes, sign me out" consent
    User->>IS: click Confirm
    IS->>IS: clear user session
    IS-->>SPA: 302 to /?reason=signed_out
    SPA->>User: login page + "You have been signed out" banner

    Note over SPA,User: If user clicks Cancel at IS instead: SPA renders<br/>"?reason=signed_out_partial" banner (BLOCK-E / EX-5)
```

**Ordering invariant (Stage 4 lock):**
1. Acquire user_sub lock.
2. Set `Session.terminating = True` (the snapshot fence).
3. Snapshot session state.
4. Cancel pending CIBAs + await `cancelled_ack` barrier.
5. Revoke token-A at IS.
6. Fan-out to all 4 receivers in parallel with inline retry-once.
7. Clear cookie + delete Session entry (the very LAST mutation).
8. Release lock.
9. Return redirect_url.

**Latency budget for steps 1–9 (server-side cascade):** ≤2 s (R-LOGOUT-1..4). Worst-case backstop window for missed fan-out: ≤20 s (Stage 5 L-3 introspection TTL). Steps 10–14 (IS consent + redirect) are user-paced.

### §1.2 D3.2 — Admin-terminate (UC-10 main flow)

```mermaid
sequenceDiagram
    autonumber
    actor Admin
    participant ISCon as "IS Console"
    participant IS as "WSO2 IS"
    participant Tunnel as "reverse-SSH tunnel<br/>(C12 rig, kept on)"
    participant Orch as "Orchestrator BCL receiver"
    participant HR as "HR-AGENT"
    participant IT as "IT-AGENT"
    participant HRMCP as "hr_server"
    participant ITMCP as "it_server"
    participant SPA as "SPA (online user)"

    Admin->>ISCon: User Management → Active Sessions → Terminate
    ISCon->>IS: terminate(user_sub)
    IS->>IS: walk user-session table; orchestrator-mcp-client has back_channel_logout_uri
    Note right of IS: Per F-19, agent apps NOT in user-session table → no BCL to them

    IS->>Tunnel: POST /backchannel-logout (logout_token=JWT)
    Tunnel->>Orch: POST /backchannel-logout
    Orch->>Orch: validate logout_token (sig, iss, aud, iat, events claim)
    Orch-->>IS: 200

    Orch->>Orch: acquire per-user_sub asyncio.Lock (FIX-12)
    Orch->>Orch: for each Session in user_sub: Session.terminating = True
    Orch->>Orch: snapshot pending_ciba + (agent_id, jti, exp) per session
    Orch->>Orch: pending[*].cancel_event.set() + await cancelled_ack barrier (BLOCK-F)
    Orch->>IS: POST /oauth2/revoke (token=token_a)  [defense-in-depth]

    par 4-receiver fan-out (same shape as UC-09)
        Orch->>HR: POST /internal/events
        Orch->>IT: POST /internal/events
        Orch->>HRMCP: POST /internal/events
        Orch->>ITMCP: POST /internal/events
    end

    alt User has open SPA
        Orch->>SPA: SSE event {type: "session_terminated", reason: "admin_terminated"}
        Orch->>Orch: await SSE channel.flushed (50–100 ms drain) (BLOCK-H)
        SPA->>SPA: clear localStorage; navigate /?reason=admin_terminated
    end

    Orch->>Orch: remove Session entries (LAST mutation; BLOCK-H)
    Orch->>Orch: release lock
```

**Reason precedence (FIX-12):** if UC-09 and UC-10 race, `admin_terminated` wins for SSE/banner emission and audit reason. The lock serialises; the second path observes Session already removed, short-circuits, but still emits `WARN cascade_already_run reason=user_signed_out` for the audit chain.

**Latency budget D3.2:** ≤5 s end-to-end (network + IS BCL + orchestrator cascade). Orchestrator's portion ≤2 s.

---

## §2. Component changes by file

### Orchestrator

| File | Change | Stage |
|---|---|---|
| [`orchestrator/auth/routes.py`](../../orchestrator/auth/routes.py) | Replace `POST /auth/logout` body with the locked five-step flow (G-1, G-2). Returns JSON `{redirect_url}`. | 6 (3A.1) |
| [`orchestrator/auth/`](../../orchestrator/auth/) (new file `revocation.py`) | New module: `revoke_token_a(token_a)`, `fan_out_revoke(jti, user_sub)`, `cancel_pending_ciba(session)`. Used by `auth/routes.py` and the BCL receiver. | 6 (3A.1, 3A.2) |
| [`orchestrator/main.py`](../../orchestrator/main.py) | Add `POST /backchannel-logout` route (D3.2). Wire `revocation.fan_out_revoke()` into both logout paths. | 6 (3B.2) |
| [`orchestrator/main.py`](../../orchestrator/main.py) | Add SSE event type `session_terminated`. Emit on admin-terminate path. | 6 (3B.2) |
| `orchestrator/agent_registry/` (new method on `A2AClient` or new `RevokeClient`) | HTTP client for `POST /internal/revoke`. Includes the shared-secret header. Retries once with 200 ms back-off. | 6 (3A.2) |
| Session map (existing in `session_store.py`) | Add a method `iter_jtis_for_user(user_sub) → [(agent_id, jti)…]`. Already has the data per S1.11. | 6 (3A.2) |

### Specialist agents (HR-AGENT, IT-AGENT)

| File | Change | Stage |
|---|---|---|
| [`hr-agent/main.py`](../../hr-agent/main.py), [`it-agent/main.py`](../../it-agent/main.py) | Add `POST /internal/revoke` endpoint. Validates shared secret. Calls `dispatcher.revoke_jti(jti, user_sub)`. | 6 (3A.2) |
| `hr-agent/ciba/orchestrator.py`, `it-agent/ciba/orchestrator.py` | Add `denylist: set[str]` (in-process). Add secondary index `_jti_to_cache_key: dict[str, tuple[str, str]]` so `revoke_jti(jti)` is O(1). Add `revoke_jti()` method: denylist.add + pop cache entry. | 6 (3A.2) |
| Same files | Modify `dispatch()`: before returning a cached token, check `jti in denylist` → if true, treat as cache miss + log. | 6 (3A.2) |
| Same files | Periodic sweep task: every 5 min, drop denylist entries where `now > exp` (parsed from JWT). Memory bound. | 6 (3A.2) |

### MCP servers (hr_server, it_server)

| File | Change | Stage |
|---|---|---|
| [`hr-server/auth/validators.py`](../../hr-server/auth/validators.py), [`it-server/auth/validators.py`](../../it-server/auth/validators.py) | After existing 6-step JWT validation, check `jti in denylist`. If true → 401 `ERR-MCP-002`. | 6 (3A.3) |
| Same files | Add introspection step: if `jti not in denylist`, check `introspection_cache[jti]`; on miss, call IS `/oauth2/introspect`; cache result for 60 s positive (active=true) / forever for negative (active=false). On `active=false` → 401. | 6 (3A.3) |
| `hr-server/main.py`, `it-server/main.py` | Add `POST /internal/revoke` endpoint. Validates shared secret. denylist.add(jti). Return 200. | 6 (3A.3) |
| Same files | Add `denylist: set[str]` and `introspection_cache: dict[str, tuple[bool, float]]` as module-level state (Q5 single-process accepted). Periodic sweep same as agents. | 6 (3A.3) |

### SPA

| File | Change | Stage |
|---|---|---|
| [`client/app.js`](../../client/app.js) `performSignOut()` | After `POST /auth/logout`, read response JSON `{redirect_url}` and `window.location.href = redirect_url`. Drop today's hard-coded `/?reason=signed_out` (G-10). | 6 (3A.1) |
| [`client/app.js`](../../client/app.js) SSE handler | Handle `session_terminated` event: clear `localStorage`, navigate to `/?reason=admin_terminated`. | 6 (3B.2) |
| [`client/index.html`](../../client/index.html) login page | Add banner branch for `?reason=admin_terminated` (amber, distinct from `signed_out`). | 6 (3B.2) |

---

## §3. API contracts

### §3.1 `POST /auth/logout` (orchestrator)

**Existing route. Body of handler is replaced. CSRF protection added per FIX-9.**

Request:
```http
POST /auth/logout HTTP/1.1
Cookie: orch_sid=<session_id>
X-Request-ID: <rid>           # REQUIRED (FIX-9): cross-site form POSTs cannot set custom headers
```

Server rejects with 400 if `X-Request-ID` is absent. SPA already sends this header per `client/app.js`. Cookie hardened from `SameSite=Lax` to `SameSite=Strict` (FIX-9). The custom-header requirement + Strict cookie is the CSRF defense; no token round-trip needed.

Response (success):
```http
200 OK
Set-Cookie: orch_sid=; Max-Age=0; HttpOnly; SameSite=Strict
Content-Type: application/json

{
  "redirect_url": "https://13.60.190.47:9443/oidc/logout?id_token_hint=<jwt>&post_logout_redirect_uri=http%3A%2F%2Flocalhost%3A8090%2F%3Freason%3Dsigned_out&client_id=BO4LfSkkUOWnl7YgJNZcGiABW5ka&state=<csrf-nonce>"
}
```

The `state` parameter is stored in a short-lived (60 s) per-session map and validated when the user returns to `/?reason=signed_out` (NIT-5). Without this binding, `state` is decorative.

Response (no session):
```http
200 OK
Content-Type: application/json

{"redirect_url": "/"}
```

Response (timeout / 5xx — for SPA error path FIX-16):
SPA sets a 10 s client-side timeout on the fetch. On timeout/5xx, the SPA renders the error banner (copy-deck row 8.10) and clears the local cookie regardless. (No 4xx defined — logout is idempotent server-side.)

### §3.2 `POST /internal/events` (× 4 receivers) — renamed per FIX-7 (CAEP-aligned)

Same shape on all four receivers. Loopback-only (docker-compose internal network — see §4.4 process model). Body shape pre-aligned with the CAEP `subject` shape so the production-roadmap migration to a SET JWT does not require renaming the route.

Request:
```http
POST /internal/events HTTP/1.1
Host: hr-agent:8001
X-Internal-Auth: <INTERNAL_REVOKE_SHARED_SECRET>   # NIT-1: dedicated header, not Authorization: Bearer
Content-Type: application/json
X-Request-ID: <rid>

{
  "type": "session-revoked",
  "subject": {
    "sub": "<user uuid>",
    "jti": "<jti-of-token-being-revoked>"
  },
  "exp": 1746825600,                  // FIX-19: receiver needs exp for sweep; can't derive from jti alone
  "reason": "user_signed_out" | "admin_terminated"
}
```

**Multi-session handling (FIX-20):** when the orchestrator has multiple `Session` entries for the same `user_sub` (multi-browser / multi-device), it loops `for session in sessions: fan_out(session.jti)` — i.e. one POST per (session, agent) pair. The receiver doesn't need to know about sessions; it just adds each jti.

Response (success):
```http
200 OK
Content-Type: application/json
{"acked": true}
```

Response (auth failure):
```http
401 Unauthorized
{"error": "invalid_secret"}
```

Response (already revoked / unknown jti — idempotent):
```http
200 OK
{"acked": true, "note": "jti not in cache; added to denylist"}
```

**Auth model (BLOCK-B locked simple):** static shared secret in `INTERNAL_REVOKE_SHARED_SECRET` env, set at compose-up time, distributed via env-only (never committed, never logged). Receiver sockets bind to docker-internal interfaces (see §4.4). Per-IP token-bucket rate limit (100 req/min per source) prevents accidental flood. **Production upgrade path:** OAuth client_credentials grant with scope `revoke:jti` against a dedicated internal IS app, OR mTLS via a compose-issued CA. Sprint 4+.

### §3.3 `POST /backchannel-logout` (orchestrator only)

Per OIDC Back-Channel Logout 1.0 §2.5–2.7. **Stage 4 hardening (BLOCK-C, FIX-11) applied — full spec coverage required.**

Request:
```http
POST /backchannel-logout HTTP/1.1
Content-Type: application/x-www-form-urlencoded

logout_token=<JWT>
```

**Validation (all 9 checks REQUIRED — gaps are forgery vectors):**

1. **JWS signature** via JWKS at `https://13.60.190.47:9443/oauth2/jwks` — reuse [`common/auth/jwt_validator.py`](../../common/auth/jwt_validator.py) (do NOT roll a new validator). Cert pinning posture matches existing JWT validation. On `kid` not in JWKS cache → refetch JWKS once before failing.
2. **`alg` allow-list** — `RS256` only. Reject `none`, `HS256`, and anything else. (Defense against the classic RSA-public-key-as-HMAC-secret forgery.) Reuse the access-token validator's allow-list.
3. **`typ` header MUST be `logout+jwt`** — without this, ANY RS256-signed JWT for `aud=orchestrator-mcp-client` (id_token, access_token, leaked id_tokens from prior logins) can be replayed as a logout_token. **Critical.**
4. **`iss` exact-match** = `https://13.60.190.47:9443/oauth2/token`.
5. **`aud` exact-match** = `orchestrator-mcp-client`.
6. **`iat`** ≤ `now` ≤ `iat + 300` seconds.
7. **`events` claim** present and contains the URI `http://schemas.openid.net/event/backchannel-logout`.
8. **`nonce` MUST be absent.**
9. **At least one of `sub` or `sid`.** If `sid` is present without `sub`, resolve `sid → sub` via the orchestrator's reverse index (populated at code-exchange time from `id_token.sid`). If neither `sub` nor a resolvable `sid` is available → 400.

**Replay protection** (FIX-3): bounded `_seen_logout_jtis: dict[jti, iat]`; sweep entries where `iat + 300 < now` (beyond the freshness window the same jti can no longer be replayed by spec). Hard cap 10k entries with FIFO eviction and WARN.

Response (success):
```http
200 OK
Cache-Control: no-store
```

Response (validation failure):
```http
400 Bad Request
{"error": "invalid_logout_token", "detail": "<reason>"}
```

(IS doesn't normally retry on 4xx.)

---

## §4. Data structures

### §4.1 Per-agent denylist + jti index — wrapped in injectable `RevocationState` (FIX-2, FIX-4)

The denylist primitive lives in `common/revocation/jti_denylist.py` (FIX-3 — one implementation, not 4 copies). All four receivers (HR-AGENT, IT-AGENT, hr_server, it_server) import the same class.

```python
# common/revocation/jti_denylist.py — new shared module

class JtiDenylist:
    """Bounded, swept set of revoked jtis. Single-process; multi-worker breaks correctness (see §4.4)."""

    HARD_CAP = 10_000
    SWEEP_INTERVAL_S = 300

    def __init__(self) -> None:
        self._items: dict[str, float] = {}  # jti → exp epoch (FIX-19: receiver gets exp on the wire)
        self._sweep_task: asyncio.Task | None = None

    def add(self, jti: str, exp: float) -> None:
        if len(self._items) >= self.HARD_CAP:
            # FIFO eviction + WARN (FIX-13)
            oldest = next(iter(self._items))
            del self._items[oldest]
            log.warning("denylist_evicted jti=%s reason=hard_cap", oldest)
        self._items[jti] = exp

    def __contains__(self, jti: str) -> bool:
        return jti in self._items

    async def sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.SWEEP_INTERVAL_S)
            now = time.time()
            expired = [j for j, e in self._items.items() if e < now]
            for j in expired:
                del self._items[j]
            log.debug("denylist_sweep removed=%d remaining=%d", len(expired), len(self._items))
```

Per-agent state class (renamed for intent per FIX-4):

```python
# hr-agent/ciba/orchestrator.py

class RevocationState:                          # FIX-2: injectable; not module-level globals
    revoked_jtis: JtiDenylist
    token_cache_by_jti: dict[str, tuple[str, str]]   # FIX-4: jti → (user_sub, ciba_scope); was _jti_to_cache_key

class HRDispatcher:
    _token_cache: dict[tuple[str, str], _CachedToken]  # existing
    _revocation: RevocationState                        # FIX-2: dependency-injected at app startup

    async def revoke_jti(self, jti: str, user_sub: str, exp: float) -> None:
        self._revocation.revoked_jtis.add(jti, exp)
        cache_key = self._revocation.token_cache_by_jti.pop(jti, None)
        if cache_key:
            self._token_cache.pop(cache_key, None)
        log.info("internal_event_received jti=%s user_sub=%s", jti, user_sub)
```

**Sweep task lifecycle (FIX-21):** wired via FastAPI `lifespan` context manager, NOT module-level `asyncio.create_task`:

```python
# hr-agent/main.py (and 3 peers)

@asynccontextmanager
async def lifespan(app: FastAPI):
    revocation = RevocationState(revoked_jtis=JtiDenylist(), token_cache_by_jti={})
    sweep = asyncio.create_task(revocation.revoked_jtis.sweep_loop())
    app.state.revocation = revocation
    try:
        yield
    finally:
        sweep.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep
```

### §4.2 MCP server denylist + introspection cache (FIX-2, FIX-10, FIX-18)

**Correctness invariant (Stage 4 lock):** *Denylist takes precedence; negative-cache is monotonic (permanent until `exp`); positive-cache is bounded by `min(TTL, exp)`. Negative cache entries are written ONLY on a successful, signed-JWS-valid `active=false` from IS — never on network errors.*

```python
# hr-server/auth/validators.py — FIX-2: state injected, not module-level

class IntrospectionCache:
    """jti → (active, fetched_at, exp). Negative permanent until exp; positive TTL min(60s, exp)."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[bool, float, float]] = {}

    def get(self, jti: str) -> tuple[bool, float, float] | None:
        return self._items.get(jti)

    def put_negative(self, jti: str, exp: float) -> None:
        self._items[jti] = (False, time.time(), exp)

    def put_positive(self, jti: str, exp: float) -> None:
        self._items[jti] = (True, time.time(), exp)

    async def sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            now = time.time()
            expired = [j for j, (_, _, exp) in self._items.items() if exp < now]
            for j in expired:
                del self._items[j]


class ServerRevocationState:                          # FIX-2
    revoked_jtis: JtiDenylist                          # shared from common/revocation/
    introspection_cache: IntrospectionCache


_INTROSPECTION_POSITIVE_TTL = 20.0  # Stage 5 L-3 lock (was Q4 60s default; user picked 20s flat — skip Day-4 measurement)


async def validate_token(token: str, state: ServerRevocationState) -> ValidationResult:
    # 1–6: existing JWT validation (signature, iss, exp, aud, act.sub allowlist, scope)
    claims = await _validate_jwt(token)
    jti = claims["jti"]
    exp = float(claims["exp"])

    # 7: denylist (zero-latency security boundary)
    if jti in state.revoked_jtis:
        raise AuthError("ERR-MCP-002", "token revoked")

    # 8: introspection cache + IS round-trip on miss
    cached = state.introspection_cache.get(jti)
    if cached is not None:
        active, fetched_at, _ = cached
        if not active:
            # Negative is permanent until exp (FIX-18)
            raise AuthError("ERR-MCP-002", "token inactive")
        if time.time() - fetched_at < _INTROSPECTION_POSITIVE_TTL:
            return ValidationResult(claims=claims)

    # Cache miss or stale positive — call IS. Network error MUST NOT cache negative (FIX-10).
    try:
        active = await _introspect(token)
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        log.warning("introspection_network_error jti=%s err=%s", jti, e)
        # Fail open ONLY because the JWT signature was already valid (step 1) and we have no
        # signed evidence that it's revoked. Denylist is the primary security boundary.
        return ValidationResult(claims=claims)

    if active:
        state.introspection_cache.put_positive(jti, exp)
        return ValidationResult(claims=claims)
    state.introspection_cache.put_negative(jti, exp)
    raise AuthError("ERR-MCP-002", "token inactive")
```

**The denylist is the security boundary; introspection is the staleness backstop.** A future "optimization" that skips introspection on cache miss would silently weaken the design — call this out explicitly in code review and N-test docstrings.

**(cache_state × denylist_state) correctness matrix (NIT-10):**

| denylist | cache | outcome |
|---|---|---|
| has(jti) | * | 401 — denylist wins |
| ¬has(jti) | hit positive fresh | accept |
| ¬has(jti) | hit positive stale | re-introspect → IS truth |
| ¬has(jti) | hit negative | 401 — monotonic; permanent until exp |
| ¬has(jti) | miss | introspect → IS truth |
| ¬has(jti) | miss + IS network error | accept (fail-open; signature valid; denylist is the boundary) |

### §4.3 Logout token replay protection (orchestrator BCL receiver) — bounded per FIX-3

```python
# orchestrator/auth/bcl_receiver.py (new file)

class SeenLogoutTokens:
    """Bounded jti → iat. Sweeps entries older than the BCL freshness window (300s)."""

    HARD_CAP = 10_000
    SWEEP_INTERVAL_S = 60

    def __init__(self) -> None:
        self._items: dict[str, float] = {}

    def __contains__(self, jti: str) -> bool:
        return jti in self._items

    def add(self, jti: str, iat: float) -> None:
        if len(self._items) >= self.HARD_CAP:
            oldest = next(iter(self._items))
            del self._items[oldest]
            log.warning("seen_logout_jtis_evicted jti=%s reason=hard_cap", oldest)
        self._items[jti] = iat

    async def sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.SWEEP_INTERVAL_S)
            cutoff = time.time() - 300  # iat tolerance window per BCL spec
            expired = [j for j, iat in self._items.items() if iat < cutoff]
            for j in expired:
                del self._items[j]


async def handle_bcl(logout_token: str, seen: SeenLogoutTokens) -> None:
    claims = await _validate_logout_token(logout_token)  # all 9 checks per §3.3
    jti = claims["jti"]
    iat = float(claims["iat"])
    if jti in seen:
        log.info("bcl_duplicate jti=%s; ignoring", jti)
        return  # idempotent
    seen.add(jti, iat)

    # Resolve user_sub: prefer sub; fall back to sid → sub via reverse index (BLOCK-C #9)
    user_sub = claims.get("sub")
    if not user_sub:
        sid = claims.get("sid")
        if sid is None:
            raise AuthError("logout_token missing both sub and sid")
        user_sub = await _sub_by_sid(sid)  # populated at code-exchange from id_token.sid
        if user_sub is None:
            raise AuthError(f"unknown sid={sid}")

    await _run_revocation_cascade(user_sub, reason="admin_terminated")
```

### §4.4 Process-model assumptions (BLOCK-I, BLOCK-D, FIX-8) — NEW

These are invariants the design depends on. Each receiver must enforce them at startup; violation must fail-fast, not produce silently-wrong behaviour.

**Single uvicorn worker per service (BLOCK-I).** The denylist + introspection cache are in-process. With `--workers > 1`, worker A's revoke doesn't reach worker B; stale tokens succeed silently.

```python
# hr-agent/main.py (and 4 peers)

import os
WORKERS = int(os.getenv("UVICORN_WORKERS", "1"))
assert WORKERS == 1, (
    "single-worker assumption (Q5). Multi-worker requires Redis-backed denylist (Sprint 4+). "
    f"Got UVICORN_WORKERS={WORKERS}."
)
```

**`/internal/events` socket binding (BLOCK-B simplified).** Each receiver binds the internal endpoint to the docker-compose internal network only. Public/host-network exposure is forbidden. docker-compose `expose` (not `ports`) is used for the internal port; no host-side mapping. Verified by a startup log line: `internal_events_bound interface=…`.

**Trust boundary (BLOCK-D resolution).**
- `13.60.190.47` shell access list = trusted population. **The user is the only operator on this VM during the demo period.** If shell access changes during the demo, treat it as a compromise event and rotate the BCL receiver auth.
- BCL receiver applies a token-bucket per-source rate limit (10 req/s per source IP) on `/backchannel-logout` to prevent CPU-burn DoS via JWKS verification of garbage tokens.
- `/internal/events` applies a token-bucket per-source rate limit (100 req/min) to defend against revoke-flood DoS once auth is bypassed (FIX-13).

**IS client secret storage (FIX-8).**
- `ORCHESTRATOR_IS_CLIENT_SECRET` — env-only. Never committed. Documented in [`docs/wso2-is-setup.md`](../wso2-is-setup.md).
- Rotation: manual on IS-side reissue; document the runbook step in `docs/demo-runbook.md`.
- IS-side mitigation: confirm during 3A Day 1 pre-flight curl that the `orchestrator-mcp-client` app cannot revoke tokens issued by other clients (it shouldn't by IS default; verify and record in `sprint-1-fixes.md` as F-22 if anomaly found).

**Ordering invariants (BLOCK-F, BLOCK-G, BLOCK-H).** The orchestrator's logout cascade has hard ordering requirements; reorder = correctness bug. Code review enforces:
1. `Session.terminating = True` is the first state mutation.
2. `pending_ciba.cancel_event.set()` precedes `await fan_out(...)`.
3. SSE `session_terminated` emit precedes `Session` removal by at least one event-loop yield (and ideally awaits an `SSE.flushed` ack).
4. `Session` removal is the LAST mutation in the cascade.

---

## §5. Error handling matrix

**Stage 4 changes:** retry semantics specified inline (FIX-22); SECURITY-DEGRADED label introduced (FIX-6).

**Sprint 3 3A.0 spike outcome + source confirmation (2026-05-09):**
- F-20 (auth_req_id revoke is no-op) — confirmed at source. Architectural; document the ghost-approval caveat.
- F-21 (token-A revoke does NOT propagate to OBO tokens) — confirmed at source. `revokeAccessTokens(String[])` is single-row UPDATE; no parent→child linkage in schema. **For CIBA OBO tokens (token-B / token-C) the denylist fan-out is the only revocation primitive.**
- F-19 addendum (BCL CAN fan out for CIBA participants) — corrected at source. The original spike's `/oidc/logout` URL lacked `id_token_hint`, falling into the empty-cache branch. With `id_token_hint` (which our locked Q3 design includes), `DefaultLogoutTokenBuilder` iterates ALL session participants without CIBA exclusion. **D3.2 admin-terminate path can lean on IS BCL fan-out as a defense-in-depth signal.**

Per Stage 5 L-2: ship with SECURITY-DEGRADED labels for the user-driven `/oauth2/revoke` paths only (those are the F-21-bound paths). For admin-terminate (D3.2), the BCL fan-out reaches all participants — those rows do NOT need SECURITY-DEGRADED labels.

| Failure point | Behaviour | Backstop (per F-21 FAIL) |
|---|---|---|
| `/oauth2/revoke` returns 5xx | log WARN, proceed with fan-out | **SECURITY-DEGRADED.** IS does not propagate token-A revoke to OBO tokens. Denylist on receivers is the only line of defense; if fan-out also failed, captured tokens remain valid until natural TTL (1 h default). Recovery: restart receivers, re-issue logout. |
| Single fan-out leg returns 5xx | **Inline retry** once @ 200 ms within the same coroutine (FIX-22); `asyncio.gather` over per-leg coroutines with `return_exceptions=True`. On second failure, log WARN `logout_fanout_partial target=…`. | **SECURITY-DEGRADED for the missed leg.** Captured token presented to the missed receiver remains valid until natural TTL. The other 3 receivers are correctly denylisted. R-LOGOUT-7 asserts the WARN; the missed-leg receiver requires operator restart to recover within demo window. |
| **All** fan-out legs fail | log ERROR `logout_fanout_total_failure SECURITY_DEGRADED jti=…` (FIX-6 — explicit grep target); orch session still cleared; user is signed out at SPA. | **SECURITY-DEGRADED — full window.** R-LOGOUT-7b asserts the ERROR label is emitted. Demo runbook surfaces this as a 1-hour replay window — operator action: restart all 4 receivers OR accept the window. |
| `pending_ciba.cancel_event.set()` raises | log WARN; the poll task will hit its own timeout within 300 s | **F-20 FAIL means `auth_req_id` cannot be revoked at IS.** "Ghost approval" caveat is real: a CIBA poll completing after logout produces a token-B with a stale auth_req_id. The token has no consumer (poll cancelled), but if captured externally, the denylist (set at fan-out time before this WARN fires) rejects it at the receiver. |
| `cancelled_ack` barrier times out (≥100 ms) (BLOCK-F) | log WARN `cancel_barrier_timeout pending=N`; proceed to fan-out anyway | **Real exposure under F-21 FAIL.** A pending CIBA can mint a token *after* fan-out begins; the new jti isn't in the denylist; IS introspection still says `active=true` (F-21). Defense: barrier-timeout is rare (<100 ms is generous); the resulting token has no in-process consumer (poll task is cancelled). External capture is the residual threat — bounded by the auth_req_id 300 s window. |
| BCL `logout_token` validation fails (any of the 9 §3.3 checks) | 400 to IS; log WARN with reason; **do NOT** run cascade | Defense against forged BCL POSTs — see BLOCK-C / §3.3. Tested by R-LOGOUT-EX-3 (negative test). |
| BCL receiver returns 5xx (internal error) | 500 to IS; IS may retry per spec | Manual recovery: admin re-clicks Terminate. |
| Orchestrator process restarts mid-cascade | denylist entries on partial-fan-out receivers are lost on their next restart too (Q5 single-process); next MCP call hits introspection. | **SECURITY-DEGRADED until tokens expire naturally** (F-21 FAIL — IS introspection does NOT reflect parent revoke for OBO tokens). Production roadmap: persistent denylist (Redis). Recovery for demo: restart all 4 receivers. |
| Orchestrator-internal stale-cookie request between session-terminating and session-removed (BLOCK-G) | Caller sees 401 at handler entry because `Session.terminating = True` was the first mutation. New CIBA initiation cannot happen on a terminating session. | Eliminates the race; no backstop required. |
| SSE channel teardown before `session_terminated` flush (BLOCK-H, UC-10) | Mitigated by ordering invariant: emit → flush ack (or 100 ms drain) → remove Session. | If flush fails (TCP reset), client's reconnect logic encounters 401 (session is gone) → SPA redirects to login with no banner. Acceptable degradation; rare. |

---

## §6. Open questions — Stage 4 resolutions

| OQ | Status | Resolution |
|---|---|---|
| OQ-1 | **Resolved** | BLOCK-B locked simple: shared secret only + per-source rate limit + receivers bind to docker-internal interfaces only. Production upgrade path (OAuth client_credentials with `revoke:jti` scope OR mTLS) documented in §3.2 + §4.4 — Sprint 4+. |
| OQ-2 | **Resolved** | Reuse C12 reverse-SSH tunnel rig. Trust boundary documented in §4.4 (BLOCK-D). Per-source rate limit added on `/backchannel-logout`. |
| OQ-3 | **Open — 3B.1 Day 1** | Probe whether IS fires BCL for the orchestrator app (auth_code). Document outcome as F-20 PASS/FAIL in `sprint-1-fixes.md`. Falls back to introspection-poll if FAIL (would bump D3.2 to a stretch goal). |
| OQ-4 | **Resolved** | Stage 5 L-3 supersedes Stage 1 Q4: introspection cache TTL = **20 s flat**, no Day-4 measurement. NIT-11 reframe still applies: this is a *propagation backstop*, not load shedder. First call per jti is always cold cache → 1 IS round-trip per session. |
| OQ-5 | **Resolved** | Hard cap 10k entries per receiver with FIFO eviction + WARN. Sweeper supervisor restarts on exception (FIX-13). |
| OQ-6 | **Resolved** | BLOCK-H ordering invariant (§4.4): emit SSE → await flush ack → remove Session. The flush ack pattern is defined in `common/sse.py` (existing wrapper). |
| OQ-7 | **Resolved** | FIX-12: per-`user_sub` `asyncio.Lock` serialises UC-09/UC-10 concurrent paths; reason precedence `admin_terminated` > `user_signed_out`. Documented in §1.2. |
| OQ-8 | **Resolved** | FIX-17: branch on `reason` field. `user_signed_out` → "you signed out at HH:MM"; `admin_terminated` → "your previous session was ended"; `token_expired` → "your previous access expired". |
| **OQ-9 (BLOCK-A)** | **Resolved — F-21 FAIL** (2026-05-09) | C13 ran against live IS with `employee_user`. Verdict: token-B remained `active=true` after token-A revoked. WSO2 IS 7.2 treats CIBA OBO grants as fully independent of parent. §5 error matrix re-classified to SECURITY-DEGRADED labels per Stage 5 L-2 lock. Captured in [`sprint-1-fixes.md`](sprint-1-fixes.md) §F-21. |
| **OQ-10 (Q-LOGOUT-4)** | **Resolved — F-20 FAIL (soft)** (2026-05-09) | C14 ran against live IS. Verdict: `/oauth2/revoke` returns 200 for `auth_req_id` but IS treats it as a no-op (polling still returns `authorization_pending`). 3B.2 will not wire `auth_req_id` revoke. Ghost-approval caveat documented. Captured in [`sprint-1-fixes.md`](sprint-1-fixes.md) §F-20. |

---

## §7. What's *not* in this design (deferred / out of scope)

- **Persistent denylist (Redis).** Q5 single-process accepted; Sprint 4+ roadmap.
- **CAEP wire format on `/internal/revoke`.** Use simple JSON. CAEP migration in production-roadmap doc.
- **Agent-side BCL receivers.** F-19 — IS will not call them.
- **Sub-second SLA on the cascade.** Industry bar; we target ≤2 s for 3A demo, ≤5 s for D3.2 admin-terminate. Sub-second is a Sprint 4+ optimization (parallel + batched fan-out).
- **`auth_req_id` revocation at IS (C14 capability).** Probed Day 1 of 3B; if PASS, wire in 3B.1; if FAIL, document the "ghost approval" caveat.
- **Cross-tenant / multi-user concurrent logout.** Single-tenant demo; correctness on concurrent logout for the same user is covered (idempotent denylist), but performance under N concurrent users is not measured.

---

## §8. References

- Stage 1 review: [`sprint-3-stage-1-product-review.md`](sprint-3-stage-1-product-review.md)
- UCs: [`UC-09`](../use-cases/UC-09-logout-cascade.md), [`UC-10`](../use-cases/UC-10-admin-terminate.md)
- Brainstorm + industry context: [`docs/spikes/sprint-3-logout-design-brainstorm.md`](../spikes/sprint-3-logout-design-brainstorm.md)
- F-19 capability finding: [`sprint-1-fixes.md`](sprint-1-fixes.md) §F-19
- C12 spike rig (kept on for D3.2 demo): [`docs/spikes/c12-bcl-spike-setup.md`](../spikes/c12-bcl-spike-setup.md)
- OIDC BCL spec: [openid.net/specs/openid-connect-backchannel-1_0.html](https://openid.net/specs/openid-connect-backchannel-1_0.html)
- OIDC RP-Initiated Logout: [openid.net/specs/openid-connect-rpinitiated-1_0.html](https://openid.net/specs/openid-connect-rpinitiated-1_0.html)
