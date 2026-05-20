"""orchestrator/auth/bcl_receiver.py — Sprint 3 3B.1 (D3.2).

OIDC Back-Channel Logout 1.0 §2.5–2.7 receiver. When an admin terminates
a user's session in the IS Console, IS POSTs a ``logout_token`` to the
URL configured on ``orchestrator-mcp-client``'s ``back_channel_logout_uri``
property. This module:

  * validates the inbound logout_token against ALL 9 BLOCK-C checks
    (sig, alg-allowlist, typ, iss, aud, iat-window, events, no-nonce,
    sub-or-sid),
  * dedups by ``jti`` (FIX-3 ``SeenLogoutTokens`` with FIFO eviction
    and time-bounded sweep),
  * resolves ``sub`` (preferring claims.sub; falling back to
    sid → user_sub via the SessionStore reverse index populated at
    code-exchange time), and
  * invokes ``LogoutHandler.execute_for_user_sub()`` with
    ``reason="admin_terminated"``.

Failure modes (BLOCK-C):
  * Any validation failure → 400 ``invalid_logout_token``. IS does NOT
    retry on 4xx, so we log WARN with the rejection reason for the
    forensic trail (R-LOGOUT-EX-3 negative test asserts cascade does
    NOT run).
  * Internal error → 500. IS may retry per spec.
  * Duplicate ``jti`` → 200 (idempotent — spec allows IS to retry).

Why we don't reuse common/auth/jwt_validator.validate() directly: that
function enforces ``aud == config.expected_aud`` AND scope checks designed
for access tokens. logout_tokens have a different invariant set
(typ=logout+jwt, no nonce, events claim, iat-window). We reuse the JWKS
cache + RS256 signature plumbing, then layer the BCL-specific checks on
top — same key infrastructure, different policy.

Single uvicorn worker invariant (BLOCK-I) is enforced by orchestrator/main.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from urllib.parse import parse_qsl

import jwt as pyjwt
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from jwt.algorithms import RSAAlgorithm

from common.auth.errors import JWTValidationError
from common.auth.jwt_validator import JWKSCache
from orchestrator.auth.logout_handler import LogoutHandler, LogoutResult
from orchestrator.auth.session_store import SessionStore

logger = logging.getLogger(__name__)

__all__ = [
    "BCL_EVENTS_URI",
    "LOGOUT_TOKEN_TYP",
    "BCLValidationError",
    "SeenLogoutTokens",
    "LogoutTokenClaims",
    "validate_logout_token",
    "BCLReceiverDeps",
    "build_bcl_router",
]

# OIDC BCL spec literals.
BCL_EVENTS_URI = "http://schemas.openid.net/event/backchannel-logout"
LOGOUT_TOKEN_TYP = "logout+jwt"
_IAT_TOLERANCE_SECONDS = 300  # spec floor; same window we sweep on


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BCLValidationError(Exception):
    """Raised by ``validate_logout_token`` when any of the 9 checks fail.

    The ``reason`` string is what surfaces back to IS in the 400 response
    body and what we log at WARN level. Don't include claim values that an
    attacker controls — keep the reason categorical.
    """

    def __init__(self, reason: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


# ---------------------------------------------------------------------------
# Replay-protection set (FIX-3)
# ---------------------------------------------------------------------------


@dataclass
class SeenLogoutTokens:
    """Bounded ``jti → iat`` seen-set with FIFO eviction + time sweep.

    Two layers of protection:

      * Hard cap (``hard_cap``) with FIFO eviction prevents unbounded
        memory growth. WARN log is emitted on each eviction so an
        operator can spot abnormal pressure.
      * Periodic sweep (``sweep_interval_seconds``) drops entries whose
        ``iat`` is older than ``iat_tolerance_seconds`` — beyond that
        window the spec says the same jti can no longer be replayed
        anyway, so we can free the slot.

    Single-process per Q5 + BLOCK-I; protected by the orchestrator's
    single-event-loop invariant (no asyncio.Lock needed for membership
    checks on a single worker).
    """

    hard_cap: int = 10_000
    sweep_interval_seconds: int = 60
    iat_tolerance_seconds: int = _IAT_TOLERANCE_SECONDS
    _items: "OrderedDict[str, float]" = field(default_factory=OrderedDict)

    def __contains__(self, jti: str) -> bool:
        return jti in self._items

    def __len__(self) -> int:
        return len(self._items)

    def add(self, jti: str, iat: float) -> None:
        """Record a freshly-validated ``jti`` with its ``iat``.

        Idempotent on existing jti (refreshes recency in OrderedDict).
        FIFO-evicts the oldest entry if at capacity.
        """
        if jti in self._items:
            self._items.move_to_end(jti, last=True)
            self._items[jti] = iat
            return
        if len(self._items) >= self.hard_cap:
            evicted_jti, _ = self._items.popitem(last=False)
            logger.warning(
                "seen_logout_jtis_evicted | jti=%s reason=hard_cap",
                evicted_jti[:8],
            )
        self._items[jti] = iat

    def sweep_once(self, now: float | None = None) -> int:
        """Drop entries older than the iat tolerance window. Return count removed."""
        cutoff = (now if now is not None else time.time()) - self.iat_tolerance_seconds
        expired = [j for j, iat in self._items.items() if iat < cutoff]
        for j in expired:
            del self._items[j]
        return len(expired)

    async def sweep_loop(self) -> None:
        """Background task. Wired via FastAPI lifespan."""
        while True:
            try:
                await asyncio.sleep(self.sweep_interval_seconds)
            except asyncio.CancelledError:
                raise
            try:
                removed = self.sweep_once()
                if removed:
                    logger.debug(
                        "seen_logout_jtis_sweep | removed=%d remaining=%d",
                        removed,
                        len(self._items),
                    )
            except Exception:  # noqa: BLE001 — never let a sweep crash kill the loop
                logger.exception("seen_logout_jtis_sweep_failed | continuing")


# ---------------------------------------------------------------------------
# Claims model + validator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogoutTokenClaims:
    """The fields a validated logout_token surfaces to the receiver.

    Only the fields the cascade actually consumes are surfaced — keeps the
    boundary tight.
    """

    jti: str
    iat: float
    iss: str
    aud: str
    sub: str | None
    sid: str | None
    raw: dict[str, Any]


async def validate_logout_token(
    raw_token: str,
    *,
    expected_iss: str,
    expected_aud: str,
    jwks_cache: JWKSCache,
    now: float | None = None,
) -> LogoutTokenClaims:
    """Run the BLOCK-C 9-check validation; return ``LogoutTokenClaims`` on success.

    Raises ``BCLValidationError`` with a categorical ``reason`` on any
    failure. The reason is what the 400 response body surfaces.

    Args:
        raw_token: The raw JWT string from ``logout_token`` form param.
        expected_iss: IS issuer URL (exact match).
        expected_aud: Orchestrator client_id (exact match).
        jwks_cache: Shared JWKS cache (RS256 keys served by IS).
        now: Override "now" for tests. ``None`` → ``time.time()``.

    Returns:
        ``LogoutTokenClaims`` carrying everything the receiver needs.
    """
    if now is None:
        now = time.time()

    # ── Pre-flight: header inspection (covers checks #2 alg + #3 typ before
    #     we burn JWKS network on a forged token).
    try:
        header = pyjwt.get_unverified_header(raw_token)
    except pyjwt.exceptions.DecodeError as exc:
        raise BCLValidationError("malformed_jwt_header") from exc

    # Check #2 — alg allow-list. RS256 only. Reject "none", "HS256", any other
    # alg. The classic forgery is "alg: HS256" with the JWKS public key as the
    # HMAC secret; rejecting anything but RS256 closes that vector.
    alg = header.get("alg")
    if alg != "RS256":
        raise BCLValidationError("alg_not_allowed", details={"alg": alg})

    # Check #3 — typ. Spec (OIDC BCL §2.4) REQUIRES ``logout+jwt``. Empirical
    # finding 2026-05-10 against WSO2 IS 7.x RC: the BCL emission omits the
    # typ header entirely (only x5t#S256, kid, alg are present). Other
    # OIDC libraries (Spring Security, Curity samples) accommodate this
    # by soft-checking — accept absent OR exact match; reject any other
    # value. We do the same here.
    #
    # Why this is acceptable defense-wise: the categorical separation
    # between logout_tokens and other JWTs (id_token, access_token) is
    # carried by Check #7 (the ``events`` claim must contain the BCL
    # URI). Neither id_tokens nor access_tokens carry that claim, so
    # they cannot be replayed as logout_tokens regardless of typ. The
    # typ check is defense in depth, not the load-bearing piece.
    typ = header.get("typ")
    if typ is not None and typ != LOGOUT_TOKEN_TYP:
        raise BCLValidationError(
            "typ_not_logout_jwt",
            details={"typ": typ, "header": header},
        )
    if typ is None:
        # Surface the deviation so SIEM / ops can audit it; this is the
        # WSO2 IS RC behaviour, not an attack.
        logger.warning(
            "bcl_typ_header_absent | header=%s — accepting per WSO2 IS"
            " RC accommodation; events-claim check (#7) remains the"
            " categorical separator from non-BCL JWTs",
            header,
        )

    kid = header.get("kid", "")
    if not kid:
        raise BCLValidationError("missing_kid")

    # ── Check #1 — JWS signature via JWKS.
    try:
        jwk_dict = await jwks_cache.get_key(kid)
    except JWTValidationError as exc:
        raise BCLValidationError("unknown_kid", details={"kid": kid}) from exc
    try:
        signing_key = RSAAlgorithm.from_jwk(jwk_dict)
    except Exception as exc:  # noqa: BLE001
        raise BCLValidationError("jwk_unparseable", details={"kid": kid}) from exc

    try:
        payload: dict[str, Any] = pyjwt.decode(
            raw_token,
            key=signing_key,
            algorithms=["RS256"],
            # Generous leeway so PyJWT doesn't pre-empt our explicit iat
            # check (#6) with its own ImmatureSignatureError. Our check is
            # tighter than PyJWT's and surfaces categorical reasons.
            leeway=86400,
            options={
                # iss + aud + exp checks done explicitly below — disable
                # PyJWT's auto-checks so failures surface with our categorical
                # reasons, not pyjwt's strings.
                "verify_signature": True,
                "verify_iss": False,
                "verify_aud": False,
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except pyjwt.InvalidSignatureError as exc:
        raise BCLValidationError("bad_signature") from exc
    except pyjwt.PyJWTError as exc:
        raise BCLValidationError("jwt_decode_failed", details={"err": str(exc)}) from exc

    # ── Check #4 — iss exact-match.
    if payload.get("iss") != expected_iss:
        raise BCLValidationError(
            "iss_mismatch",
            details={"actual_iss": payload.get("iss"), "expected_iss": expected_iss},
        )

    # ── Check #5 — aud exact-match. logout_token aud is a string per spec
    # (not a list) but allow either shape defensively.
    aud = payload.get("aud")
    aud_set = {aud} if isinstance(aud, str) else set(aud or [])
    if expected_aud not in aud_set:
        raise BCLValidationError(
            "aud_mismatch",
            details={"actual_aud": aud, "expected_aud": expected_aud},
        )

    # ── Check #6 — iat freshness window.
    iat_raw = payload.get("iat")
    if not isinstance(iat_raw, (int, float)):
        raise BCLValidationError("iat_missing_or_invalid")
    iat = float(iat_raw)
    if iat > now + 30:  # small forward skew tolerance
        raise BCLValidationError("iat_in_future", details={"iat": iat, "now": now})
    if iat + _IAT_TOLERANCE_SECONDS < now:
        raise BCLValidationError("iat_too_old", details={"iat": iat, "now": now})

    # ── Check #7 — events claim must contain the BCL URI.
    events = payload.get("events")
    if not isinstance(events, dict) or BCL_EVENTS_URI not in events:
        raise BCLValidationError(
            "events_claim_missing_bcl_uri",
            details={"events": events},
        )

    # ── Check #8 — nonce MUST be absent. Per BCL spec §2.4: a logout_token
    # with a nonce is malformed (nonce belongs in id_tokens).
    if "nonce" in payload:
        raise BCLValidationError("nonce_must_be_absent")

    # ── Check #9 — at least one of sub or sid. Either or both is fine; sid
    # → user_sub resolution happens in the route handler (uses SessionStore).
    sub = payload.get("sub")
    sid = payload.get("sid")
    sub = sub if isinstance(sub, str) and sub else None
    sid = sid if isinstance(sid, str) and sid else None
    if sub is None and sid is None:
        raise BCLValidationError("sub_and_sid_both_missing")

    # Replay protection requires jti per BCL spec §2.4. Spec says SHOULD; we
    # treat as MUST because without jti SeenLogoutTokens cannot dedup, which
    # opens an admin to repeated cascade fire.
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise BCLValidationError("jti_missing")

    return LogoutTokenClaims(
        jti=jti,
        iat=iat,
        iss=payload["iss"],
        aud=aud_set.pop() if isinstance(aud, str) else expected_aud,
        sub=sub,
        sid=sid,
        raw=payload,
    )


# ---------------------------------------------------------------------------
# Route factory
# ---------------------------------------------------------------------------


@dataclass
class BCLReceiverDeps:
    """Dependencies for ``build_bcl_router``.

    Wired in ``orchestrator/main.py`` lifespan startup.
    """

    expected_iss: str
    expected_aud: str
    jwks_cache: JWKSCache
    seen_logout_tokens: SeenLogoutTokens
    session_store: SessionStore
    logout_handler: LogoutHandler


def build_bcl_router(deps: BCLReceiverDeps) -> APIRouter:
    """Return a FastAPI router exposing ``POST /backchannel-logout``.

    Wire as ``app.include_router(build_bcl_router(deps))``. The route is
    intentionally NOT mounted under any prefix — the BCL spec requires the
    URI to be exactly what's registered on the IS app.
    """
    router = APIRouter()

    @router.post("/backchannel-logout")
    async def backchannel_logout(request: Request) -> JSONResponse:
        """Validate the logout_token and run the admin-terminate cascade."""
        # Use existing X-Request-ID middleware-stamped value if present, else
        # synthesize one. The cascade reuses this rid for the audit chain so
        # ``tools/grep-trace.sh`` works for UC-10 the same way as UC-09.
        rid = request.headers.get("X-Request-ID") or f"bcl-{int(time.time()*1000)}"

        # Parse application/x-www-form-urlencoded body manually — avoids a
        # python-multipart dependency for one route. ``parse_qsl`` handles the
        # spec-required encoding correctly. Reject obviously-wrong content
        # types early so we don't decode random JSON bodies as form data.
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            logger.warning(
                "bcl_bad_content_type | rid=%s content_type=%r", rid, ctype
            )
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_logout_token", "detail": "bad_content_type"},
            )
        body_bytes = await request.body()
        try:
            form = dict(parse_qsl(body_bytes.decode("utf-8"), keep_blank_values=False))
        except UnicodeDecodeError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_logout_token", "detail": "body_not_utf8"},
            )
        logout_token = form.get("logout_token")
        if not logout_token:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_logout_token", "detail": "missing_logout_token"},
            )

        # ── 9-check validation. Any failure → 400 (per spec). Log WARN.
        try:
            claims = await validate_logout_token(
                logout_token,
                expected_iss=deps.expected_iss,
                expected_aud=deps.expected_aud,
                jwks_cache=deps.jwks_cache,
            )
        except BCLValidationError as exc:
            logger.warning(
                "bcl_logout_token_invalid | rid=%s reason=%s details=%s",
                rid,
                exc.reason,
                exc.details,
            )
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_logout_token", "detail": exc.reason},
            )

        # ── Replay dedup (FIX-3). Idempotent 200 on duplicate; spec allows
        # IS to retry on a transient network blip.
        if claims.jti in deps.seen_logout_tokens:
            logger.info(
                "bcl_duplicate | rid=%s jti=%s — idempotent ack",
                rid,
                claims.jti[:8],
            )
            return JSONResponse(content={"ok": True, "deduped": True})
        deps.seen_logout_tokens.add(claims.jti, claims.iat)

        # ── Resolve user_sub. Prefer sub; fall back to sid → user_sub.
        user_sub = claims.sub
        if user_sub is None:
            assert claims.sid is not None  # validator guarantees one or the other
            user_sub = deps.session_store.resolve_sid(claims.sid)
            if user_sub is None:
                logger.warning(
                    "bcl_unknown_sid | rid=%s sid=%s — no session in reverse index",
                    rid,
                    claims.sid,
                )
                # 200 — we have nothing to revoke for this sid; idempotent.
                return JSONResponse(content={"ok": True, "no_session": True})

        # ── Run the cascade. ``execute_for_user_sub`` strips the redirect URL
        # since IS is the originator (no SPA redirect needed back to /oidc/logout).
        logger.info(
            "bcl_received | rid=%s user_sub=%s jti=%s sub_present=%s sid_present=%s",
            rid,
            user_sub,
            claims.jti[:8],
            claims.sub is not None,
            claims.sid is not None,
        )
        try:
            result: LogoutResult = await deps.logout_handler.execute_for_user_sub(
                user_sub=user_sub,
                request_id=rid,
                reason="admin_terminated",
            )
        except Exception:  # noqa: BLE001
            logger.exception("bcl_cascade_failed | rid=%s user_sub=%s", rid, user_sub)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "internal_error"},
            )

        return JSONResponse(
            content={
                "ok": True,
                "had_session": result.had_session,
                "request_id": rid,
            }
        )

    return router
