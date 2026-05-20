"""JWKS-backed JWT validator — Sprint 1 v4 implementation.

Replaces the Sprint 0 stub. Provides:
  - ``ValidatorConfig``  — frozen dataclass for per-service configuration.
  - ``JWKSCache``        — async TTL-based JWKS fetcher; kid-miss triggers one refetch.
  - ``validate()``       — async 6-step claim validator returning ``JWTClaims``.

Design constraints (sprint-1-fixes.md):
  F-02  JWTClaims shape; ``jti`` is required (see F-08).
  F-04  MCP 6-step validator: sig → iss → exp → aud → act.sub → scopes.
  F-08  ``jti`` is required and non-empty; missing ``jti`` raises ERR-AUTH-010.
  F-09  Dataclass-vs-Pydantic boundary: this module uses dataclasses only.

Library choice: ``PyJWT[cryptography]`` (simpler API, single decode call,
RFC 7517 JWK import via ``jwt.algorithms.RSAAlgorithm``).

Clock-skew tolerance: leeway is applied to both ``exp`` and ``nbf`` via PyJWT's
built-in ``leeway`` parameter (``datetime.timedelta``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from .errors import JWTValidationError
from .models import JWTClaims

logger = logging.getLogger(__name__)

__all__ = [
    "ValidatorConfig",
    "JWKSCache",
    "prewarm_shared_cache",
    "validate",
]

# ── ValidatorConfig ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ValidatorConfig:
    """Per-service JWT validation configuration.

    All fields are read from env / service config at startup.
    ``expected_iss`` and ``jwks_url`` are always required.
    ``expected_aud`` is optional; when ``None`` the audience claim is not
    checked by ``validate()`` (caller may enforce it separately).
    ``required_scopes`` is a *subset* check — every listed scope must be
    present in the token's ``scope`` claim.
    ``leeway_seconds`` is forwarded to PyJWT for ``exp`` / ``nbf`` clock-skew
    tolerance.
    ``insecure_tls`` disables TLS certificate verification for development
    against self-signed IS certificates.

    Attributes:
        expected_iss: Exact issuer URL; must match token ``iss``.
        jwks_url: Full URL to the IS JWKS endpoint.
        expected_aud: Optional exact audience string; if None, aud is unchecked.
        required_scopes: Every scope in this set must be present in the token.
        leeway_seconds: Allowed clock skew in seconds for exp/nbf checks.
        insecure_tls: Disable TLS verification (dev only).
    """

    expected_iss: str
    jwks_url: str
    expected_aud: str | None = None
    required_scopes: frozenset[str] = frozenset()
    leeway_seconds: int = 30
    insecure_tls: bool = False


# ── JWKSCache ─────────────────────────────────────────────────────────────────


@dataclass
class JWKSCache:
    """Async TTL-based JWKS key cache.

    One instance per ``jwks_url``.  Keys are fetched lazily on the first
    ``get_key()`` call and cached for ``ttl_seconds`` (default 1 hour).
    On a ``kid`` miss the cache is refreshed once before raising.

    Args:
        jwks_url: JWKS endpoint URL.
        ttl_seconds: Time-to-live for the cached key set.
        insecure_tls: Disable TLS cert verification (dev only).
    """

    jwks_url: str
    ttl_seconds: int = 3600
    insecure_tls: bool = False

    # Private runtime state — excluded from __init__ via field(init=False)
    _keys: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _fetched_at: float = field(default=0.0, init=False, repr=False)

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) >= self.ttl_seconds

    async def refresh(self) -> None:
        """Fetch the JWKS endpoint and repopulate the key map.

        Keys are indexed by ``kid``.  Raises ``JWTValidationError`` on HTTP or
        JSON failures so callers receive a typed error rather than raw httpx
        exceptions.
        """
        logger.debug("jwks_cache_refresh url=%s", self.jwks_url)
        try:
            async with httpx.AsyncClient(verify=not self.insecure_tls) as client:
                response = await client.get(self.jwks_url, timeout=10.0)
                response.raise_for_status()
                jwks: dict[str, Any] = response.json()
        except httpx.HTTPStatusError as exc:
            raise JWTValidationError(
                f"JWKS fetch failed: HTTP {exc.response.status_code}",
                error_id="ERR-AUTH-006",
                details={"jwks_url": self.jwks_url, "status_code": exc.response.status_code},
            ) from exc
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            raise JWTValidationError(
                f"JWKS fetch error: {exc}",
                error_id="ERR-AUTH-006",
                details={"jwks_url": self.jwks_url},
            ) from exc

        new_keys: dict[str, dict[str, Any]] = {}
        for key_dict in jwks.get("keys", []):
            kid = key_dict.get("kid")
            if kid:
                new_keys[kid] = key_dict
        self._keys = new_keys
        self._fetched_at = time.monotonic()
        logger.debug("jwks_cache_populated kid_count=%d", len(self._keys))

    async def get_key(self, kid: str) -> dict[str, Any]:
        """Return the JWK dict for *kid*, refreshing the cache if stale or on miss.

        Args:
            kid: Key ID from the JWT header.

        Returns:
            JWK dict suitable for ``jwt.algorithms.RSAAlgorithm.from_jwk()``.

        Raises:
            JWTValidationError: If the key is not found after one forced refresh.
        """
        if self._is_stale():
            await self.refresh()

        if kid in self._keys:
            return self._keys[kid]

        # kid miss — try a single forced refresh
        logger.debug("jwks_cache_kid_miss kid=%s — forcing refresh", kid)
        await self.refresh()

        if kid not in self._keys:
            raise JWTValidationError(
                f"Unknown signing key kid={kid!r}",
                error_id="ERR-AUTH-006",
                details={"kid": kid, "known_kids": list(self._keys.keys())},
            )
        return self._keys[kid]


# ── Module-level cache registry ───────────────────────────────────────────────

# Keyed by (jwks_url, insecure_tls) so callers that omit jwks_cache get a
# shared singleton without constructing a new httpx client on every call.
_cache_registry: dict[tuple[str, bool], JWKSCache] = {}


def _get_or_create_cache(config: ValidatorConfig) -> JWKSCache:
    key = (config.jwks_url, config.insecure_tls)
    if key not in _cache_registry:
        _cache_registry[key] = JWKSCache(
            jwks_url=config.jwks_url,
            insecure_tls=config.insecure_tls,
        )
    return _cache_registry[key]


async def prewarm_shared_cache(*, jwks_url: str, insecure_tls: bool) -> JWKSCache:
    """Eagerly populate the shared registry's JWKS cache for ``(jwks_url, insecure_tls)``.

    Mid-sprint observability fix (2026-05-09): callers that go through the
    shared registry path (``validate(..., jwks_cache=None)`` — used by the
    A2A inbound validator on agents) get the cache lazily on first request,
    paying ~800 ms IS RTT on the user-visible critical path. Calling this
    from ``lifespan`` startup amortises the cost.

    Best-effort: returns the (possibly empty) cache instance so callers can
    inspect it. Failure is allowed to surface — caller decides whether to
    log + continue or fail-fast.
    """
    config = ValidatorConfig(
        expected_iss="",  # unused for cache key
        jwks_url=jwks_url,
        expected_aud=None,
        required_scopes=frozenset(),
        insecure_tls=insecure_tls,
    )
    cache = _get_or_create_cache(config)
    await cache.refresh()
    return cache


# ── validate() ────────────────────────────────────────────────────────────────


async def validate(
    jwt_token: str,
    config: ValidatorConfig,
    *,
    jwks_cache: JWKSCache | None = None,
) -> JWTClaims:
    """Verify a JWT token and return decoded ``JWTClaims``.

    Performs a 6-step validation in order:

    1. Decode header to extract ``kid``; fetch signing key from JWKS cache.
    2. Verify signature, ``iss``, ``exp`` (+ leeway), ``nbf`` (+ leeway) via
       PyJWT.  Passes ``options={"verify_aud": False}`` so aud is handled in
       step 3 (PyJWT strict-aud rejects list-aud when expected is a string).
    3. Check ``aud`` if ``config.expected_aud`` is set.
    4. Verify ``jti`` is present and non-empty (F-08).
    5. Check ``config.required_scopes`` is a subset of the token's scopes.
    6. Build and return ``JWTClaims``.

    Args:
        jwt_token: The raw Bearer token string.
        config: Validator configuration for this service endpoint.
        jwks_cache: Optional pre-built cache; if ``None`` a shared registry
            instance is used (keyed by ``jwks_url``).

    Returns:
        Decoded and fully-verified ``JWTClaims`` instance.

    Raises:
        JWTValidationError: On any validation failure; ``error_id`` identifies
            the specific catalog entry:
            - ``ERR-AUTH-006`` — bad/unknown signature
            - ``ERR-AUTH-007`` — issuer mismatch
            - ``ERR-AUTH-008`` — token expired (outside leeway)
            - ``ERR-AUTH-010`` — missing or empty ``jti``
            - ``ERR-MCP-001`` — audience mismatch
            - ``ERR-MCP-003`` — missing required scope
    """
    cache = jwks_cache if jwks_cache is not None else _get_or_create_cache(config)

    # ── Step 1: header → kid → signing key ───────────────────────────────────
    try:
        header = jwt.get_unverified_header(jwt_token)
    except jwt.exceptions.DecodeError as exc:
        raise JWTValidationError(
            f"Malformed JWT header: {exc}",
            error_id="ERR-AUTH-006",
        ) from exc

    kid: str = header.get("kid", "")
    if not kid:
        raise JWTValidationError(
            "JWT header missing 'kid'",
            error_id="ERR-AUTH-006",
            details={"header": header},
        )

    jwk_dict = await cache.get_key(kid)  # raises JWTValidationError on unknown kid

    try:
        signing_key = RSAAlgorithm.from_jwk(jwk_dict)
    except Exception as exc:
        raise JWTValidationError(
            f"Failed to build signing key from JWK: {exc}",
            error_id="ERR-AUTH-006",
            details={"kid": kid},
        ) from exc

    # ── Step 2: signature + iss + exp/nbf via PyJWT ───────────────────────────
    leeway = timedelta(seconds=config.leeway_seconds)
    try:
        payload: dict[str, Any] = jwt.decode(
            jwt_token,
            key=signing_key,
            algorithms=["RS256"],
            issuer=config.expected_iss,
            leeway=leeway,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iss": True,
                # Audience checked manually below (step 3) so PyJWT's strict
                # equality doesn't trip on list-aud tokens.
                "verify_aud": False,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise JWTValidationError(
            f"Token expired: {exc}",
            error_id="ERR-AUTH-008",
            details={"exp": _unverified_exp(jwt_token)},
        ) from exc
    except jwt.InvalidIssuerError as exc:
        unverified = _decode_unverified(jwt_token)
        raise JWTValidationError(
            f"Issuer mismatch: {exc}",
            error_id="ERR-AUTH-007",
            details={
                "actual_iss": unverified.get("iss"),
                "expected_iss": config.expected_iss,
            },
        ) from exc
    except jwt.InvalidSignatureError as exc:
        raise JWTValidationError(
            f"Invalid signature: {exc}",
            error_id="ERR-AUTH-006",
            details={"kid": kid},
        ) from exc
    except jwt.DecodeError as exc:
        raise JWTValidationError(
            f"JWT decode error: {exc}",
            error_id="ERR-AUTH-006",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise JWTValidationError(
            f"Invalid token: {exc}",
            error_id="ERR-AUTH-006",
        ) from exc

    # ── Step 3: audience check (optional) ────────────────────────────────────
    if config.expected_aud is not None:
        raw_aud: str | list[str] | None = payload.get("aud")
        aud_list: list[str] = (
            raw_aud if isinstance(raw_aud, list) else ([raw_aud] if raw_aud else [])
        )
        if config.expected_aud not in aud_list:
            raise JWTValidationError(
                f"Audience mismatch: expected {config.expected_aud!r}",
                error_id="ERR-MCP-001",
                details={
                    "expected_aud": config.expected_aud,
                    "actual_aud": raw_aud,
                },
            )

    # ── Step 4: jti required and non-empty (F-08) ─────────────────────────────
    jti: str | None = payload.get("jti")
    if not jti:
        raise JWTValidationError(
            "Token is missing required 'jti' claim",
            error_id="ERR-AUTH-010",
            details={"sub": payload.get("sub"), "iss": payload.get("iss")},
        )

    # ── Step 5: required scope subset check ──────────────────────────────────
    if config.required_scopes:
        token_scopes: frozenset[str] = frozenset(payload.get("scope", "").split())
        missing: frozenset[str] = config.required_scopes - token_scopes
        if missing:
            raise JWTValidationError(
                f"Token missing required scopes: {sorted(missing)}",
                error_id="ERR-MCP-003",
                details={
                    "required": sorted(config.required_scopes),
                    "present": sorted(token_scopes),
                    "missing": sorted(missing),
                },
            )

    # ── Step 6: assemble JWTClaims ────────────────────────────────────────────
    raw_aud_value: str | list[str] = payload.get("aud", "")
    return JWTClaims(
        sub=str(payload.get("sub", "")),
        iss=str(payload.get("iss", "")),
        aud=raw_aud_value,
        exp=int(payload.get("exp", 0)),
        iat=int(payload.get("iat", 0)),
        jti=jti,
        act=payload.get("act"),
        scope=payload.get("scope"),
        aut=payload.get("aut"),
        # Sprint 4: read identity claims if present. _sanitise_user_string
        # strips control chars + Unicode line separators and caps length
        # (security audit F-03).
        username=_sanitise_user_string(payload.get("username"), max_len=64),
        email=_sanitise_user_string(payload.get("email"), max_len=256),
    )


# ── Private helpers ───────────────────────────────────────────────────────────


# Sprint 4 (security audit F-03): identity claim sanitisation.
# Strips control chars (\x00-\x1F except \t, \n, \r — but \n/\r are removed too
# because they would inject log-line breaks) and Unicode line/paragraph
# separators ( / , used in JS injection vectors). Caps length to
# bound log-line size + UI-render cost. Returns None on absent / non-string
# input — callers fail-closed if they need a non-None value.
_USER_STRING_FORBIDDEN = re.compile(r"[\x00-\x1F  ]")


def _sanitise_user_string(value: Any, *, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = _USER_STRING_FORBIDDEN.sub("", value).strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def _decode_unverified(token: str) -> dict[str, Any]:
    """Return payload without signature verification; used only for error detail extraction."""
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return {}


def _unverified_exp(token: str) -> int | None:
    payload = _decode_unverified(token)
    exp = payload.get("exp")
    return int(exp) if exp is not None else None
