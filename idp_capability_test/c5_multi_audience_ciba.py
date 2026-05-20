#!/usr/bin/env python3
"""
C5 — Can a single CIBA flow bind multiple `aud` values?

Architectural impact: if YES, hr_agent does ONE consent prompt per
user request and gets a token usable at both `hr-a2a` and `hr-mcp`.
If NO, every specialist needs TWO CIBA flows per invocation — doubles
the user-consent count and roughly doubles the latency budget.

Approach: identical to c8_ciba.py except the /oauth2/ciba POST passes
multiple `resource=` params (RFC 8707). After polling completes,
inspect `aud` claim on the issued token.

Pass: aud is an array containing both requested resources.
Fail (acceptable, just costly): aud is a single string — only one
resource bound. Means we plan for two CIBA flows per specialist.
"""

from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path

import requests

from _lib import Config, IdpClient, decode_jwt, dim, fail, hr, info, ok, show_jwt, warn

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

    inbound = decode_jwt(C1_TOKEN_PATH.read_text().strip())
    user_sub = inbound.get("sub")

    agent_b_id = cfg.raw["PROBE_AGENT_B_ID"]
    agent_b_secret = cfg.raw["PROBE_AGENT_B_SECRET"]
    agent_b_oauth_id = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_ID"]
    agent_b_oauth_secret = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_SECRET"]

    primary = cfg.probe_api_audience
    secondary = cfg.probe_api_secondary_audience

    hr("C5 — Multi-audience CIBA (one consent → multiple aud)")
    info(f"Login hint   : {user_sub}")
    info(f"Resources    : [{primary!r}, {secondary!r}]")
    info(f"Note: secondary may not be a registered API resource — IS may reject")

    # Mint actor_token
    hr("C5.1 — Mint probe-agent-b actor_token")
    actor_token = mint_agent_token_via_authn(
        client, cfg, agent_b_id, agent_b_secret, agent_b_oauth_id, agent_b_oauth_secret
    )
    ok("actor_token minted")

    # CIBA initiation with TWO resource params
    hr("C5.2 — POST /oauth2/ciba with multiple resource= params")
    ciba_url = cfg.base_url + "/oauth2/ciba"
    payload: list[tuple[str, str]] = [
        ("scope", "openid"),
        ("login_hint", user_sub),
        ("binding_message", "C5: multi-audience consent test"),
        ("actor_token", actor_token),
        ("notification_channel", "external"),
        ("resource", primary),
        ("resource", secondary),
    ]
    r = s.post(
        ciba_url,
        auth=(agent_b_oauth_id, agent_b_oauth_secret),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    dim(f"  ← HTTP {r.status_code}")
    body = r.json()
    import json as _json
    for line in _json.dumps(body, indent=2).splitlines()[:20]:
        dim(f"    {line}")

    auth_req_id = body.get("auth_req_id")
    auth_url = body.get("auth_url")
    interval = body.get("interval", 2)
    expires_in = body.get("expires_in", 120)

    if not auth_req_id:
        fail(f"/ciba failed: error={body.get('error')} description={body.get('error_description')}")
        info("  → If 'invalid_resource', secondary URN is not a registered API resource.")
        info("    Try registering urn:probe:other in Console → API Resources to retest.")
        return 1
    ok(f"auth_req_id received; auth_url={'present' if auth_url else 'MISSING'}")

    # User consent
    hr("C5.3 — Open consent URL — Approve in browser")
    info(f"  {auth_url}")
    info(f"  Login as {cfg.probe_user_username} / {cfg.probe_user_password}")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    time.sleep(3)

    # Poll
    hr("C5.4 — Poll for token")
    deadline = time.time() + min(expires_in, 240)
    exchanged: str | None = None
    while time.time() < deadline:
        rr = s.post(
            cfg.token_url,
            auth=(agent_b_oauth_id, agent_b_oauth_secret),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "urn:openid:params:grant-type:ciba", "auth_req_id": auth_req_id},
        )
        b = rr.json()
        if b.get("access_token"):
            exchanged = b["access_token"]
            break
        if b.get("error") in {"authorization_pending", "slow_down"}:
            time.sleep(interval)
            continue
        fail(f"poll error: {b.get('error')} {b.get('error_description', '')}")
        return 1

    if not exchanged:
        fail("Timed out before user consented")
        return 1

    payload_jwt = show_jwt(exchanged, "C5.5 — Decoded token")

    # Inspect aud
    hr("C5 verdict")
    aud = payload_jwt.get("aud")
    info(f"aud type  : {type(aud).__name__}")
    info(f"aud value : {aud}")

    if isinstance(aud, list):
        present = [r for r in (primary, secondary) if r in aud]
        if len(present) == 2:
            ok(f"aud is array with BOTH requested resources: {present}")
            hr("C5 PASS — multi-audience CIBA works on this IS install")
            info("→ One CIBA per specialist suffices. Sprint 1 latency budget unchanged.")
            return 0
        if len(present) == 1:
            warn(f"aud is array but only contains: {present}")
            info("→ IS honored only one resource. Plan for one CIBA per audience.")
            return 1
        fail(f"aud array contains neither requested resource")
        return 1

    if isinstance(aud, str):
        warn(f"aud is a single string: {aud}")
        info("→ IS collapsed to one audience. Multi-audience CIBA NOT supported on this build.")
        info("→ Architectural implication: each specialist needs TWO CIBA flows per invocation,")
        info("  one for `_a2a` audience and one for `_mcp` audience. Doubles the consent count.")
        info("  Sprint 1 must account for this. Document as Finding F6.")
        return 1

    fail(f"aud has unexpected shape: {aud}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
