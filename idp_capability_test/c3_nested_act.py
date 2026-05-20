#!/usr/bin/env python3
"""
C3 — Depth-2 nested act via RFC 8693 token-exchange.

Validates ingredient I2 — the prize the WSO2 expert's "Multi-Agent
Authorization" slide demonstrates. Without it, the orchestrator →
specialist-agent → MCP-server architecture loses end-to-end audit.

Approach:
  1. Read C1's Pattern C output from /tmp/c1_pattern_c_token.txt
     (sub=probe.user, act.sub=probe-agent-a).
  2. Mint probe-agent-b's actor_token via App-Native Auth (3-step,
     identical to c4 but for agent-b).
  3. POST /oauth2/token with grant_type=token-exchange,
     subject_token=<C1 token>, actor_token=<agent-b's token>,
     authenticated as probe-agent-b's OAuth app.
  4. Decode result. Expect:
       sub=probe.user
       act.sub=probe-agent-b
       act.act.sub=probe-agent-a

Pass: depth-2 nested act in result.
Fail: act flat (just new actor; previous lost), no act, or error.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
from pathlib import Path

import requests

from _lib import Config, IdpClient, act_chain, decode_jwt, dim, fail, hr, info, ok, show_jwt, warn


C1_TOKEN_PATH = Path("/tmp/c1_pattern_c_token.txt")
C3_TOKEN_PATH = Path("/tmp/c3_chained_token.txt")


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def mint_agent_token_via_authn(
    client: IdpClient,
    cfg: Config,
    agent_id: str,
    agent_secret: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    redirect_uri: str = "http://localhost:9999/agent-callback",
) -> str:
    """3-step App-Native Auth (identical to c4) to mint a fresh agent token."""
    s = client.session
    verifier, challenge = pkce_pair()

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
    body1 = r1.json()
    flow_id = body1.get("flowId")
    auth_id = (body1.get("nextStep") or {}).get("authenticators", [{}])[0].get("authenticatorId")
    if not flow_id or not auth_id:
        raise RuntimeError(f"/authorize failed: {body1}")

    r2 = s.post(
        cfg.authn_url,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={
            "flowId": flow_id,
            "selectedAuthenticator": {
                "authenticatorId": auth_id,
                "params": {"username": agent_id, "password": agent_secret},
            },
        },
    )
    body2 = r2.json()
    code = (body2.get("authData") or {}).get("code") or body2.get("code")
    if not code:
        raise RuntimeError(f"/authn failed: {body2}")

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
    body3 = r3.json()
    token = body3.get("access_token")
    if not token:
        raise RuntimeError(f"/token failed: {body3}")
    return token


def main() -> int:
    cfg = Config.load()
    client = IdpClient(cfg, verbose=False)

    if not C1_TOKEN_PATH.exists():
        fail(f"{C1_TOKEN_PATH} not found — run c1_pattern_c.py first")
        return 2
    if not cfg.raw.get("PROBE_AGENT_B_ID"):
        fail("PROBE_AGENT_B_* values missing in .substrate.env")
        return 2

    agent_a_id = cfg.raw["PROBE_AGENT_A_ID"]
    agent_b_id = cfg.raw["PROBE_AGENT_B_ID"]
    agent_b_secret = cfg.raw["PROBE_AGENT_B_SECRET"]
    agent_b_oauth_id = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_ID"]
    agent_b_oauth_secret = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_SECRET"]

    hr("C3 — Depth-2 nested act via RFC 8693")
    info(f"Subject token : C1 Pattern C output (sub=probe.user, act.sub=probe-agent-a)")
    info(f"New actor     : probe-agent-b ({agent_b_id}) — identity via actor_token")
    info(f"Authenticator : probe-client-b ({cfg.probe_client_b_id})")
    info(f"  (Finding F3: Agent Apps lack Token Exchange grant; a Standard-Based/MCP")
    info(f"   Client App must authenticate the TX request. Actor identity is decoupled.)")

    # ─── 1) Subject = C1 output ─────────────────────────────────────────
    hr("C3.1 — Load C1's depth-1 token as subject_token")
    subject_token = C1_TOKEN_PATH.read_text().strip()
    sp = decode_jwt(subject_token)
    info(f"  sub      = {sp.get('sub')}")
    info(f"  act.sub  = {sp.get('act', {}).get('sub')}")
    info(f"  aut      = {sp.get('aut')}")
    if sp.get("act", {}).get("sub") != agent_a_id:
        warn("Subject token's act.sub doesn't equal agent-a's id — re-run c1?")

    # ─── 2) New actor: probe-agent-b actor_token via App-Native Auth ────
    hr("C3.2 — Mint probe-agent-b actor_token (App-Native Auth, 3-step)")
    try:
        actor_token = mint_agent_token_via_authn(
            client, cfg, agent_b_id, agent_b_secret, agent_b_oauth_id, agent_b_oauth_secret
        )
    except RuntimeError as e:
        fail(f"Could not mint probe-agent-b token: {e}")
        return 1
    ap = decode_jwt(actor_token)
    ok(f"actor_token minted; sub={ap.get('sub')}, aut={ap.get('aut')}")

    # ─── 3) RFC 8693 chained exchange ───────────────────────────────────
    # Empirical: try authenticator = probe-client-a (matches subject_token's aud)
    # WSO2 IS may enforce audience-binding for impersonation chain-of-custody.
    hr("C3.3 — POST /oauth2/token (probe-client-a authenticates — matches subject aud)")
    s = client.session
    r = s.post(
        cfg.token_url,
        auth=(cfg.probe_client_a_id, cfg.probe_client_a_secret),
        headers={"Accept": "application/json"},
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "actor_token": actor_token,
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "scope": "openid",
        },
    )
    dim(f"  ← HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        fail(f"Non-JSON response: {r.text[:500]}")
        return 1

    import json as _json
    for line in _json.dumps(body, indent=2).splitlines()[:30]:
        dim(f"    {line}")

    exchanged = body.get("access_token")
    if not exchanged:
        err = body.get("error", "unknown")
        msg = body.get("error_description", "")
        fail(f"Exchange failed: error={err} description={msg}")
        info("")
        if "impersonator" in msg.lower() or "actor" in msg.lower():
            info("→ TX engine couldn't validate the impersonation chain.")
            info("  Check that subject_token's act claim is valid (re-run c1?).")
        elif err == "unauthorized_client":
            info("→ Token Exchange grant not enabled on probe-agent-b's OAuth app.")
            info("  Console → Applications → AGENT-941a5f32-... → Protocol → ☑ Token Exchange.")
        elif err == "invalid_grant":
            info("→ subject_token or actor_token rejected (likely expired). Re-run c1.")
        return 1
    C3_TOKEN_PATH.write_text(exchanged)
    ok(f"Chained token saved to {C3_TOKEN_PATH}")

    payload = show_jwt(exchanged, "C3.4 — Decoded chained token")

    # ─── 4) Validate depth-2 ────────────────────────────────────────────
    hr("C3 verdict")
    sub = payload.get("sub")
    chain = act_chain(payload)
    info(f"sub = {sub}")
    info(f"act chain (outer → inner): {chain}")

    if not payload.get("act"):
        fail("act claim absent")
        return 1

    if len(chain) >= 2:
        if chain[0] == agent_b_id and chain[1] == agent_a_id:
            ok(f"act.sub == probe-agent-b ({agent_b_id}) ✓")
            ok(f"act.act.sub == probe-agent-a ({agent_a_id}) ✓")
            ok("Nested act preserves prior delegation — depth-2 confirmed")
            hr("C3 PASS — RFC 8693 chains delegation on this WSO2 IS install")
            info("")
            info("→ Ingredient I2 confirmed.")
            info("→ The v3 architecture (orchestrator → specialist → MCP) is FULLY GREENLIT.")
            info("→ Audit story = full delegation chain captured natively.")
            info("→ The Asgardeo SaaS blocker has no equivalent on on-prem WSO2 IS 7.2.")
            return 0
        warn(f"Chain has 2 entries but values unexpected: {chain}")
        return 1

    if len(chain) == 1:
        fail(f"Result has only depth-1 act ({chain[0]}); previous actor was DROPPED")
        info("→ WSO2 IS flattens the act chain on re-exchange.")
        info("→ Architectural decision: drop specialist tier OR accept depth-1 audit only.")
        return 1

    fail(f"Unexpected act shape — chain={chain}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
