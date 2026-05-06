"""IT Server Configuration.

Mirror of hr-server/config.py — same env-loading pattern, same shape,
different audience. The `IT_SERVER_EXPECTED_AUD` is verified by spike P14.
"""
import os
from dotenv import load_dotenv

load_dotenv()

AUTH_ISSUER = os.getenv("AUTH_ISSUER")
EXPECTED_AUD = os.getenv("IT_SERVER_EXPECTED_AUD", "https://it-server.local/mcp")
JWKS_URL = os.getenv("JWKS_URL")
SSL_VERIFY = os.getenv("DISABLE_SSL_VERIFY", "").lower() != "true"

# Sprint 2 introspection (greenfield: ON from day 1 per milestone-plan §3.4 task 23)
INTROSPECT_ENABLED = os.getenv("IT_SERVER_INTROSPECT_ENABLED", "true").lower() == "true"
INTROSPECT_URL = os.getenv("ASGARDEO_INTROSPECT_URL")

# Nested act allowlist (chain depth 2: it-agent on top, orchestrator below)
TRUSTED_PEER_AGENTS = [
    p.strip()
    for p in os.getenv("IT_SERVER_TRUSTED_PEER_AGENTS", "it-agent,orchestrator-agent").split(",")
    if p.strip()
]

if not all([AUTH_ISSUER, JWKS_URL]):
    raise ValueError(
        "Missing required environment variables: AUTH_ISSUER or JWKS_URL"
    )

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3001,http://127.0.0.1:3001").split(",")
    if o.strip()
]

PORT = int(os.getenv("IT_SERVER_PORT", os.getenv("PORT", "8004")))
HOST = os.getenv("IT_SERVER_HOST", "0.0.0.0")
