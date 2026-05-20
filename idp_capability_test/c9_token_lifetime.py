#!/usr/bin/env python3
"""
C9 — Does CIBA-issued token come with a refresh_token? Can we extend session?

Architectural question per memo §7.2: when access_token expires (default
3600s), is there ANY refresh path, or does every >TTL session require a
fresh user consent?

The user's expert clarified: "use the issued token until lifetime expires,
don't extend." This probe just confirms empirically.

Approach: re-run CIBA with `scope=openid offline_access` (the standard
OIDC way to request a refresh_token). Check what's in the response.

Pass criteria (clarifying, not pass/fail):
  - If response has refresh_token → IS supports refresh on CIBA, but per
    expert we shouldn't use it.
  - If response has NO refresh_token → confirms what expert said.
"""

from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path

from _lib import Config, IdpClient, decode_jwt, dim, fail, hr, info, ok, warn

sys.path.insert(0, str(Path(__file__).parent))
from c8_ciba import mint_agent_token_via_authn  # type: ignore  # noqa: E402


C1_TOKEN_PATH = Path("/tmp/c1_pattern_c_token.txt")


def main() -> int:
    cfg = Config.load()
    client = IdpClient(cfg, verbose=False)
    s = client.session

    if not C1_TOKEN_PATH.exists():
        fail(f"{C1_TOKEN_PATH} not found — run c1_pattern_c.py first")
        return 2

    user_sub = decode_jwt(C1_TOKEN_PATH.read_text().strip()).get("sub")

    agent_b_id = cfg.raw["PROBE_AGENT_B_ID"]
    agent_b_secret = cfg.raw["PROBE_AGENT_B_SECRET"]
    agent_b_oauth_id = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_ID"]
    agent_b_oauth_secret = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_SECRET"]

    hr("C9 — CIBA + offline_access: does refresh_token come back?")

    # Mint actor_token
    actor_token = mint_agent_token_via_authn(
        client, cfg, agent_b_id, agent_b_secret, agent_b_oauth_id, agent_b_oauth_secret
    )

    # CIBA with offline_access
    hr("C9.1 — POST /oauth2/ciba with scope=openid offline_access")
    ciba_url = cfg.base_url + "/oauth2/ciba"
    r = s.post(
        ciba_url,
        auth=(agent_b_oauth_id, agent_b_oauth_secret),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "scope": "openid offline_access",
            "login_hint": user_sub,
            "binding_message": "C9: testing offline_access for refresh_token",
            "actor_token": actor_token,
            "notification_channel": "external",
        },
    )
    dim(f"  ← HTTP {r.status_code}")
    body = r.json()
    auth_req_id = body.get("auth_req_id")
    auth_url = body.get("auth_url")
    interval = body.get("interval", 2)
    expires_in = body.get("expires_in", 120)

    if not auth_req_id:
        fail(f"/ciba init failed: {body}")
        return 1
    ok("CIBA init OK")

    # User consent
    hr("C9.2 — Approve in browser")
    info(f"  {auth_url}")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    time.sleep(3)

    # Poll
    deadline = time.time() + min(expires_in, 240)
    response_body: dict = {}
    while time.time() < deadline:
        rr = s.post(
            cfg.token_url,
            auth=(agent_b_oauth_id, agent_b_oauth_secret),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "urn:openid:params:grant-type:ciba", "auth_req_id": auth_req_id},
        )
        b = rr.json()
        if b.get("access_token"):
            response_body = b
            break
        if b.get("error") in {"authorization_pending", "slow_down"}:
            time.sleep(interval)
            continue
        fail(f"poll error: {b.get('error')}")
        return 1

    if not response_body:
        fail("Timed out before user consented")
        return 1

    # Inspect for refresh_token
    hr("C9.3 — Token response inspection")
    has_refresh = "refresh_token" in response_body
    info(f"Response keys: {sorted(response_body.keys())}")
    info(f"refresh_token present: {has_refresh}")
    info(f"expires_in: {response_body.get('expires_in')}s")
    info(f"scope (returned): {response_body.get('scope')}")

    hr("C9 verdict")
    if has_refresh:
        rt = response_body["refresh_token"]
        ok(f"refresh_token IS issued: {rt[:20]}... (length {len(rt)})")
        info("→ IS DOES support refresh on CIBA when offline_access is requested.")
        info("→ Per user/expert decision: we will NOT use this in the demo.")
        info("→ Document as 'available but unused' — option for future sessions extension.")
        return 0

    ok("No refresh_token issued (expected per expert guidance)")
    info("→ Recovery on token expiry = full re-CIBA round-trip with new user consent.")
    info("→ Sprint 1 design: scope all agent operations to fit within 3600s TTL window.")
    info("→ Long-running tasks: explicit checkpointing required (out of scope for demo).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
