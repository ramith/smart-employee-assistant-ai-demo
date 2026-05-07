#!/usr/bin/env python3
"""
C10 — Single-resource CIBA against a registered MCP Server.

Question this probe answers (refines F-06):

  When the IS Console has a registered MCP Server (e.g., Identifier
  `mcp://probe-hr-server.local`), and a CIBA `/oauth2/ciba` POST includes
  EXACTLY ONE `resource=mcp://probe-hr-server.local` parameter, what does
  IS issue as the `aud` claim in the resulting JWT?

  F-06 (from c5_multi_audience_ciba.py) tested MULTIPLE `resource=` params
  and found IS silently ignored them; aud collapsed to the calling OAuth
  Client ID. F-06 did NOT test the single-resource case against a
  registered MCP Server entity.

This probe is the empirical input to the Path C Hybrid decision
(Sprint 1 build state memo). Outcomes:

  aud == "mcp://probe-hr-server.local"  → Path B clean (MCP Server registration works)
  aud == "<oauth_client_id>"            → Path A confirmed (F-06 holds even single-resource)
  aud == [client_id, mcp_uri]           → Path C native (true hybrid)
  HTTP 400 "invalid_target"             → Path A; resource needs allowlisting

Prereq: register `mcp://probe-hr-server.local` in IS Console
  Console → API Resources → + New MCP Server → Identifier=mcp://probe-hr-server.local
  Add scope `probe.read` (any scope works; we just need the resource registered).
  Optionally subscribe probe-agent-b's Agent App to it.

  Add to .substrate.env:  PROBE_MCP_SERVER_URI=mcp://probe-hr-server.local

Approach: clone of c8_ciba.py with one new line — `"resource": cfg.raw["PROBE_MCP_SERVER_URI"]`
in the /oauth2/ciba POST body. After polling completes, decode the issued
token and report `aud` shape.
"""
from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path

from _lib import Config, IdpClient, decode_jwt, dim, fail, hr, info, ok, show_jwt, warn

sys.path.insert(0, str(Path(__file__).parent))
from c8_ciba import mint_agent_token_via_authn  # type: ignore  # noqa: E402


C10_TOKEN_PATH = Path("/tmp/c10_ciba_single_resource_token.txt")
CIBA_ENDPOINT = "/oauth2/ciba"
TOKEN_ENDPOINT = "/oauth2/token"


def main() -> int:
    cfg = Config.load()
    client = IdpClient(cfg, verbose=False)
    s = client.session

    # Read user_sub from prior C1 token (so login_hint is a known-valid value)
    c1_path = Path("/tmp/c1_pattern_c_token.txt")
    if not c1_path.exists():
        fail(f"{c1_path} not found — run c1_pattern_c.py first to capture a user UUID")
        return 2
    inbound = decode_jwt(c1_path.read_text().strip())
    user_sub = inbound.get("sub")

    mcp_uri = cfg.raw.get("PROBE_MCP_SERVER_URI", "").strip()
    if not mcp_uri:
        fail("PROBE_MCP_SERVER_URI not set in .substrate.env")
        info("  Register an MCP Server in IS Console with identifier `mcp://probe-hr-server.local`,")
        info("  then add `PROBE_MCP_SERVER_URI=mcp://probe-hr-server.local` to .substrate.env.")
        return 2
    # Scope name on the MCP Server (probe.read was taken, so user used probe.hr_mcp_read)
    mcp_scope = cfg.raw.get("PROBE_MCP_SERVER_SCOPE", "probe.hr_mcp_read").strip()

    agent_b_id = cfg.raw["PROBE_AGENT_B_ID"]
    agent_b_secret = cfg.raw["PROBE_AGENT_B_SECRET"]
    agent_b_oauth_id = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_ID"]
    agent_b_oauth_secret = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_SECRET"]

    hr("C10 — Single-resource CIBA against registered MCP Server")
    info(f"  resource (single)   : {mcp_uri}")
    info(f"  login_hint          : {user_sub}")
    info(f"  authenticated as    : probe-agent-b's Agent App ({agent_b_oauth_id})")

    # ─── Mint actor_token ───────────────────────────────────────────────
    hr("C10.1 — Mint probe-agent-b actor_token")
    try:
        actor_token = mint_agent_token_via_authn(
            client, cfg, agent_b_id, agent_b_secret, agent_b_oauth_id, agent_b_oauth_secret
        )
    except RuntimeError as e:
        fail(f"Could not mint actor_token: {e}")
        return 1
    ok("actor_token minted")

    # ─── POST /oauth2/ciba with single resource ─────────────────────────
    hr("C10.2 — POST /oauth2/ciba with resource=" + mcp_uri)
    ciba_url = cfg.base_url + CIBA_ENDPOINT

    r_ciba = s.post(
        ciba_url,
        auth=(agent_b_oauth_id, agent_b_oauth_secret),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "scope": f"openid {mcp_scope}",
            "login_hint": user_sub,
            "binding_message": "C10 single-resource CIBA test",
            "actor_token": actor_token,
            "notification_channel": "external",
            "resource": mcp_uri,
        },
    )
    dim(f"  ← HTTP {r_ciba.status_code}")
    try:
        ciba_body = r_ciba.json()
    except ValueError:
        fail(f"Non-JSON /ciba response: {r_ciba.text[:500]}")
        return 1

    import json as _json
    for line in _json.dumps(ciba_body, indent=2).splitlines():
        dim(f"    {line}")

    auth_req_id = ciba_body.get("auth_req_id")
    auth_url = ciba_body.get("auth_url")
    interval = ciba_body.get("interval", 2)
    expires_in = ciba_body.get("expires_in", 120)

    if not auth_req_id:
        err = ciba_body.get("error", "unknown")
        msg = ciba_body.get("error_description", "")
        fail(f"/ciba did NOT return auth_req_id: error={err} description={msg}")
        info("")
        if err == "invalid_target" or "resource" in msg.lower():
            info("→ Outcome: HTTP 400 / invalid_target → Path A (resource needs allowlisting)")
            info("  IS rejects unknown/unauthorized MCP Server URIs.")
            info("  Either subscribe probe-agent-b's Agent App to the MCP Server in Console,")
            info("  or accept Path A and skip MCP Server registration in Sprint 1.")
        elif err == "unauthorized_client":
            info("→ CIBA grant not enabled on probe-agent-b's Agent App.")
        return 1

    ok(f"auth_req_id received: {auth_req_id}")
    if auth_url:
        ok("auth_url received")

    # ─── Open browser, prompt user to approve ───────────────────────────
    hr("C10.3 — Approve in browser")
    info(f"  {auth_url}")
    info(f"  Login as {cfg.probe_user_username} / {cfg.probe_user_password}")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    time.sleep(3)

    # ─── Poll /oauth2/token ─────────────────────────────────────────────
    hr("C10.4 — Poll /oauth2/token")
    deadline = time.time() + min(expires_in, 240)
    exchanged: str | None = None
    while time.time() < deadline:
        rr = s.post(
            cfg.base_url + TOKEN_ENDPOINT,
            auth=(agent_b_oauth_id, agent_b_oauth_secret),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "urn:openid:params:grant-type:ciba",
                "auth_req_id": auth_req_id,
            },
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

    C10_TOKEN_PATH.write_text(exchanged)
    payload = show_jwt(exchanged, "C10.5 — Decoded token")

    # ─── Inspect aud ────────────────────────────────────────────────────
    hr("C10 verdict — what is `aud`?")
    aud = payload.get("aud")
    info(f"  aud type  : {type(aud).__name__}")
    info(f"  aud value : {aud}")
    info(f"  expected MCP URI         : {mcp_uri}")
    info(f"  expected client_id       : {agent_b_oauth_id}")

    aud_set = set(aud) if isinstance(aud, list) else {aud} if isinstance(aud, str) else set()

    if mcp_uri in aud_set and agent_b_oauth_id in aud_set:
        ok("aud array contains BOTH mcp_uri AND client_id → Path C (true hybrid)")
        info("  Validator accepts frozenset({client_id, mcp_uri}); both audit chains valid.")
        result = "PATH_C_HYBRID"
    elif mcp_uri in aud_set and agent_b_oauth_id not in aud_set:
        ok("aud == mcp_uri (only) → Path B (MCP Server registration works cleanly)")
        info("  Validator should use mcp://... URI as expected_aud.")
        result = "PATH_B_CLEAN"
    elif agent_b_oauth_id in aud_set and mcp_uri not in aud_set:
        warn("aud == client_id only → Path A (F-06 confirmed even for single-resource)")
        info("  IS silently ignores `resource=` even single-value on CIBA.")
        info("  Stay with current architecture; MCP Server registration is metadata-only.")
        result = "PATH_A_F06_HOLDS"
    else:
        fail(f"Unexpected aud shape: {aud}")
        result = "UNEXPECTED"
        return 1

    info("")
    info(f"  → {result}")
    info("  Update F-17 in docs/architecture/sprint-1-fixes.md with this finding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
