"""IT REST JWT Validator — Sprint 4 S4.0 reconciliation.

  Mirrors ``hr_server/auth/jwt_validator.py`` verbatim with IT-specific config
  wiring. Validates JWT tokens used on the IT REST surface (browser SPA token,
  optionally orchestrator MCP-client audience) using JWKS fetched from the
  authorization server.

  Audience handling: accepts a list of audiences, capped at ≤3 entries
  (security audit F-01). The list is composed at module load time from
  ``ITServerConfig.expected_aud`` plus optional extras read from
  ``IT_SERVER_REST_VALID_AUDIENCES``. Fail-closed at startup if cap is
  exceeded; every accepted audience is logged at INFO during construction.

  The MCP-tool path uses ``it_server/auth/validators.py`` and stays on the
  strict single-aud check — this REST validator is REST-only.

  Error types:
    token_expired  - JWT has passed its expiration time
    invalid_token  - JWT signature verification failed or token is malformed
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Union

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger(__name__)

# ── Audience cap (F-01) ──────────────────────────────────────────────────────

#: Hard cap on accepted audiences. Natural ceiling: own CLIENT_ID + optional
#: SPA_CLIENT_ID + optional orchestrator MCP client. Configurations exceeding
#: this fail-closed at startup.
_AUDIENCE_CAP: int = 3


class TokenError(Exception):
    """Structured token validation error with an error type identifier."""

    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        self.message = message
        super().__init__(message)


class JWTValidator:
    """JWT token validator using JWKS, with audience-list support.

    Fetches and caches JWKS keys for performance. Refetches once on a kid
    miss to tolerate IdP key rotation without a service restart.
    """

    def __init__(
        self,
        jwks_url: str,
        issuer: str,
        audience: Union[str, list[str]],
        ssl_verify: bool = True,
    ):
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.audience = audience
        self.ssl_verify = ssl_verify
        self._jwks_cache: Optional[dict[str, Any]] = None

    async def _fetch_jwks(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(verify=self.ssl_verify) as client:
                response = await client.get(self.jwks_url)
                response.raise_for_status()
                self._jwks_cache = response.json()
                return self._jwks_cache
        except Exception as e:
            logger.error(f"Failed to fetch JWKS from {self.jwks_url}: {e}")
            raise

    async def _get_jwks(self) -> dict[str, Any]:
        if self._jwks_cache is None:
            await self._fetch_jwks()
        return self._jwks_cache

    def _find_key_for_kid(self, kid: str, jwks: dict[str, Any]):
        for key in jwks.get('keys', []):
            if key.get('kid') == kid:
                return RSAAlgorithm.from_jwk(key)
        return None

    async def _get_signing_key(self, token_header: dict[str, Any]):
        """Resolve the signing key for the token, refreshing JWKS once on a kid miss
        so we tolerate IdP key rotation without requiring a service restart."""
        kid = token_header.get('kid')
        if not kid:
            raise TokenError("invalid_token", "Token header missing 'kid' field")

        jwks = await self._get_jwks()
        key = self._find_key_for_kid(kid, jwks)
        if key is not None:
            return key

        # kid not in cached JWKS — IdP may have rotated keys. Refetch once.
        logger.info(f"Unknown kid '{kid}' in cached JWKS; refetching from IdP")
        jwks = await self._fetch_jwks()
        key = self._find_key_for_kid(kid, jwks)
        if key is not None:
            return key

        raise TokenError("invalid_token", f"Unable to find matching key for kid: {kid}")

    async def validate_token(self, token: str) -> dict[str, Any]:
        """Validate a JWT token and return the payload.

        Checks signature (RS256), expiry, issuer, and audience.
        Raises TokenError on any validation failure.
        """
        try:
            unverified_header = jwt.get_unverified_header(token)
            signing_key = await self._get_signing_key(unverified_header)

            payload = jwt.decode(
                token,
                signing_key,
                algorithms=['RS256'],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": True,
                }
            )

            return payload

        except jwt.ExpiredSignatureError:
            raise TokenError("token_expired", "Token has expired")
        except jwt.InvalidAudienceError:
            try:
                unverified = jwt.decode(token, options={"verify_signature": False})
                actual_aud = unverified.get("aud")
            except Exception:
                actual_aud = "(undecodable)"
            logger.warning(
                "JWT audience mismatch: token_aud=%s expected_aud=%s",
                actual_aud, self.audience,
            )
            raise TokenError(
                "invalid_token",
                f"Invalid audience (token had {actual_aud}, expected {self.audience})",
            )
        except jwt.InvalidIssuerError:
            try:
                unverified = jwt.decode(token, options={"verify_signature": False})
                actual_iss = unverified.get("iss")
            except Exception:
                actual_iss = "(undecodable)"
            logger.warning(
                "JWT issuer mismatch: token_iss=%s expected_iss=%s",
                actual_iss, self.issuer,
            )
            raise TokenError(
                "invalid_token",
                f"Invalid issuer (token had {actual_iss}, expected {self.issuer})",
            )
        except jwt.InvalidSignatureError:
            raise TokenError("invalid_token", "Invalid token signature")
        except jwt.DecodeError:
            raise TokenError("invalid_token", "Invalid token format")
        except TokenError:
            raise
        except Exception as e:
            logger.error(f"Token validation error: {e}")
            raise TokenError("invalid_token", f"Token validation failed: {e}")


def build_audiences(
    expected_aud: str,
    extras_env: Optional[str] = None,
) -> list[str]:
    """Compose the REST audience list with the F-01 ≤3-entry cap.

    Inputs:
      expected_aud: the IT server's own OAuth Client ID (always entry 0).
      extras_env: comma-separated extras (typically read from
        ``IT_SERVER_REST_VALID_AUDIENCES``). May be ``None`` or empty.

    Returns:
      Deduplicated list preserving order: ``[expected_aud, *extras]``.

    Raises:
      ValueError: if the resulting list exceeds ``_AUDIENCE_CAP`` entries.
        Fails closed so misconfiguration is visible at startup, not silently
        broadening the trust set (see security audit F-01 / OQ-3 path D).
    """
    audiences: list[str] = [expected_aud]
    if extras_env:
        for raw in extras_env.split(","):
            item = raw.strip()
            if not item or item in audiences:
                continue
            audiences.append(item)
    if len(audiences) > _AUDIENCE_CAP:
        raise ValueError(
            f"IT REST audience list exceeds cap of {_AUDIENCE_CAP}: got "
            f"{len(audiences)} entries={audiences}. Trim "
            "IT_SERVER_REST_VALID_AUDIENCES (security audit F-01)."
        )
    return audiences


def build_validator_from_config(cfg: object) -> JWTValidator:
    """Construct a configured ``JWTValidator`` from an ``ITServerConfig``.

    Reads the optional comma-separated env var
    ``IT_SERVER_REST_VALID_AUDIENCES`` and appends it to ``cfg.expected_aud``
    (capped at ≤3 entries — F-01). Logs every accepted audience at INFO.
    """
    extras_env = os.getenv("IT_SERVER_REST_VALID_AUDIENCES", "").strip() or None
    audiences = build_audiences(cfg.expected_aud, extras_env)  # type: ignore[union-attr]

    logger.info(
        "validator.startup expected_audiences=%s",
        audiences,
    )

    return JWTValidator(
        jwks_url=cfg.is_jwks_url,  # type: ignore[union-attr]
        issuer=cfg.is_issuer,  # type: ignore[union-attr]
        audience=audiences,
        ssl_verify=not cfg.is_insecure_tls,  # type: ignore[union-attr]
    )
