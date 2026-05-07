#!/usr/bin/env python3
"""
C8 — CIBA flow: per-agent depth-1 OBO via Client Initiated Backchannel Auth.

Validates the architectural pivot decided 2026-05-07 with the WSO2 expert:
since depth-2 nested `act` is NOT supported on WSO2 IS 7.2, each specialist
agent runs its OWN CIBA flow when invoked. The user consents per-agent at
runtime; each agent gets a depth-1 OBO token directly from IS.

This probe simulates the new pattern with our existing substrate:
  - probe-agent-a stands in for `orchestrator-agent` — its identity is the
    `act.sub` in the C1 token we already minted; from that token we extract
    `sub` = probe.user's UUID for the `login_hint`.
  - probe-agent-b stands in for `hr_agent` — it initiates CIBA, polls, and
    receives an OBO token where it (probe-agent-b) is the actor.

Flow:
  1. Read C1 output to extract probe.user's UUID (proves answer #1: `sub`
     carries user identity directly via the orchestrator's forwarded token).
  2. Mint probe-agent-b's actor_token via App-Native Auth (same as c4).
  3. POST /oauth2/ciba — authenticated as probe-agent-b's auto-created Agent
     App (proves answer #2: Agent Apps have CIBA grant; no separate
     confidential client needed). Pass login_hint, actor_token,
     binding_message, scope, notification_channel=external.
  4. Print auth_url. User opens it, clicks Approve, completes consent.
  5. Poll /oauth2/token with grant_type=ciba + auth_req_id (NO actor_token
     on polling — per your edit to the WSO2 doc).
  6. Decode result. Verify sub=probe.user, act.sub=probe-agent-b.

Pass: depth-1 OBO with the right sub/act.
Fail: any error code from /ciba or /token, or claim mismatch.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
import time
import webbrowser
from pathlib import Path

import requests

from _lib import Config, IdpClient, decode_jwt, dim, fail, hr, info, ok, show_jwt, warn


C1_TOKEN_PATH = Path("/tmp/c1_pattern_c_token.txt")
C8_TOKEN_PATH = Path("/tmp/c8_ciba_token.txt")
CIBA_GRANT = "urn:openid:params:grant-type:ciba"
CIBA_ENDPOINT = "/oauth2/ciba"


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
    """3-step App-Native Auth — re-used from c3/c4."""
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
    s = client.session

    if not C1_TOKEN_PATH.exists():
        fail(f"{C1_TOKEN_PATH} not found — run c1_pattern_c.py first")
        return 2

    agent_b_id = cfg.raw["PROBE_AGENT_B_ID"]
    agent_b_secret = cfg.raw["PROBE_AGENT_B_SECRET"]
    agent_b_oauth_id = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_ID"]
    agent_b_oauth_secret = cfg.raw["PROBE_AGENT_B_OAUTH_CLIENT_SECRET"]

    hr("C8 — Per-agent CIBA → depth-1 OBO")
    info("Stand-in mapping: probe-agent-a ≈ orchestrator-agent, probe-agent-b ≈ hr_agent")

    # ─── 1) Extract user UUID from C1 (the orchestrator's forwarded OBO) ─
    hr("C8.1 — Extract user UUID from C1 OBO token (validates answer #1)")
    inbound_token = C1_TOKEN_PATH.read_text().strip()
    inbound = decode_jwt(inbound_token)
    user_sub = inbound.get("sub")
    inbound_act = inbound.get("act", {}).get("sub")
    info(f"  inbound.sub      = {user_sub}    (this is probe.user's UUID)")
    info(f"  inbound.act.sub  = {inbound_act} (= probe-agent-a, simulating orchestrator)")
    if not user_sub:
        fail("C1 token has no sub — re-run c1_pattern_c.py")
        return 1
    ok(f"login_hint will be: {user_sub}")

    # ─── 2) Mint probe-agent-b's actor_token via App-Native Auth ────────
    hr("C8.2 — Mint probe-agent-b actor_token (App-Native Auth)")
    try:
        actor_token = mint_agent_token_via_authn(
            client, cfg, agent_b_id, agent_b_secret, agent_b_oauth_id, agent_b_oauth_secret
        )
    except RuntimeError as e:
        fail(f"Could not mint probe-agent-b actor_token: {e}")
        return 1
    ap = decode_jwt(actor_token)
    ok(f"actor_token minted; sub={ap.get('sub')}, aut={ap.get('aut')}")

    # ─── 3) POST /oauth2/ciba ───────────────────────────────────────────
    hr("C8.3 — POST /oauth2/ciba (authenticated as probe-agent-b's Agent App)")
    ciba_url = cfg.base_url + CIBA_ENDPOINT
    info(f"  endpoint            : {ciba_url}")
    info(f"  Basic auth          : probe-agent-b's Agent App ({agent_b_oauth_id})")
    info(f"  login_hint          : {user_sub}")
    info(f"  actor_token         : (probe-agent-b's I4 token)")
    info(f"  notification_channel: external")
    info(f"  binding_message     : 'probe-agent-b wants to act on your behalf'")

    r_ciba = s.post(
        ciba_url,
        auth=(agent_b_oauth_id, agent_b_oauth_secret),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "scope": "openid",
            "login_hint": user_sub,
            "binding_message": "probe-agent-b wants to act on your behalf",
            "actor_token": actor_token,
            "notification_channel": "external",
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
        if err == "unauthorized_client":
            info("→ CIBA grant not enabled on probe-agent-b's Agent App.")
            info("  Console → Applications → AGENT-941a5f32-... → Protocol → ☑ CIBA")
        elif "notification" in msg.lower():
            info("→ Notification channels not configured on the app. Set Notification Channels=External.")
        elif "login_hint" in msg.lower() or "user" in msg.lower():
            info("→ login_hint rejected. Check that probe.user UUID is correct.")
        return 1

    ok(f"auth_req_id received: {auth_req_id}")
    if auth_url:
        ok(f"auth_url received (External delivery confirmed)")
    else:
        warn("No auth_url in response — External notification channel may not be set on the app")

    # ─── 4) Show auth_url, prompt user to approve ───────────────────────
    hr("C8.4 — User consent step")
    info("")
    info("=" * 70)
    info("  USER ACTION REQUIRED — open this URL in a browser, log in as")
    info(f"  probe.user / {cfg.probe_user_password}, and click Approve:")
    info("")
    info(f"  {auth_url}")
    info("")
    info(f"  (You have ~{expires_in}s. Opening browser; polling starts in 3s...)")
    info("=" * 70)
    try:
        webbrowser.open(auth_url)
    except Exception as e:  # noqa: BLE001
        warn(f"webbrowser.open failed: {e} — copy the URL above manually")
    time.sleep(3)

    # ─── 5) Poll /oauth2/token ──────────────────────────────────────────
    hr("C8.5 — Poll /oauth2/token (no actor_token on poll)")
    deadline = time.time() + min(expires_in, 240)
    poll_count = 0
    exchanged: str | None = None
    while time.time() < deadline:
        poll_count += 1
        r = s.post(
            cfg.token_url,
            auth=(agent_b_oauth_id, agent_b_oauth_secret),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": CIBA_GRANT,
                "auth_req_id": auth_req_id,
            },
        )
        try:
            body = r.json()
        except ValueError:
            fail(f"Non-JSON /token response: {r.text[:500]}")
            return 1

        err = body.get("error")
        if body.get("access_token"):
            exchanged = body["access_token"]
            ok(f"poll #{poll_count}: token issued")
            break
        if err == "authorization_pending":
            dim(f"  poll #{poll_count}: authorization_pending — sleeping {interval}s")
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval += 5
            dim(f"  poll #{poll_count}: slow_down — increasing interval to {interval}s")
            time.sleep(interval)
            continue
        if err == "expired_token":
            fail(f"poll #{poll_count}: auth_req_id expired before user consented")
            return 1
        if err == "access_denied":
            fail(f"poll #{poll_count}: user denied the consent")
            return 1
        # Unexpected error
        fail(f"poll #{poll_count}: unexpected error={err} description={body.get('error_description', '')}")
        return 1

    if not exchanged:
        fail(f"Polling timed out after {poll_count} attempts ({expires_in}s)")
        return 1

    C8_TOKEN_PATH.write_text(exchanged)
    ok(f"Token saved to {C8_TOKEN_PATH}")

    payload = show_jwt(exchanged, "C8.6 — Decoded CIBA-issued token")

    # ─── 6) Validate ────────────────────────────────────────────────────
    hr("C8 verdict")
    sub = payload.get("sub")
    aut = payload.get("aut")
    act = payload.get("act")
    aud = payload.get("aud")
    scope = payload.get("scope")

    info(f"  sub = {sub}")
    info(f"  aut = {aut}")
    info(f"  act = {act}")
    info(f"  aud = {aud}")
    info(f"  scope = {scope}")

    if sub != user_sub:
        fail(f"sub ({sub}) does not equal probe.user ({user_sub})")
        return 1
    ok("sub == probe.user UUID ✓")

    if not act:
        fail("act claim absent — CIBA didn't capture the actor binding")
        return 1
    if not isinstance(act, dict):
        fail(f"act is not a dict: {act}")
        return 1
    if act.get("sub") != agent_b_id:
        fail(f"act.sub ({act.get('sub')}) != probe-agent-b id ({agent_b_id})")
        return 1
    ok(f"act.sub == probe-agent-b ✓")

    if aut == "APPLICATION_USER":
        ok("aut == APPLICATION_USER ✓")
    else:
        warn(f"aut = {aut} (expected APPLICATION_USER)")

    hr("C8 PASS — CIBA produces depth-1 OBO with user-on-agent binding")
    info("")
    info("→ The architecture pivot is validated:")
    info("  - Per-agent CIBA produces sub=user, act.sub=this-agent")
    info("  - User consents per-agent in real time (auth_url delivered to client)")
    info("  - No nested act needed; each specialist gets its own depth-1 OBO")
    info("  - This is what the v3 architecture rebuild is based on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
