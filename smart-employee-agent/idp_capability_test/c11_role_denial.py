#!/usr/bin/env python3
"""
C11 — Role-based scope denial path: initiation-time vs. consent-time.

Question this probe answers (UC-08 / D2.9 prerequisite):

  When an Employee-role user attempts to initiate a CIBA flow that requests
  a scope only granted to the HR Admin role (e.g. ``it_assets_write_rest``),
  does WSO2 IS 7.2.0 reject:

  Path A — at /oauth2/ciba initiation
           HTTP 4xx with ``error=invalid_scope`` (or similar). No auth_req_id
           returned. No consent screen shown. No token can possibly be issued.
           Best for the security narrative: "the IdP refuses to even try."

  Path B — at the consent screen
           /oauth2/ciba returns 200 with auth_req_id + auth_url. The user is
           sent to the consent page; IS shows a denial / "you don't have
           permission" page. Polling /oauth2/token returns access_denied.

The outcome decides:
  - Which error code the agent dispatcher classifies (ERR-CIBA-003 init vs.
    ERR-CIBA-005 consent).
  - Which copy-deck branch the orchestrator uses (§7.17 vs §5.16).
  - The mock-IS branch in the N30 / N31 test fixtures.

Approach: read the demo ``it_agent/.env`` for credentials, run a real
``/oauth2/ciba`` POST with ``login_hint=<employee_user_sub>``, ``actor_token=
<it_agent's I4 token>``, ``scope=openid it_assets_write_rest``. Inspect the
HTTP status + body. Print the outcome.

Prereq:
  - Sprint 2A.3 IS pre-flight complete: ``it_assets_write_rest`` registered;
    HR Admin role granted it; Employee role NOT granted it; IT Agent App
    has ``it_assets_write_rest`` in Allowed Scopes.
  - ``EMPLOYEE_USER_SUB`` env var (or .env entry) — the UUID of employee_user.
    Pull from orchestrator logs after a successful login (look for
    ``user_sub=<uuid>``).

Run::

  cd idp_capability_test
  EMPLOYEE_USER_SUB=2048ad8c-... python3 c11_role_denial.py

Outputs the verdict: ``PATH_A_INIT_DENIAL`` or ``PATH_B_CONSENT_DENIAL``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
import urllib3

# Allow self-signed cert on the IS VM
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Helpers (mirroring _lib.py without the substrate dependency) ──────────────

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
    """Minimal .env loader (no quotes, no interpolation). Mirror Sprint 1's pattern."""
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


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    it_env = _load_env_file(repo_root / "it_agent" / ".env")

    is_base = it_env.get("WSO2_IS_BASE_URL", "").rstrip("/")
    oauth_client_id = it_env.get("IT_AGENT_OAUTH_CLIENT_ID", "")
    oauth_client_secret = it_env.get("IT_AGENT_OAUTH_CLIENT_SECRET", "")
    agent_id = it_env.get("IT_AGENT_ID", "")
    agent_secret = it_env.get("IT_AGENT_SECRET", "")

    if not all([is_base, oauth_client_id, oauth_client_secret, agent_id, agent_secret]):
        fail("Missing required values in it_agent/.env (need WSO2_IS_BASE_URL + IT_AGENT_*).")
        return 2

    employee_sub = os.environ.get("EMPLOYEE_USER_SUB", "").strip()
    if not employee_sub:
        fail("EMPLOYEE_USER_SUB env var not set.")
        info("Sign in to the demo as employee_user, then check orchestrator logs for")
        info("a line like 'user_sub=<uuid>' and pass it as EMPLOYEE_USER_SUB.")
        return 2

    hr_("C11 — Role-based scope denial path probe")
    info(f"  IS base URL          : {is_base}")
    info(f"  IT Agent OAuth client: {oauth_client_id}")
    info(f"  Login hint (user)    : {employee_sub}")
    info(f"  Requested scope      : it_assets_write_rest (Employee should NOT have this)")

    s = requests.Session()
    s.verify = False  # noqa: S501 — self-signed dev cert

    # ── Step 1: mint IT-agent's I4 actor token via /oauth2/authn ──────────────
    hr_("C11.1 — Mint IT Agent actor_token via App-Native Auth")
    actor_token = _mint_agent_actor_token(
        s, is_base, oauth_client_id, oauth_client_secret, agent_id, agent_secret
    )
    if not actor_token:
        fail("Could not mint actor_token — aborting probe.")
        return 1
    ok(f"actor_token minted (len={len(actor_token)})")

    # ── Step 2: /oauth2/ciba with the write-scope as employee_user ────────────
    hr_("C11.2 — POST /oauth2/ciba scope=it_assets_write_rest login_hint=<employee>")
    ciba_url = f"{is_base}/oauth2/ciba"
    r = s.post(
        ciba_url,
        auth=(oauth_client_id, oauth_client_secret),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "scope": "openid it_assets_write_rest",
            "login_hint": employee_sub,
            "binding_message": "C11 role-denial probe",
            "actor_token": actor_token,
            "notification_channel": "external",
        },
    )

    info(f"  HTTP status : {r.status_code}")
    try:
        body = r.json()
        import json as _json
        for line in _json.dumps(body, indent=2).splitlines():
            info(f"    {line}")
    except ValueError:
        info(f"  body (non-JSON): {r.text[:300]}")
        body = {}

    # ── Verdict ───────────────────────────────────────────────────────────────
    hr_("C11 verdict")
    if r.status_code == 200 and body.get("auth_req_id"):
        warn("Path B — IS accepted CIBA initiation; denial would be at consent screen.")
        info("  Implication: agent must POLL /oauth2/token to discover the denial.")
        info("  Expected polling response: {\"error\": \"access_denied\"}.")
        info("  Map this to ERR-CIBA-005 in the dispatcher; copy-deck §5.16 (DENIED widget).")
        info("")
        info("  → PATH_B_CONSENT_DENIAL")
        return 0

    if r.status_code in (400, 401, 403):
        err = body.get("error", "")
        info(f"  error       : {err}")
        info(f"  description : {body.get('error_description', '')}")
        if "scope" in err.lower():
            ok("Path A — IS rejected at initiation. No auth_req_id, no consent prompt.")
            info("  Implication: agent surfaces denial WITHOUT showing a consent widget.")
            info("  Map this to ERR-CIBA-003 in the dispatcher; copy-deck §7.17 (no-permission).")
            info("")
            info("  → PATH_A_INIT_DENIAL")
            return 0
        warn(f"Path A-ish — IS rejected but with non-scope error: {err}")
        info("  → PATH_A_INIT_DENIAL_OTHER")
        return 0

    fail(f"Unexpected response shape (HTTP {r.status_code}). Investigate before proceeding.")
    info("  → UNEXPECTED")
    return 1


def _mint_agent_actor_token(
    s: requests.Session,
    is_base: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    agent_id: str,
    agent_secret: str,
) -> str | None:
    """Three-step App-Native Auth: /authorize → /authn → /token. Return access_token or None."""
    import base64
    import hashlib
    import secrets

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    redirect_uri = "http://localhost:9999/agent-callback"

    # /oauth2/authorize (POST, response_mode=direct, App-Native flow)
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

    # /oauth2/authn — supply agent identity
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

    # /oauth2/token — exchange code
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


if __name__ == "__main__":
    sys.exit(main())
