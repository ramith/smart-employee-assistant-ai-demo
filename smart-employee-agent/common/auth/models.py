"""Pure dataclass types for OAuth tokens and JWT claims.

Boundary rule (F-09): these types are pure runtime data — they never cross HTTP
or SSE boundaries directly. Do NOT wrap these in Pydantic BaseModel; serialise
at the API boundary by extracting individual fields into a Pydantic model.

All datetime values are timezone-aware UTC. Callers must never pass naive datetimes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=timezone.utc)


# ── OAuthToken ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OAuthToken:
    """Raw /oauth2/token response wrapper. Never serialise via Pydantic."""

    access_token: str
    token_type: str          # always "Bearer"
    expires_in: int          # IS-issued seconds from issuance
    expires_at: datetime     # computed at issuance: now + timedelta(seconds=expires_in)
    refresh_token: str | None  # only set if offline_access requested (T8/Q3: do not request)
    scope: str               # space-separated
    id_token: str | None     # CIBA may issue; not used in Sprint 1

    @classmethod
    def from_response(
        cls,
        body: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> "OAuthToken":
        """Construct from a raw /oauth2/token JSON body, computing expires_at."""
        effective_now = now if now is not None else _utc_now()
        expires_in: int = int(body["expires_in"])
        return cls(
            access_token=body["access_token"],
            token_type=body.get("token_type", "Bearer"),
            expires_in=expires_in,
            expires_at=effective_now + timedelta(seconds=expires_in),
            refresh_token=body.get("refresh_token"),
            scope=body.get("scope", ""),
            id_token=body.get("id_token"),
        )


# ── OBOToken ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OBOToken:
    """User-on-behalf-of token. Wraps OAuthToken plus decoded + verified claims."""

    raw: OAuthToken
    sub: str       # user UUID (token subject)
    act_sub: str   # specialist agent ID (depth-1 act.sub)
    aud: str       # specialist's OAuth Client ID (per F-06)
    iss: str       # IS issuer URL
    iat: datetime  # token issuance time (timezone-aware UTC)
    jti: str       # JWT ID — required (F-08); used for revocation in Sprint 3

    def is_expired(self, buffer_s: int = 30) -> bool:
        """Return True if the token will expire within buffer_s seconds from now."""
        return _utc_now() >= self.raw.expires_at - timedelta(seconds=buffer_s)


# ── JWTClaims ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class JWTClaims:
    """Decoded but NOT verified JWT payload. Verification is the caller's responsibility."""

    sub: str
    iss: str
    aud: str | list[str]
    exp: int               # Unix epoch seconds
    iat: int               # Unix epoch seconds
    jti: str | None
    act: dict[str, Any] | None  # RFC 8693 actor claim; may be nested
    scope: str | None
    aut: str | None        # WSO2 IS application user type, e.g. "APPLICATION_USER"

    def act_chain(self) -> list[str]:
        """Return actor sub values from outermost to innermost by walking nested act claims.

        Matches the semantics of idp_capability_test/_lib.py:act_chain().
        Returns an empty list when the act claim is absent.

        Examples:
            No act claim                 -> []
            {"sub": "agent-a"}           -> ["agent-a"]
            {"sub": "b", "act":
              {"sub": "a"}}              -> ["b", "a"]
        """
        chain: list[str] = []
        cur: Any = self.act
        while isinstance(cur, dict):
            sub = cur.get("sub")
            if sub:
                chain.append(str(sub))
            cur = cur.get("act")
        return chain
