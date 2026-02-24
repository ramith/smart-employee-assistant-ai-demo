"""
  JWT Token Validation Module

  Validates JWT tokens using JWKS (JSON Web Key Set) fetched from the
  authorization server. Supports RS256 algorithm with key caching.
"""

import jwt
from jwt.algorithms import RSAAlgorithm
import httpx
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class JWTValidator:
    """
    JWT token validator using JWKS.
    Fetches and caches JWKS keys for performance.
    """

    def __init__(self, jwks_url: str, issuer: str, audience: str, ssl_verify: bool = True):
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.audience = audience
        self.ssl_verify = ssl_verify
        self._jwks_cache: Optional[Dict[str, Any]] = None

    async def _fetch_jwks(self) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(verify=self.ssl_verify) as client:
                response = await client.get(self.jwks_url)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"Failed to fetch JWKS from {self.jwks_url}: {e}")
            raise

    async def _get_jwks(self) -> Dict[str, Any]:
        if self._jwks_cache is None:
            self._jwks_cache = await self._fetch_jwks()
        return self._jwks_cache

    def _get_signing_key(self, token_header: Dict[str, Any], jwks: Dict[str, Any]) -> str:
        kid = token_header.get('kid')
        if not kid:
            raise ValueError("Token header missing 'kid' field")

        for key in jwks.get('keys', []):
            if key.get('kid') == kid:
                return RSAAlgorithm.from_jwk(key)

        raise ValueError(f"Unable to find matching key for kid: {kid}")

    async def validate_token(self, token: str) -> Dict[str, Any]:
        try:
            unverified_header = jwt.get_unverified_header(token)
            jwks = await self._get_jwks()
            signing_key = self._get_signing_key(unverified_header, jwks)

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
            raise ValueError("Token has expired")
        except jwt.InvalidAudienceError:
            raise ValueError("Invalid audience")
        except jwt.InvalidIssuerError:
            raise ValueError("Invalid issuer")
        except jwt.InvalidSignatureError:
            raise ValueError("Invalid token signature")
        except jwt.DecodeError:
            raise ValueError("Invalid token format")
        except Exception as e:
            logger.error(f"Token validation error: {e}")
            raise ValueError(f"Token validation failed: {e}")
