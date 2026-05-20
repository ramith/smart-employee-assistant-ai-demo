#!/usr/bin/env python3
"""
C12 — Logout capability probe (Sprint 3 design spike).

Questions this probe answers (in order of decision-impact for Sprint 3):

  Q1 — Does an OBO token issued via the CIBA grant carry an ``sid``
       (session ID) claim?
       OIDC Back-Channel Logout 1.0 §2.4 says the logout_token sent to the
       RP contains EITHER ``sub`` OR ``sid`` (or both) so the RP can decide
       which session(s) to invalidate. If our CIBA-issued tokens lack
       ``sid``, IS can only target by ``sub`` — meaning a BCL would
       invalidate *every* OBO token the agent ever minted for that user,
       not just the one tied to the user's current orchestrator session.

  Q2 — Does the access_token from CIBA carry ``aud`` matching the agent
       app's OAuth client_id (per F-17), so introspection / denylist
       lookups can route correctly?
       Already known YES from C8 / Sprint 1 sign-off, but reconfirm here
       on the same token shape used by Sprint 3.

  Q3 — Does the id_token (when CIBA includes ``openid`` scope) carry a
       claim that ties the token to the user's OP-side session, so a
       later ``end_session_endpoint`` call from the orchestrator can
       cascade to the agent?

  Q4 — Does ``end_session_endpoint`` exist and accept the orchestrator's
       id_token_hint? (Smoke-tests RP-initiated logout for the
       user-facing client.)

What this probe does NOT do (manual recipe at the bottom):

  * Verify that IS *actually fires* a back-channel logout POST to a
    registered ``backchannel_logout_uri`` for an agent app whose tokens
    were issued via CIBA. That requires an HTTP listener and IS Console
    setup; documented as a manual recipe so we can decide whether to
    automate it in Sprint 3 Stage 1.

Run::

    cd idp_capability_test
    python3 c12_logout_capability.py

Prereq:
  - hr_agent/.env populated (used as the agent under test).
  - A real CIBA flow for ``probe.user`` recently completed (the probe
    re-runs the C8 substrate to mint a fresh OBO token; **requires the
    user to click Approve on the IS consent page** during the run).
  - Sub of probe.user is captured automatically from the C8 output if
    available, or pass via PROBE_USER_SUB env var.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Tiny output helpers (mirroring c11_role_denial.py) ────────────────────────

def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def hr_(title: str) -> None:
    print(_bold(f"\n──── {title} ────"))


def info(s: str) -> None:
    print(f"  {s}")


def ok(s: str) -> None:
    print(f"  \033[32m✓\033[0m {s}")


def warn(s: str) -> None:
    print(f"  \033[33m⚠\033[0m {s}")


def fail(s: str) -> None:
    print(f"  \033[31m✗\033[0m {s}")


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'").strip('"')
    return env


def _decode_jwt_unverified(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (header, payload) without signature verification — inspection only."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}, {}
    def _b64url(seg: str) -> dict[str, Any]:
        seg += "=" * (-len(seg) % 4)
        try:
            return json.loads(base64.urlsafe_b64decode(seg).decode())
        except Exception:
            return {}
    return _b64url(parts[0]), _b64url(parts[1])


# ── Main probe ────────────────────────────────────────────────────────────────

def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    hr_env = _load_env_file(repo_root / "hr_agent" / ".env")

    is_base = hr_env.get("WSO2_IS_BASE_URL", "").rstrip("/")
    oauth_client_id = hr_env.get("HR_AGENT_OAUTH_CLIENT_ID", "")
    oauth_client_secret = hr_env.get("HR_AGENT_OAUTH_CLIENT_SECRET", "")
    agent_id = hr_env.get("HR_AGENT_ID", "")
    agent_secret = hr_env.get("HR_AGENT_SECRET", "")
    user_sub = os.environ.get("PROBE_USER_SUB", "").strip()

    if not all([is_base, oauth_client_id, oauth_client_secret, agent_id, agent_secret]):
        fail("Missing required values in hr_agent/.env (WSO2_IS_BASE_URL + HR_AGENT_*).")
        return 2
    if not user_sub:
        fail("PROBE_USER_SUB env var not set.")
        info("Sign in to the demo as employee_user, then check orchestrator logs for")
        info("a 'user_sub=<uuid>' line and pass it as PROBE_USER_SUB.")
        return 2

    hr_("C12 — Logout capability probe")
    info(f"  IS base URL          : {is_base}")
    info(f"  HR Agent OAuth client: {oauth_client_id}")
    info(f"  Probe user sub       : {user_sub}")

    s = requests.Session()
    s.verify = False  # noqa: S501 — self-signed dev cert

    # ── Q4: end_session_endpoint discovery ────────────────────────────────────
    hr_("C12.0 — Discover end_session_endpoint via OIDC discovery")
    discovery_url = f"{is_base}/oauth2/oidcdiscovery/.well-known/openid-configuration"
    r = s.get(discovery_url)
    if r.status_code != 200:
        warn(f"OIDC discovery returned {r.status_code} — trying fixed path /oidc/logout")
        end_session_endpoint = f"{is_base}/oidc/logout"
        check_session_iframe = None
    else:
        meta = r.json()
        end_session_endpoint = meta.get("end_session_endpoint")
        check_session_iframe = meta.get("check_session_iframe")
        backchannel_logout_supported = meta.get("backchannel_logout_supported")
        backchannel_session_supported = meta.get("backchannel_session_supported")
        frontchannel_logout_supported = meta.get("frontchannel_logout_supported")
        frontchannel_session_supported = meta.get("frontchannel_session_supported")
        info(f"  end_session_endpoint            : {end_session_endpoint}")
        info(f"  check_session_iframe            : {check_session_iframe}")
        info(f"  backchannel_logout_supported    : {backchannel_logout_supported}")
        info(f"  backchannel_session_supported   : {backchannel_session_supported}")
        info(f"  frontchannel_logout_supported   : {frontchannel_logout_supported}")
        info(f"  frontchannel_session_supported  : {frontchannel_session_supported}")
        if backchannel_logout_supported:
            ok("IS advertises OIDC Back-Channel Logout 1.0 support")
        else:
            warn("IS discovery does NOT advertise backchannel_logout_supported")

    # ── Mint fresh actor_token, run CIBA, inspect tokens ──────────────────────
    hr_("C12.1 — Mint HR Agent actor_token via App-Native Auth")
    actor_token = _mint_agent_actor_token(
        s, is_base, oauth_client_id, oauth_client_secret, agent_id, agent_secret
    )
    if not actor_token:
        fail("Could not mint actor_token — aborting.")
        return 1
    ok(f"actor_token minted (len={len(actor_token)})")

    hr_("C12.2 — POST /oauth2/ciba (probe.user, scope=openid hr_self_rest)")
    info("  → IS will issue an auth_url. The probe user must click Approve.")
    info("  → This run requires manual interaction at the IS consent screen.")
    r = s.post(
        f"{is_base}/oauth2/ciba",
        auth=(oauth_client_id, oauth_client_secret),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "scope": "openid hr_self_rest",
            "login_hint": user_sub,
            "binding_message": "C12 logout capability probe",
            "actor_token": actor_token,
            "notification_channel": "external",
        },
    )
    if r.status_code != 200:
        fail(f"/oauth2/ciba: HTTP {r.status_code} {r.text[:300]}")
        return 1
    body = r.json()
    auth_req_id = body.get("auth_req_id")
    auth_url = body.get("auth_url")
    interval = int(body.get("interval", 2))
    expires_in = int(body.get("expires_in", 300))
    ok(f"CIBA initiated  auth_req_id={auth_req_id}")
    info(f"  Open this URL in a browser and click Approve as probe.user:")
    info(f"    {auth_url}")
    info(f"  Polling /oauth2/token every {interval}s (timeout {expires_in}s)…")

    # Poll for the OBO token
    deadline = time.time() + expires_in
    obo_token: dict[str, Any] | None = None
    while time.time() < deadline:
        time.sleep(interval)
        rt = s.post(
            f"{is_base}/oauth2/token",
            auth=(oauth_client_id, oauth_client_secret),
            data={
                "grant_type": "urn:openid:params:grant-type:ciba",
                "auth_req_id": auth_req_id,
            },
        )
        if rt.status_code == 200:
            obo_token = rt.json()
            break
        body = rt.json() if rt.headers.get("content-type", "").startswith("application/json") else {}
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        fail(f"poll error: {err}  body={body}")
        return 1

    if obo_token is None:
        fail("CIBA polling timed out without a token.")
        return 1
    ok("OBO token issued")

    # ── Q1, Q2, Q3: inspect access_token + id_token claims ────────────────────
    hr_("C12.3 — Inspect access_token claims (Q1: sid?  Q2: aud?)")
    at_header, at_payload = _decode_jwt_unverified(obo_token.get("access_token", ""))
    info(f"  access_token header.alg : {at_header.get('alg')}  kid={at_header.get('kid')}")
    for key in ("iss", "aud", "sub", "act", "scope", "exp", "iat", "jti", "sid", "azp", "client_id"):
        if key in at_payload:
            info(f"  access_token.{key}  : {json.dumps(at_payload[key], default=str)}")

    has_sid_at = "sid" in at_payload
    if has_sid_at:
        ok("access_token carries `sid` claim (BCL can target this exact session)")
    else:
        warn("access_token does NOT carry `sid` (BCL would have to target by `sub` only)")

    hr_("C12.4 — Inspect id_token claims (Q3: session linkage?)")
    id_token = obo_token.get("id_token")
    if not id_token:
        warn("No id_token in CIBA response (scope did not include openid?)")
    else:
        it_header, it_payload = _decode_jwt_unverified(id_token)
        info(f"  id_token header.alg : {it_header.get('alg')}  kid={it_header.get('kid')}")
        for key in ("iss", "aud", "sub", "act", "exp", "iat", "auth_time", "sid", "nonce", "azp"):
            if key in it_payload:
                info(f"  id_token.{key}  : {json.dumps(it_payload[key], default=str)}")

        has_sid_id = "sid" in it_payload
        if has_sid_id:
            ok("id_token carries `sid` — BCL can target this token's session specifically")
        else:
            warn("id_token does NOT carry `sid` — BCL must target by `sub` only")

    # ── Verdict ───────────────────────────────────────────────────────────────
    hr_("C12 verdict (preliminary — see manual recipe below for the actual BCL fire test)")
    info("Decision matrix for Sprint 3 Stage 1:")
    if has_sid_at:
        info("  • CIBA tokens carry `sid` → IS could fire BCL with sid → RP can target")
        info("    a single mint precisely. Best case for per-token revocation.")
    else:
        info("  • CIBA tokens lack `sid` → BCL fan-out from IS is only by `sub`.")
        info("    Implication: 'logout user X' would invalidate ALL of agent's OBO tokens")
        info("    for that user across ALL their orchestrator sessions. Acceptable for the")
        info("    POC (single-session) but worth noting for multi-session future.")
    info("")
    info("Cross-check before locking the design:")
    info("  1. Run the manual recipe below to confirm IS *actually* POSTs a logout_token")
    info("     to the agent's backchannel_logout_uri when the orchestrator hits")
    info("     /oidc/logout with id_token_hint.")
    info("  2. Decide: do agent apps register their own BCL URLs (defense-in-depth) or")
    info("     does the orchestrator alone fan-out cache-bust signals (S3.2)?")
    return 0


def _mint_agent_actor_token(
    s: requests.Session,
    is_base: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    agent_id: str,
    agent_secret: str,
) -> str | None:
    """Three-step App-Native Auth: /authorize → /authn → /token. Mirrors c11."""
    import hashlib
    import secrets

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    redirect_uri = "http://localhost:9999/agent-callback"

    r1 = s.post(
        f"{is_base}/oauth2/authorize",
        data={
            "client_id": oauth_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid internal_login",
            "state": secrets.token_urlsafe(8),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_mode": "direct",
        },
    )
    if r1.status_code != 200:
        fail(f"/oauth2/authorize: HTTP {r1.status_code} {r1.text[:200]}")
        return None
    flow_id = r1.json().get("flowId")
    if not flow_id:
        fail(f"/oauth2/authorize: missing flowId in {r1.text[:200]}")
        return None

    r2 = s.post(
        f"{is_base}/oauth2/authn",
        json={
            "flowId": flow_id,
            "selectedAuthenticator": {
                "authenticatorId": "QmFzaWNBdXRoZW50aWNhdG9yOkxPQ0FM",  # BasicAuthenticator:LOCAL
                "params": {"username": agent_id, "password": agent_secret},
            },
        },
    )
    if r2.status_code != 200:
        fail(f"/oauth2/authn: HTTP {r2.status_code} {r2.text[:200]}")
        return None
    code = r2.json().get("authData", {}).get("code")
    if not code:
        fail(f"/oauth2/authn: no code in {r2.text[:200]}")
        return None

    r3 = s.post(
        f"{is_base}/oauth2/token",
        auth=(oauth_client_id, oauth_client_secret),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    if r3.status_code != 200:
        fail(f"/oauth2/token: HTTP {r3.status_code} {r3.text[:200]}")
        return None
    return r3.json().get("access_token")


# ─────────────────────────────────────────────────────────────────────────────
# Manual recipe for verifying IS *actually* fires back-channel logout for an
# agent app. This is the part the probe cannot self-execute — it requires
# Console clicks and a localhost listener.
# ─────────────────────────────────────────────────────────────────────────────

MANUAL_RECIPE = """
Manual recipe — verify IS *actually fires* BCL for an agent app
───────────────────────────────────────────────────────────────

WSO2 IS runs on an AWS VM; your demo runs on the laptop. We bridge with a
**reverse SSH tunnel** from the laptop to the IS VM, set up by a docker-
compose profile + autossh. Full first-time-setup walkthrough lives in:

   docs/spikes/c12-bcl-spike-setup.md

Quick steps (assumes prep-mac.sh already run):

1. Bring up the capture rig:
     ./scripts/spike-bcl-up.sh

   It will:
     - start bcl-listener (docker container bound to 127.0.0.1:8123)
     - spawn autossh -R 8123:127.0.0.1:8123 to the AWS VM
     - smoke-test by curl-ing http://127.0.0.1:8123/healthz from the VM

2. In WSO2 IS Console → Applications → IT-AGENT-9900… (or HR-AGENT-…) →
   Protocol tab → Logout URLs:
     Back channel logout URL: http://localhost:8123/bcl
     (Front channel logout URL: leave blank for this test.)
   Click Update. Repeat for the orchestrator app for a control comparison.

3. Sign into the orchestrator demo as employee_user. Issue ONE chat that
   triggers the agent's CIBA flow (so a token has been minted for this
   user against the specific agent app).

4. Trigger logout — try BOTH paths so you can compare:

   a. RP-initiated:
        Open in the browser:
        https://13.60.190.47:9443/oidc/logout?id_token_hint=<orch-id-token>
                                            &post_logout_redirect_uri=http://localhost:8090/
                                            &client_id=<orch-client-id>

   b. Admin terminate:
        Console → User Management → probe.user → Active Sessions → Terminate.

Capture
───────
   docker compose --profile spike-bcl logs -f bcl-listener
   cat tools/_bcl_log/bcl_received.log

The listener pretty-prints each captured POST as JSON with the decoded
logout_token header + payload. Verify in the payload:
   - aud == agent's OAuth client_id (or orchestrator's, depending on which
     URL fired)
   - events.{"http://schemas.openid.net/event/backchannel-logout"} present
   - sub OR sid present
   - nonce ABSENT
   - typ header: "logout+jwt" (recommended by spec; nice-to-have)

Tear down
─────────
   ./scripts/spike-bcl-down.sh

Verdict matrix
──────────────
   * Listener received POST for the orchestrator URL  → expected (orch is a
     conventional OIDC RP).
   * Listener received POST for the agent URL too     → IS DOES fan-out BCL
     to agent apps with CIBA-issued tokens. Option C's defense-in-depth
     layer is viable; we'll wire BCL receivers in agents during Sprint 3B.
   * Listener received POST for orchestrator only     → IS treats agent apps
     as "machine clients without sessions" for BCL purposes. Option C
     degrades to Option A (orchestrator-driven cache-bust only). Document
     and move on.
   * Neither path fires anything                      → check IS Console
     URL save; check IS audit log for OIDC logout events; consider
     federated-IDP-initiated logout config (separate doc).
"""


if __name__ == "__main__":
    rc = main()
    print(MANUAL_RECIPE)
    sys.exit(rc)
