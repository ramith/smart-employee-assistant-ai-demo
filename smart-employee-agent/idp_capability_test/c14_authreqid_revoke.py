#!/usr/bin/env python3
"""
C14 — Does WSO2 IS support revoking a pending CIBA `auth_req_id` before the
user has approved at the consent screen?

This probe answers Q-LOGOUT-4 (the "ghost approval" caveat) referenced in
[`docs/spikes/sprint-3-logout-design-brainstorm.md`](../docs/spikes/sprint-3-logout-design-brainstorm.md)
and locked into Sprint 3 §3 R-2 mitigation.

The race we're trying to close:

    T=0    user clicks Sign Out in the SPA.
    T=0.1  orchestrator starts the cascade. CIBA poll loop is cancelled
           locally via `cancel_event.set()`.
    T=1.5  orchestrator fan-out completes. Session is gone.
    T=2.0  user (out-of-band, on a different device) approves at IS.
    T=2.1  IS happily completes the CIBA grant, mints token-B with a stale
           auth_req_id. The token has no consumer (poll loop is dead) but
           the IS audit row says "successful CIBA after logout".

The mitigation is to call IS at logout time and tell it to invalidate any
pending `auth_req_id` for this user. Whether IS supports this is the
question.

WSO2 IS does not advertise an `auth_req_id` revoke endpoint in the OIDC
discovery document, but `/oauth2/revoke` may accept `token_type_hint=
auth_req_id` (or similar) per the OAuth 2.0 Token Revocation RFC 7009 +
CIBA spec. We probe both shapes.

Verdict matrix:

  /oauth2/revoke with token=<auth_req_id> returns 200/204
    AND polling /oauth2/token returns expired/access_denied/invalid_grant
                                          → F-20 PASS — wire revoke into
                                            UC-09 cancel path (3B.2).

  Endpoint returns 4xx OR polling continues to issue a token after approval
                                          → F-20 FAIL — accept ghost-approval
                                            caveat. Denylist still defends
                                            the resource server side once
                                            the token reaches an MCP call.

────────────────────────────────────────────────────────────────────────────
Operator workflow:

  1. Bring up the demo stack: `make demo-up`.

  2. Set EMPLOYEE_USER_SUB (sub of any test user). Pull from orchestrator
     logs after a successful login.

  3. Run:

       cd idp_capability_test
       EMPLOYEE_USER_SUB=<uuid> python3 c14_authreqid_revoke.py

     The probe will:
       a. Mint HR-AGENT's I4 actor_token via App-Native Auth.
       b. POST /oauth2/ciba with login_hint=<sub> — capturing auth_req_id.
          (The user is NOT prompted to approve — we leave it pending.)
       c. Try to revoke the auth_req_id via several endpoint shapes.
       d. Try to poll /oauth2/token for the auth_req_id and observe the
          response.

  Outputs the verdict: PASS or FAIL.

────────────────────────────────────────────────────────────────────────────

Document the verdict in `docs/architecture/sprint-1-fixes.md` as F-20.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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


def _mint_agent_actor_token(
    s: requests.Session,
    is_base: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    agent_id: str,
    agent_secret: str,
) -> str | None:
    """Three-step App-Native Auth: /authorize → /authn → /token. Mirrors c11_role_denial.py."""
    import base64
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
                "authenticatorId": "QmFzaWNBdXRoZW50aWNhdG9yOkxPQ0FM",
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


def _try_revoke_shape(
    s: requests.Session,
    is_base: str,
    client_id: str,
    client_secret: str,
    auth_req_id: str,
    extra_form: dict[str, str] | None = None,
    label: str = "/oauth2/revoke",
) -> tuple[int, str]:
    data = {"token": auth_req_id}
    if extra_form:
        data.update(extra_form)
    r = s.post(
        f"{is_base}/oauth2/revoke",
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=data,
    )
    info(f"  {label}: HTTP {r.status_code} body={r.text[:200] or '(empty)'}")
    return r.status_code, r.text[:200]


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    hr_env = _load_env_file(repo_root / "hr_agent" / ".env")

    is_base = hr_env.get("WSO2_IS_BASE_URL", "").rstrip("/")
    oauth_client_id = hr_env.get("HR_AGENT_OAUTH_CLIENT_ID", "")
    oauth_client_secret = hr_env.get("HR_AGENT_OAUTH_CLIENT_SECRET", "")
    agent_id = hr_env.get("HR_AGENT_ID", "")
    agent_secret = hr_env.get("HR_AGENT_SECRET", "")

    missing = [k for k, v in {
        "WSO2_IS_BASE_URL": is_base,
        "HR_AGENT_OAUTH_CLIENT_ID": oauth_client_id,
        "HR_AGENT_OAUTH_CLIENT_SECRET": oauth_client_secret,
        "HR_AGENT_ID": agent_id,
        "HR_AGENT_SECRET": agent_secret,
    }.items() if not v]
    if missing:
        fail(f"hr_agent/.env missing: {', '.join(missing)}")
        return 2

    employee_sub = os.environ.get("EMPLOYEE_USER_SUB", "").strip()
    if not employee_sub:
        fail("EMPLOYEE_USER_SUB env var not set.")
        info("Sign in as employee_user (or any test user) and check orchestrator")
        info("logs for the user_sub UUID.")
        return 2

    s = requests.Session()
    s.verify = False  # noqa: S501

    hr_("C14 — auth_req_id revocation capability probe")
    info(f"  IS base URL          : {is_base}")
    info(f"  HR Agent OAuth client: {oauth_client_id}")
    info(f"  login_hint (user)    : {employee_sub}")

    # ── C14.1 mint actor_token ────────────────────────────────────────────────
    hr_("C14.1 — Mint HR Agent actor_token via App-Native Auth")
    actor_token = _mint_agent_actor_token(
        s, is_base, oauth_client_id, oauth_client_secret, agent_id, agent_secret
    )
    if not actor_token:
        fail("Could not mint actor_token — aborting probe.")
        return 1
    ok(f"actor_token minted (len={len(actor_token)})")

    # ── C14.2 initiate CIBA ───────────────────────────────────────────────────
    hr_("C14.2 — POST /oauth2/ciba scope=openid hr_basic_rest (do NOT approve)")
    r = s.post(
        f"{is_base}/oauth2/ciba",
        auth=(oauth_client_id, oauth_client_secret),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "scope": "openid hr_basic_rest",
            "login_hint": employee_sub,
            "binding_message": "C14 auth_req_id revoke probe — do NOT approve",
            "actor_token": actor_token,
            "notification_channel": "external",
        },
    )
    info(f"  HTTP status: {r.status_code}")
    if r.status_code != 200:
        fail(f"/oauth2/ciba did not return 200: {r.text[:200]}")
        return 1
    body = r.json()
    auth_req_id = body.get("auth_req_id")
    if not auth_req_id:
        fail(f"No auth_req_id in CIBA response: {body}")
        return 1
    ok(f"auth_req_id obtained (len={len(auth_req_id)})")
    info(f"  auth_req_id : {auth_req_id[:24]}…")
    info(f"  expires_in  : {body.get('expires_in', '?')}")
    info(f"  interval    : {body.get('interval', '?')}")

    # ── C14.3 try to revoke the auth_req_id (multiple shapes) ────────────────
    hr_("C14.3 — Try /oauth2/revoke with auth_req_id (probe four shapes)")
    info("  Shape A — token=<auth_req_id> (no token_type_hint)")
    code_a, _ = _try_revoke_shape(s, is_base, oauth_client_id, oauth_client_secret, auth_req_id)
    info("  Shape B — token=<auth_req_id> + token_type_hint=auth_req_id")
    code_b, _ = _try_revoke_shape(
        s, is_base, oauth_client_id, oauth_client_secret, auth_req_id,
        extra_form={"token_type_hint": "auth_req_id"},
    )
    info("  Shape C — token=<auth_req_id> + token_type_hint=ciba_request")
    code_c, _ = _try_revoke_shape(
        s, is_base, oauth_client_id, oauth_client_secret, auth_req_id,
        extra_form={"token_type_hint": "ciba_request"},
    )
    info("  Shape D — POST /oauth2/ciba/revoke (non-standard, some IdPs use this)")
    rD = s.post(
        f"{is_base}/oauth2/ciba/revoke",
        auth=(oauth_client_id, oauth_client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"auth_req_id": auth_req_id},
    )
    code_d = rD.status_code
    info(f"  /oauth2/ciba/revoke: HTTP {code_d} body={rD.text[:200] or '(empty)'}")

    accepted = [
        ("A no-hint", code_a),
        ("B token_type_hint=auth_req_id", code_b),
        ("C token_type_hint=ciba_request", code_c),
        ("D /oauth2/ciba/revoke", code_d),
    ]
    succeeded = [name for name, code in accepted if code in (200, 204)]
    if succeeded:
        ok(f"At least one revoke shape returned 2xx: {', '.join(succeeded)}")
    else:
        warn("None of the revoke shapes returned 2xx.")

    # ── C14.4 poll the token endpoint and observe ─────────────────────────────
    hr_("C14.4 — Poll /oauth2/token grant_type=urn:openid:params:grant-type:ciba")
    info("  We expect either: invalid_grant / expired_token / access_denied → revoke worked.")
    info("  Or: authorization_pending → revoke did NOT cancel the auth_req_id.")
    rT = s.post(
        f"{is_base}/oauth2/token",
        auth=(oauth_client_id, oauth_client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "urn:openid:params:grant-type:ciba",
            "auth_req_id": auth_req_id,
        },
    )
    info(f"  HTTP status : {rT.status_code}")
    try:
        token_body = rT.json()
        import json as _json
        for line in _json.dumps(token_body, indent=2).splitlines():
            info(f"    {line}")
    except ValueError:
        info(f"  body (non-JSON): {rT.text[:300]}")
        token_body = {}
    error = token_body.get("error", "")

    # ── Verdict ───────────────────────────────────────────────────────────────
    hr_("C14 verdict")
    revoke_accepted = bool(succeeded)
    cancellation_observed = error in {
        "invalid_grant",
        "expired_token",
        "access_denied",
        "invalid_request",
    }
    pending = error == "authorization_pending"

    if revoke_accepted and cancellation_observed:
        ok("Revoke accepted AND polling observed cancellation.")
        info(f"  Working shapes: {', '.join(succeeded)}")
        info(f"  Polling error : {error}")
        info("")
        info("  → F-20 PASS. Wire `auth_req_id` revoke into UC-09 cancel path (3B.2).")
        info("  Use the FIRST working shape from {succeeded}; document in")
        info("  docs/architecture/sprint-1-fixes.md §F-20.")
        return 0

    if revoke_accepted and pending:
        warn("Revoke endpoint returned 2xx, but polling still says authorization_pending.")
        info("  IS treats the revoke as a no-op for auth_req_id.")
        info("")
        info("  → F-20 FAIL (soft). Document the caveat; do not wire revoke.")
        return 0

    if not revoke_accepted and pending:
        ok("All four revoke shapes rejected; polling shows authorization_pending.")
        info("  IS does not expose an auth_req_id revocation primitive.")
        info("")
        info("  → F-20 FAIL. Accept ghost-approval caveat per Stage 1 §3 R-2.")
        info("  Document in docs/architecture/sprint-1-fixes.md §F-20.")
        info("  Denylist on MCP servers still defends if a ghost token reaches them.")
        return 0

    warn(f"Unexpected combination: revoke_accepted={revoke_accepted}, polling_error={error!r}.")
    info("Investigate before drawing a conclusion.")
    info("  → INDETERMINATE")
    return 1


if __name__ == "__main__":
    sys.exit(main())
