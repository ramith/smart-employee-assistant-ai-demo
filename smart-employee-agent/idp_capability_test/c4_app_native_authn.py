#!/usr/bin/env python3
"""
C4 — App-Native Authentication 3-step flow (`/oauth2/authn`).

Validates ingredient I4: an agent can self-authenticate via the 3-step
direct flow that Asgardeo/WSO2-IS use for agent identities, returning
an access_token without any browser involvement.

This is the simplest agent-flow test:
  - No user
  - No API resource (uses built-in `openid internal_login` scopes)
  - No authorized actors / Pattern C
  - Just: agent OAuth App authenticates, agent ID/secret answer the challenge,
    auth code is exchanged for an access_token

Approach:
  1. POST /oauth2/authorize with response_mode=direct → returns flowId +
     authenticatorId
  2. POST /oauth2/authn with the flowId and (agent_id, agent_secret) as
     Username/Password → returns authorization code
  3. POST /oauth2/token with the code + PKCE verifier → returns access_token

Pass: response has access_token; decoded JWT has sub=<agent-id>,
      aut=AGENT (or APPLICATION_USER), iss matches our IdP.

Tests probe-agent-a only (b is identical config — no need to test twice).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
from pathlib import Path

import requests

from _lib import Config, IdpClient, dim, fail, hr, info, ok, show_jwt, warn


C4_TOKEN_PATH = Path("/tmp/c4_agent_a_token.txt")


def pkce_pair() -> tuple[str, str]:
    """Generate a fresh PKCE verifier + S256 challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def main() -> int:
    cfg = Config.load()
    client = IdpClient(cfg)
    s = client.session  # requests.Session with TLS bypass already configured

    if not cfg.raw.get("PROBE_AGENT_A_ID") or not cfg.raw.get("PROBE_AGENT_A_OAUTH_CLIENT_ID"):
        fail("PROBE_AGENT_A_* values missing in .substrate.env")
        return 2

    agent_id = cfg.raw["PROBE_AGENT_A_ID"]
    agent_secret = cfg.raw["PROBE_AGENT_A_SECRET"]
    oauth_client_id = cfg.raw["PROBE_AGENT_A_OAUTH_CLIENT_ID"]
    oauth_client_secret = cfg.raw["PROBE_AGENT_A_OAUTH_CLIENT_SECRET"]
    redirect_uri = cfg.raw.get("PROBE_AGENT_A_REDIRECT_URI", "http://localhost:9999/agent-callback")

    hr("C4 — Agent App-Native Auth (3-step /oauth2/authn flow)")
    info(f"Agent ID         : {agent_id}")
    info(f"OAuth Client ID  : {oauth_client_id}")
    info(f"Redirect URI     : {redirect_uri}")

    verifier, challenge = pkce_pair()

    # ─── Step 1: /oauth2/authorize with response_mode=direct ────────────
    hr("C4.1 — POST /oauth2/authorize (response_mode=direct)")
    r1 = s.post(
        cfg.authz_url,
        auth=(oauth_client_id, oauth_client_secret),
        headers={"Accept": "application/json"},
        data={
            "client_id": oauth_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid internal_login",
            "response_mode": "direct",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    dim(f"  ← HTTP {r1.status_code}")
    try:
        body1 = r1.json()
    except ValueError:
        fail(f"Non-JSON response: {r1.text[:500]}")
        return 1
    for line in str(body1).splitlines()[:30]:
        dim(f"    {line}")
    flow_id = body1.get("flowId")
    next_step = body1.get("nextStep") or {}
    authenticators = next_step.get("authenticators") or []
    authenticator_id = authenticators[0].get("authenticatorId") if authenticators else None

    if not flow_id or not authenticator_id:
        fail(f"No flowId / authenticatorId in /authorize response: {body1}")
        info("")
        info("Common causes:")
        info("  - PROBE_AGENT_A_OAUTH_CLIENT_ID / SECRET wrong")
        info("  - App-Native Authentication NOT enabled on the agent's OAuth App")
        info("    Console → Applications → probe-agent-a → Advanced tab → toggle on")
        info(f"  - redirect_uri not registered exactly as '{redirect_uri}'")
        info("  - Scope 'openid internal_login' not available on this OAuth App")
        return 1
    ok(f"flowId={flow_id}")
    ok(f"authenticatorId={authenticator_id}")

    # ─── Step 2: /oauth2/authn with Agent ID + Secret ───────────────────
    hr("C4.2 — POST /oauth2/authn (Agent ID/Secret as username/password)")
    r2 = s.post(
        cfg.authn_url,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={
            "flowId": flow_id,
            "selectedAuthenticator": {
                "authenticatorId": authenticator_id,
                "params": {"username": agent_id, "password": agent_secret},
            },
        },
    )
    dim(f"  ← HTTP {r2.status_code}")
    try:
        body2 = r2.json()
    except ValueError:
        fail(f"Non-JSON response: {r2.text[:500]}")
        return 1
    import json as _json
    for line in _json.dumps(body2, indent=2).splitlines():
        dim(f"    {line}")
    code = (body2.get("authData") or {}).get("code") or body2.get("code")
    if not code:
        fail(f"No code in /authn response: {body2}")
        info("")
        info("Common causes:")
        info("  - Agent ID or Agent Secret wrong")
        info("  - Agent has been blocked / disabled")
        info("  - Agent's role assignment missing required scopes")
        return 1
    ok(f"auth code={code}")

    # ─── Step 3: /oauth2/token (Basic auth required) ────────────────────
    hr("C4.3 — POST /oauth2/token (auth code + PKCE verifier)")
    r3 = s.post(
        cfg.token_url,
        auth=(oauth_client_id, oauth_client_secret),
        headers={"Accept": "application/json"},
        data={
            "grant_type": "authorization_code",
            "client_id": oauth_client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
    )
    dim(f"  ← HTTP {r3.status_code}")
    try:
        body3 = r3.json()
    except ValueError:
        fail(f"Non-JSON response: {r3.text[:500]}")
        return 1
    for line in _json.dumps(body3, indent=2).splitlines()[:30]:
        dim(f"    {line}")
    token = body3.get("access_token")
    if not token:
        fail(f"No access_token in /token response: {body3.get('error')}")
        return 1
    ok(f"access_token (first 40): {token[:40]}...")
    C4_TOKEN_PATH.write_text(token)
    ok(f"saved to {C4_TOKEN_PATH}")

    payload = show_jwt(token, "C4.4 — Decoded agent token")

    # ─── Validate ───────────────────────────────────────────────────────
    hr("C4 verdict")
    sub = payload.get("sub")
    aut = payload.get("aut")
    iss = payload.get("iss")
    aud = payload.get("aud")
    scope = payload.get("scope")

    info(f"sub  = {sub}")
    info(f"aut  = {aut}")
    info(f"iss  = {iss}")
    info(f"aud  = {aud}")
    info(f"scope= {scope}")

    issues: list[str] = []
    if sub != agent_id:
        issues.append(f"sub ({sub}) does not equal agent_id ({agent_id})")
    # In WSO2 IS the OIDC issuer equals the token endpoint URL.
    if iss != cfg.token_url:
        issues.append(f"iss ({iss}) does not match token endpoint ({cfg.token_url})")

    if issues:
        for i in issues:
            warn(i)
        fail("C4 produced a token but key claims didn't match expectations")
        return 1

    ok("sub == agent_id ✓")
    ok("iss == configured issuer ✓")
    if aut == "AGENT":
        ok("aut == AGENT ✓ (matches Asgardeo behavior)")
    else:
        warn(f"aut == {aut} (expected 'AGENT'; not blocking but worth noting)")

    hr("C4 PASS — agent self-authenticates via 3-step /oauth2/authn")
    info("→ Ingredient I4 confirmed. Agents can mint actor_tokens without browser.")
    info("→ This token (saved to /tmp/c4_agent_a_token.txt) is now usable as actor_token in Pattern C / TX tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
