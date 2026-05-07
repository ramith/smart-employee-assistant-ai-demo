#!/usr/bin/env python3
"""
C1 — Pattern C: user→agent delegation via `requested_actor` + actor_token.

Validates ingredient I1: the canonical Asgardeo/WSO2-IS pattern where
a confidential client initiates a user authorization flow naming an
agent as the intended actor, then exchanges the code together with
the agent's actor_token to receive an OBO token bearing
`sub=<user>, act.sub=<agent>`.

Approach:
  1. Pre-flight: load probe-agent-a's actor_token from /tmp/c4_agent_a_token.txt
     (mint via c4 if missing).
  2. Build /oauth2/authorize URL with PKCE + requested_actor=<probe-agent-a>.
  3. Spin up a tiny HTTP server on localhost:9999 to catch the redirect.
  4. Open the URL in the user's browser. User authenticates as probe.user.
  5. Server captures the auth code and shuts down.
  6. POST /oauth2/token with code + actor_token → exchanged token.
  7. Verify sub=<probe.user UUID>, act.sub=<probe-agent-a id>.

Pass: act claim present, sub=user, act.sub matches PROBE_AGENT_A_ID.
Fail: any /authorize error, /token rejection, or missing/wrong claims.
Common failure modes are diagnosed inline.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from _lib import Config, IdpClient, dim, fail, hr, info, ok, show_jwt, warn


C4_TOKEN_PATH = Path("/tmp/c4_agent_a_token.txt")
C1_TOKEN_PATH = Path("/tmp/c1_pattern_c_token.txt")
CALLBACK_PORT = 9999
CALLBACK_PATH = "/callback"


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot handler that captures the auth code from the redirect."""

    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(parsed.query))
        _CallbackHandler.captured.update(params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<html><body style='font-family: system-ui; padding: 2rem;'>"
            "<h1>OK — callback received</h1>"
            "<p>You can close this tab and return to the terminal.</p>"
            f"<pre>{params}</pre>"
            "</body></html>"
        ).encode()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:  # silence default access log
        return


def wait_for_callback(timeout_s: int = 300) -> dict[str, str]:
    """Run a one-shot http.server on CALLBACK_PORT until a callback hits."""
    _CallbackHandler.captured = {}
    server = socketserver.TCPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    server.timeout = 1
    info(f"Listening on http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH} for redirect...")

    stop = threading.Event()

    def run() -> None:
        while not stop.is_set() and not _CallbackHandler.captured:
            server.handle_request()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    stop.set()
    server.server_close()
    return _CallbackHandler.captured


def main() -> int:
    cfg = Config.load()
    client = IdpClient(cfg)
    s = client.session

    if not C4_TOKEN_PATH.exists():
        fail(f"{C4_TOKEN_PATH} not found — run c4_app_native_authn.py first to mint actor_token")
        return 2

    actor_token = C4_TOKEN_PATH.read_text().strip()
    agent_a_id = cfg.raw["PROBE_AGENT_A_ID"]
    redirect_uri = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

    hr("C1 — Pattern C (requested_actor + actor_token)")
    info(f"Front-door client : probe-client-a ({cfg.probe_client_a_id})")
    info(f"requested_actor   : probe-agent-a ({agent_a_id})")
    info(f"actor_token       : (loaded from {C4_TOKEN_PATH})")
    info(f"redirect_uri      : {redirect_uri}")

    # ─── Step 1: Build /authorize URL ───────────────────────────────────
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": cfg.probe_client_a_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "requested_actor": agent_a_id,
    }
    auth_url = f"{cfg.authz_url}?{urllib.parse.urlencode(params)}"

    hr("C1.1 — Open /authorize in browser, authenticate as probe.user")
    info(f"URL: {auth_url}")
    info("")
    info(f"Login as: {cfg.probe_user_username} / {cfg.probe_user_password}")
    info("(If WSO2 IS shows a consent screen, approve it.)")
    info("")
    info("Browser is being opened... (also paste the URL manually if it didn't)")
    try:
        webbrowser.open(auth_url)
    except Exception as e:  # noqa: BLE001
        warn(f"webbrowser.open failed: {e}")

    captured = wait_for_callback(timeout_s=300)
    if not captured:
        fail("Timed out waiting for redirect callback after 300s")
        return 1
    if captured.get("error"):
        fail(f"/authorize returned error in callback: {captured}")
        info("")
        err = captured.get("error", "")
        if "actor" in err.lower() or "actor" in captured.get("error_description", "").lower():
            info("→ requested_actor rejected. Possible causes:")
            info("  - probe-agent-a not registered as an authorized actor on probe-client-a")
            info("    (we suspected this UI step might be needed)")
            info("  - The agent's OAuth App needs Token Exchange grant enabled")
            info("  - Try removing requested_actor and see if vanilla auth-code works")
        return 1
    if captured.get("state") != state:
        fail(f"state mismatch: sent {state}, got {captured.get('state')}")
        return 1
    code = captured.get("code")
    if not code:
        fail(f"No `code` in callback params: {captured}")
        return 1
    ok(f"Auth code captured: {code[:20]}...")

    # ─── Step 2: /oauth2/token with code + actor_token ──────────────────
    hr("C1.2 — POST /oauth2/token (auth code + actor_token in BODY)")
    # Pattern C empirical learning from Asgardeo P10.B: actor_token MUST
    # go in the request body (NOT Authorization header). Repeat here.
    r = s.post(
        cfg.token_url,
        auth=(cfg.probe_client_a_id, cfg.probe_client_a_secret),
        headers={"Accept": "application/json"},
        data={
            "grant_type": "authorization_code",
            "client_id": cfg.probe_client_a_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "actor_token": actor_token,
            "actor_token_type": "urn:ietf:params:oauth:token-type:access_token",
        },
    )
    dim(f"  ← HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        fail(f"Non-JSON response: {r.text[:500]}")
        return 1

    import json as _json
    for line in _json.dumps(body, indent=2).splitlines()[:40]:
        dim(f"    {line}")

    token = body.get("access_token")
    if not token:
        fail(f"No access_token: error={body.get('error')}")
        info("")
        err = body.get("error", "")
        if err == "invalid_request" and "actor" in body.get("error_description", "").lower():
            info("→ actor_token rejected. Possible causes:")
            info("  - actor_token expired (re-run c4 to mint fresh)")
            info("  - actor_token not from a permitted agent (allowlist needed)")
            info("  - actor_token signed by an issuer WSO2 IS doesn't trust here")
        return 1
    ok(f"Got delegated token (first 40): {token[:40]}...")
    C1_TOKEN_PATH.write_text(token)

    payload = show_jwt(token, "C1.3 — Decoded delegated token")

    # ─── Step 3: Validate ───────────────────────────────────────────────
    hr("C1 verdict")
    sub = payload.get("sub")
    aut = payload.get("aut")
    act = payload.get("act")

    info(f"sub  = {sub}")
    info(f"aut  = {aut}")
    info(f"act  = {act}")

    if not act:
        fail("`act` claim absent — Pattern C didn't produce delegation marker")
        info("→ WSO2 IS may not support requested_actor on this build, or the")
        info("  parameter is named differently. The token is a plain user token.")
        return 1

    act_sub = act.get("sub") if isinstance(act, dict) else None
    if act_sub != agent_a_id:
        warn(f"act.sub ({act_sub}) does not equal probe-agent-a id ({agent_a_id})")
        info("  Token has act, but actor identity unexpected. Inspect above.")
        return 1

    ok(f"act.sub == probe-agent-a ✓")
    if aut == "APPLICATION_USER":
        ok("aut == APPLICATION_USER ✓ (matches Pattern C output shape)")

    hr("C1 PASS — Pattern C produces depth-1 act on this WSO2 IS install")
    info(f"→ Ingredient I1 confirmed.")
    info(f"→ Token saved to {C1_TOKEN_PATH} for c3 (RFC 8693 chaining → depth-2).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
