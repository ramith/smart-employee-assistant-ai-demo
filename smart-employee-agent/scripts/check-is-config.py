#!/usr/bin/env python3
"""scripts/check-is-config.py — full IS system-readiness audit.

Audits the live WSO2 IS instance to confirm every prerequisite the demo
fleet needs is in place: applications, API resources, scopes, users,
roles, and access-token claim mappings. Used:

    * Stage 11 pre-flight (sprint-4-stage-11-manual-gate.md §1.4).
    * Day-one check on a freshly imaged IS environment.
    * Sanity check after IS Console reconfigurations.

Stdlib only — no `requests`, no `httpx` dep. Operator-run, not hot path.

Checks (in execution order):

    Section 1 — Connectivity      JWKS endpoint up.
    Section 2 — JWKS payload      keys[] non-empty.
    Section 3 — Mgmt API auth     admin creds work against /api-resources.
    Section 4 — OAuth Apps        all 4 client IDs from orchestrator/.env
                                  registered as Applications.
    Section 4b — Subscriptions    orchestrator-mcp-client subscribed to HR+IT APIs.
    Section 4c — Agent subs       hr-agent/it-agent OAuth apps subscribed to ALL
                                  their API scopes (incl. hr_assets_write_rest).
    Section 4d — Agent auth       each agent's 4-value credential tuple actually
                                  authenticates via the App-Native 3-step flow
                                  (the exact thing ActorTokenProvider does at
                                  runtime — catches a stale/regenerated agent
                                  secret, which surfaces to users as
                                  "Sign-in is temporarily unavailable").
    Section 5 — API Resources     hr_server-api + it_server-api exist.
    Section 6 — Scopes            full Sprint 1-4 scope inventory present.
    Section 7 — Users (SCIM2)     the demo users (DEMO_USERS) exist.
    Section 8 — Roles (SCIM2 v2)  Employee + HR Admin exist.
    Section 9 — Token claims      sample access token carries username + email.

Exits 0 on full PASS; 1 on any FAIL. WARN does not fail (e.g. a check
where the Mgmt API endpoint shape varies across IS versions).

Usage:
    ./scripts/check-is-config.py
    ./scripts/check-is-config.py --base-url https://my-is:9443

Env-var overrides (all optional; defaults sourced from orchestrator/.env
when present):

    IS_BASE_URL                  https://13.60.190.47:9443
    IS_ADMIN_USER                admin                # for SCIM2 + Mgmt API
    IS_ADMIN_PASS                admin

    ORCHESTRATOR_MCP_CLIENT_ID       sourced from orchestrator/.env
    ORCHESTRATOR_MCP_CLIENT_SECRET   same
    ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID    sourced
    HR_AGENT_OAUTH_CLIENT_ID         sourced
    IT_AGENT_OAUTH_CLIENT_ID         sourced

    HR_API_RESOURCE_NAME         hr_server-api  (override if Console uses a different name)
    IT_API_RESOURCE_NAME         it_server-api

Section 9 (claim audit) uses the **password grant** against the demo
user — `client_credentials` returns an app-identity token without
`username`/`email`, so it can't verify the Sprint 4 plumbing. The
ROPC path mints a real user-bearing token and we decode that.

If ROPC isn't enabled on `orchestrator-mcp-client`, Section 9 emits a
WARN with the IS Console path to enable it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ─── Defaults ────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Locked scope inventory (verified 2026-05-11 against live IS Console).
# Note: there is no separate `hr_apply_rest` — apply-leave is gated by
# `hr_self_rest` (UC-13 §Preconditions line 18).
HR_SCOPES = {
    "hr_basic_rest",
    "hr_self_rest",
    "hr_read_rest",
    "hr_approve_rest",
    "hr_assets_write_rest",  # Sprint 4 NEW
}
IT_SCOPES = {
    "it_assets_read_rest",
    "it_assets_self_rest",  # Sprint 4 NEW
    "it_assets_write_rest",
}
# The demo accounts to check exist + carry an email attribute. Convention:
# username == email (see docs/wso2-is-setup.md §5.5). Override per demo.
DEMO_USERS = ["employee@example.com", "hradmin@example.com"]
# Role names verified 2026-05-11 against live IS Console — note `employee`
# is lowercase (deviates from the docs which say "Employee").
DEMO_ROLES = ["employee", "HR Admin"]
# Agents are managed via SCIM2 /scim2/Agents (separate from /applications).
# Each agent also has an auto-created Agent Application (OAuth) which is
# checked in Section 4; this is the IS-side "agent registry" view.
DEMO_AGENTS = ["orchestrator-agent", "hr-agent", "it-agent"]

# Locked role-scope matrix (sprint-4.md §6, post-Sprint-4 close).
# Roles are NOT inherited at IS — HR Admin explicitly carries Employee's
# scopes too. Sprint 4 NEW scopes (it_assets_self_rest, hr_assets_write_rest)
# must be attached for the demo to pass live verification.
EXPECTED_ROLE_SCOPES = {
    "employee": {
        "hr_basic_rest",
        "hr_self_rest",
        "it_assets_read_rest",
        "it_assets_self_rest",  # Sprint 4 NEW
    },
    "HR Admin": {
        # Employee's scopes (explicitly attached, no inheritance):
        "hr_basic_rest",
        "hr_self_rest",
        "it_assets_read_rest",
        "it_assets_self_rest",  # Sprint 4 NEW
        # HR Admin-only:
        "hr_read_rest",
        "hr_approve_rest",
        "it_assets_write_rest",
        "hr_assets_write_rest",  # Sprint 4 NEW
    },
}

# ─── Pretty output ───────────────────────────────────────────────────────────


def _supports_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"


if _supports_colour():
    _R, _G, _Y, _RD, _B = "\033[0m", "\033[32m", "\033[33m", "\033[31m", "\033[1m"
else:
    _R = _G = _Y = _RD = _B = ""


@dataclass
class Counters:
    passes: int = 0
    fails: int = 0
    warns: int = 0
    failed_checks: list[str] = field(default_factory=list)


C = Counters()


def hdr(text: str) -> None:
    print(f"\n{_B}── {text} ──{_R}")


def ok(label: str) -> None:
    print(f"  {_G}[PASS]{_R} {label}")
    C.passes += 1


def bad(label: str, reason: str) -> None:
    print(f"  {_RD}[FAIL]{_R} {label} — {reason}")
    C.fails += 1
    C.failed_checks.append(label)


def warn(label: str, reason: str) -> None:
    print(f"  {_Y}[WARN]{_R} {label} — {reason}")
    C.warns += 1


# ─── HTTP helper (stdlib, insecure TLS for self-signed dev cert) ─────────────


_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _basic_auth_header(user: str, pw: str) -> str:
    raw = f"{user}:{pw}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def http_get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0):
    """Return (status, body_text). Body is "" on connection failure."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"<network-error: {exc}>"


def http_post_form(
    url: str, *, data: dict[str, str], headers: dict[str, str] | None = None, timeout: float = 15.0
):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"<network-error: {exc}>"


def http_post_json(
    url: str, *, payload: dict, headers: dict[str, str] | None = None, timeout: float = 15.0
):
    """POST a JSON body. Returns (status, body_text). Body is "" on connection failure."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"<network-error: {exc}>"


# ─── PKCE (matches common/auth/actor_token_provider.py:_pkce_pair) ───────────


def _pkce_pair() -> tuple[str, str]:
    """Return a fresh ``(verifier, S256-challenge)`` pair, padding stripped."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ─── .env sourcing ───────────────────────────────────────────────────────────


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser. Ignores comments + blank lines + quoted values."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        out[key.strip()] = value
    return out


def _resolved(name: str, env_file: dict[str, str], default: str = "") -> str:
    return os.environ.get(name) or env_file.get(name) or default


# ─── base64url JWT decode ────────────────────────────────────────────────────


def b64url_decode_payload(jwt: str) -> dict | None:
    """Decode the JWT payload segment (no signature verification)."""
    parts = jwt.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    pad = "=" * (-len(segment) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(segment + pad).decode("utf-8", "replace"))
    except Exception:
        return None


# ─── Section runners ─────────────────────────────────────────────────────────


def check_connectivity(base_url: str) -> None:
    hdr("Section 1 — Connectivity")
    code, _ = http_get(f"{base_url}/oauth2/jwks", timeout=10)
    if code == 200:
        ok(f"JWKS reachable at {base_url}/oauth2/jwks")
    else:
        bad("JWKS reachable", f"expected 200, got {code}")


def check_jwks(base_url: str) -> None:
    hdr("Section 2 — JWKS payload")
    code, body = http_get(f"{base_url}/oauth2/jwks", timeout=10)
    if code != 200:
        bad("JWKS payload", "skipped (connectivity failed)")
        return
    try:
        keys = json.loads(body).get("keys") or []
    except Exception:
        bad("JWKS payload", "invalid JSON")
        return
    if keys:
        ok(f"JWKS keys[] populated ({len(keys)} key(s))")
    else:
        bad("JWKS payload", "keys[] empty")


def check_mgmt_api_auth(base_url: str, auth_hdr: str) -> bool:
    """Returns True if Mgmt API auth works — gates downstream sections."""
    hdr("Section 3 — Mgmt API auth")
    code, _ = http_get(
        f"{base_url}/api/server/v1/api-resources",
        headers={"Authorization": auth_hdr, "Accept": "application/json"},
    )
    if code == 200:
        ok("admin credentials accepted on /api/server/v1/api-resources")
        return True
    bad(
        "admin credentials",
        f"GET /api-resources returned {code} — set IS_ADMIN_USER + IS_ADMIN_PASS or fix admin account",
    )
    return False


def check_applications(base_url: str, auth_hdr: str, expected_client_ids: dict[str, str]) -> None:
    """Verify each known client_id is present.

    The IS list endpoint (`GET /applications`) does NOT return `clientId`
    in row data — only the filter form (`?filter=clientId eq <id>`) does.
    So query per-id; one round-trip per expected app.
    """
    hdr("Section 4 — OAuth Applications")
    if not expected_client_ids:
        warn(
            "applications",
            "no expected client IDs (orchestrator/.env not found and no env vars set)",
        )
        return
    for label, client_id in expected_client_ids.items():
        if not client_id:
            warn(label, "no client_id in env — skipping")
            continue
        code, body = http_get(
            f"{base_url}/api/server/v1/applications?filter=clientId+eq+{urllib.parse.quote(client_id)}",
            headers={"Authorization": auth_hdr, "Accept": "application/json"},
        )
        if code != 200:
            warn(label, f"filter query returned {code}")
            continue
        try:
            apps = json.loads(body).get("applications") or []
        except Exception:
            bad(label, "invalid JSON from /applications filter")
            continue
        if apps and any(a.get("clientId") == client_id for a in apps):
            app_name = apps[0].get("name", "(unnamed)")
            ok(f"{label} ({client_id[:8]}…) registered — app name: '{app_name}'")
        else:
            bad(label, f"client_id '{client_id}' not registered as any application")


def check_orchestrator_subscriptions(base_url: str, auth_hdr: str, mcp_client_id: str) -> None:
    """Section 4b — verify orchestrator-mcp-client is subscribed to HR + IT APIs.

    The SPA's Pattern C login authorize URL requests business scopes; IS will
    strip them unless the OAuth app is subscribed to the API resources that
    own those scopes. Without these subscriptions, the user signs in but
    token-A only carries `openid profile email` — Reports nav stays hidden,
    My Leaves panel can't load, reports proxy returns 403 on pre-flight.

    Subscribed via Console → Applications → orchestrator-mcp-client →
    Authorization tab → "+ Authorize resource".
    """
    hdr("Section 4b — orchestrator-mcp-client API subscriptions")
    if not mcp_client_id:
        warn("subscriptions", "ORCHESTRATOR_MCP_CLIENT_ID not in env — skipping")
        return

    # Resolve the app's IS-side id from the client_id.
    code, body = http_get(
        f"{base_url}/api/server/v1/applications?filter=clientId+eq+{urllib.parse.quote(mcp_client_id)}",
        headers={"Authorization": auth_hdr, "Accept": "application/json"},
    )
    if code != 200:
        warn("subscriptions", f"could not look up orchestrator-mcp-client: HTTP {code}")
        return
    try:
        apps = json.loads(body).get("applications") or []
    except Exception:
        bad("subscriptions", "invalid JSON from /applications filter")
        return
    if not apps:
        bad("subscriptions", "orchestrator-mcp-client not found — see Section 4")
        return
    app_id = apps[0].get("id")

    code, body = http_get(
        f"{base_url}/api/server/v1/applications/{app_id}/authorized-apis",
        headers={"Authorization": auth_hdr, "Accept": "application/json"},
    )
    if code != 200:
        bad("subscriptions", f"GET /authorized-apis returned {code}")
        return
    try:
        subs = json.loads(body)
    except Exception:
        bad("subscriptions", "invalid JSON from /authorized-apis")
        return
    if not isinstance(subs, list) or not subs:
        bad(
            "subscriptions",
            "orchestrator-mcp-client has NO API authorizations — "
            "subscribe in Console → Applications → orchestrator-mcp-client → "
            "Authorization tab → '+ Authorize resource' for HR API + IT API.",
        )
        return

    # Build name → scope-set map from the subscriptions.
    sub_by_name: dict[str, set[str]] = {}
    for entry in subs:
        name = entry.get("displayName") or entry.get("identifier") or ""
        scopes = {s.get("name") for s in (entry.get("authorizedScopes") or []) if s.get("name")}
        sub_by_name[name] = scopes

    # HR coverage.
    hr_sub = sub_by_name.get("HR API") or sub_by_name.get("urn:hr:api") or set()
    hr_missing = HR_SCOPES - hr_sub
    if not hr_missing:
        ok(f"HR API subscription: {len(HR_SCOPES)} scope(s) authorized on orchestrator-mcp-client")
    else:
        bad(
            "HR API subscription",
            f"missing: {sorted(hr_missing)} — add via Console → orchestrator-mcp-client → "
            f"Authorization → HR API → tick the scope(s).",
        )

    # IT coverage.
    it_sub = sub_by_name.get("IT API") or sub_by_name.get("urn:it:api") or set()
    it_missing = IT_SCOPES - it_sub
    if not it_missing:
        ok(f"IT API subscription: {len(IT_SCOPES)} scope(s) authorized on orchestrator-mcp-client")
    else:
        bad(
            "IT API subscription",
            f"missing: {sorted(it_missing)} — add via Console → orchestrator-mcp-client → "
            f"Authorization → IT API → tick the scope(s).",
        )


def _app_authorized_scopes_by_api(
    base_url: str, auth_hdr: str, client_id: str, label: str
) -> dict[str, set[str]] | None:
    """Resolve an OAuth app (by client_id) → ``{API display-name/identifier: {scope, …}}``.

    Returns ``None`` (and emits a warn/bad) on lookup failure. Used by the
    subscription checks below.
    """
    if not client_id:
        warn(f"{label} subscriptions", "client_id not in env (orchestrator/.env) — skipping")
        return None
    code, body = http_get(
        f"{base_url}/api/server/v1/applications?filter=clientId+eq+{urllib.parse.quote(client_id)}",
        headers={"Authorization": auth_hdr, "Accept": "application/json"},
    )
    if code != 200:
        warn(f"{label} subscriptions", f"could not look up OAuth app: HTTP {code}")
        return None
    try:
        apps = json.loads(body).get("applications") or []
    except Exception:
        bad(f"{label} subscriptions", "invalid JSON from /applications filter")
        return None
    if not apps:
        bad(f"{label} subscriptions", f"no OAuth app for client_id {client_id[:8]}… — see Section 4")
        return None
    app_id = apps[0].get("id")
    code, body = http_get(
        f"{base_url}/api/server/v1/applications/{app_id}/authorized-apis",
        headers={"Authorization": auth_hdr, "Accept": "application/json"},
    )
    if code != 200:
        bad(f"{label} subscriptions", f"GET /authorized-apis returned {code}")
        return None
    try:
        subs = json.loads(body)
    except Exception:
        bad(f"{label} subscriptions", "invalid JSON from /authorized-apis")
        return None
    out: dict[str, set[str]] = {}
    for entry in (subs or []):
        name = entry.get("displayName") or entry.get("identifier") or ""
        out[name] = {s.get("name") for s in (entry.get("authorizedScopes") or []) if s.get("name")}
    return out


def check_agent_app_subscriptions(
    base_url: str, auth_hdr: str, hr_agent_client_id: str, it_agent_client_id: str
) -> None:
    """Section 4c — the specialist-agent OAuth apps must be subscribed to their API resources.

    Each agent runs CIBA requesting business scopes (e.g. ``hr.cubicle_assign``
    → ``openid hr_assets_write_rest``). WSO2 IS **silently strips** any
    requested scope the OAuth app isn't subscribed to — so a missing
    subscription doesn't error at CIBA initiation; the token-C just comes back
    under-scoped, and the failure surfaces later as a 401 / ERR-MCP-003 from
    the MCP server (e.g. "I couldn't assign the cubicle. There was an issue
    with authorization."). The ``hr-agent`` OAuth app must be subscribed to ALL
    HR API scopes (including ``hr_assets_write_rest``); the ``it-agent`` OAuth
    app to ALL IT API scopes. Subscribe via Console → Applications → <agent
    OAuth App> → API Authorization → "+ Authorize resource".
    """
    hdr("Section 4c — agent OAuth app API subscriptions")

    hr_subs = _app_authorized_scopes_by_api(base_url, auth_hdr, hr_agent_client_id, "hr-agent")
    if hr_subs is not None:
        have = hr_subs.get("HR API") or hr_subs.get("urn:hr:api") or set()
        missing = HR_SCOPES - have
        if not missing:
            ok(f"hr-agent OAuth app: subscribed to all {len(HR_SCOPES)} HR API scope(s)")
        else:
            hint = (
                " (hr_assets_write_rest is the one the cubicle/seat-assign chat flow needs)"
                if "hr_assets_write_rest" in missing else ""
            )
            bad(
                "hr-agent HR API subscription",
                f"missing: {sorted(missing)}{hint} — Console → Applications → the hr-agent "
                f"OAuth App → API Authorization → HR API → tick the scope(s).",
            )

    it_subs = _app_authorized_scopes_by_api(base_url, auth_hdr, it_agent_client_id, "it-agent")
    if it_subs is not None:
        have = it_subs.get("IT API") or it_subs.get("urn:it:api") or set()
        missing = IT_SCOPES - have
        if not missing:
            ok(f"it-agent OAuth app: subscribed to all {len(IT_SCOPES)} IT API scope(s)")
        else:
            bad(
                "it-agent IT API subscription",
                f"missing: {sorted(missing)} — Console → Applications → the it-agent "
                f"OAuth App → API Authorization → IT API → tick the scope(s).",
            )


def _appnative_auth_probe(
    base_url: str,
    *,
    oauth_client_id: str,
    oauth_client_secret: str,
    redirect_uri: str,
    agent_id: str,
    agent_secret: str,
    scope: str = "openid internal_login",
) -> tuple[bool, str]:
    """Run the App-Native 3-step auth flow for one agent. Returns (ok, detail).

    Mirrors ``common/auth/actor_token_provider.ActorTokenProvider._mint``:
      1. POST /oauth2/authorize (Basic auth, form) → flowId + authenticatorId
      2. POST /oauth2/authn   (JSON) → authorization code
      3. POST /oauth2/token   (Basic auth, form, grant_type=authorization_code) → access_token

    A failure at step 2 (HTTP 200 with no code) almost always means the agent
    secret is stale — IS returns flowStatus=FAIL_INCOMPLETE / ABA-60003. We
    surface that distinctly so the operator knows to regenerate + update .env.
    """
    verifier, challenge = _pkce_pair()
    client_auth = {"Authorization": _basic_auth_header(oauth_client_id, oauth_client_secret)}

    # ── Step 1: /oauth2/authorize ──────────────────────────────────────────
    code, body = http_post_form(
        f"{base_url}/oauth2/authorize",
        data={
            "client_id": oauth_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "response_mode": "direct",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        headers=client_auth,
    )
    if code != 200:
        snippet = body[:200].replace("\n", " ")
        hint = ""
        if code in (401, 400) and ("client" in body.lower() or "unauthorized" in body.lower()):
            hint = " — OAuth client_id/secret likely stale (App regenerated). Update *_AGENT_OAUTH_CLIENT_* in .env."
        elif code in (400, 403) and "callback" in body.lower():
            hint = f" — redirect_uri '{redirect_uri}' not registered on the agent's OAuth App."
        return False, f"step 1 /oauth2/authorize → HTTP {code}: {snippet}{hint}"
    try:
        doc = json.loads(body)
    except Exception:
        return False, f"step 1 /oauth2/authorize → 200 but body is not JSON: {body[:200]}"
    flow_id = doc.get("flowId")
    authenticators = ((doc.get("nextStep") or {}).get("authenticators")) or []
    authenticator_id = authenticators[0].get("authenticatorId") if authenticators else None
    if not flow_id or not authenticator_id:
        return False, f"step 1 /oauth2/authorize → 200 but flowId/authenticatorId absent: {body[:200]}"

    # ── Step 2: /oauth2/authn ──────────────────────────────────────────────
    code, body = http_post_json(
        f"{base_url}/oauth2/authn",
        payload={
            "flowId": flow_id,
            "selectedAuthenticator": {
                "authenticatorId": authenticator_id,
                "params": {"username": agent_id, "password": agent_secret},
            },
        },
    )
    if code != 200:
        return False, f"step 2 /oauth2/authn → HTTP {code}: {body[:200]}"
    try:
        doc = json.loads(body)
    except Exception:
        return False, f"step 2 /oauth2/authn → 200 but body is not JSON: {body[:200]}"
    auth_code = (doc.get("authData") or {}).get("code") or doc.get("code")
    if not auth_code:
        flow_status = doc.get("flowStatus", "UNKNOWN")
        messages = (doc.get("nextStep") or {}).get("messages") or []
        err_msgs = [
            f"{m.get('messageId', '?')}: {m.get('message', '')}"
            for m in messages
            if isinstance(m, dict) and m.get("type") == "ERROR"
        ]
        is_cred_fail = flow_status in ("FAIL_INCOMPLETE", "INCOMPLETE", "FAIL") or any(
            "ABA-60003" in m or "login.fail" in m for m in err_msgs
        )
        hint = (
            " — agent authentication did not complete; the agent secret is most "
            "likely stale (rotated by 'Regenerate' in IS Console, or the agent "
            "was recreated). Update the agent's *_AGENT_SECRET in the service "
            ".env and recreate the container."
            if is_cred_fail else ""
        )
        return False, (
            f"step 2 /oauth2/authn → flowStatus={flow_status}"
            + (f" errors={err_msgs}" if err_msgs else "")
            + hint
        )

    # ── Step 3: /oauth2/token ──────────────────────────────────────────────
    code, body = http_post_form(
        f"{base_url}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": oauth_client_id,
            "code": auth_code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
        headers=client_auth,
    )
    if code != 200:
        return False, f"step 3 /oauth2/token → HTTP {code}: {body[:200]}"
    try:
        tok = json.loads(body)
    except Exception:
        return False, f"step 3 /oauth2/token → 200 but body is not JSON: {body[:200]}"
    access_token = tok.get("access_token")
    if not access_token:
        return False, f"step 3 /oauth2/token → 200 but access_token absent: {body[:200]}"
    expires_in = tok.get("expires_in", "?")
    granted_scope = tok.get("scope", "")
    return True, f"3-step App-Native auth OK (expires_in={expires_in}s, scope={granted_scope!r})"


def check_agent_appnative_auth(base_url: str, agents: list[dict[str, str]]) -> None:
    """Section 4d — verify each agent's 4-value credential tuple actually authenticates.

    Section 4 only confirms the agent's OAuth App *exists*; it does not prove the
    agent can mint its I4 actor-token. This section runs the same App-Native
    3-step flow ``ActorTokenProvider`` runs at runtime. If the orchestrator-agent
    leg fails here, Pattern C login's ``/auth/exchange`` returns 502 and the SPA
    shows the generic "Sign-in is temporarily unavailable" — so this check is the
    direct early-warning for that user-visible failure.
    """
    hdr("Section 4d — Agent App-Native authentication")
    if not agents:
        warn(
            "agent auth",
            "no agent credentials found (orchestrator/.env, hr_agent/.env, it_agent/.env) — skipping",
        )
        return
    for a in agents:
        label = a["label"]
        missing = [k for k in ("agent_id", "agent_secret", "oauth_client_id", "oauth_client_secret") if not a.get(k)]
        if missing:
            warn(f"{label} auth", f"incomplete credentials in .env (missing: {missing}) — skipping")
            continue
        success, detail = _appnative_auth_probe(
            base_url,
            oauth_client_id=a["oauth_client_id"],
            oauth_client_secret=a["oauth_client_secret"],
            redirect_uri=a.get("redirect_uri") or "http://localhost:9999/agent-callback",
            agent_id=a["agent_id"],
            agent_secret=a["agent_secret"],
        )
        if success:
            ok(f"{label} ({a['agent_id'][:8]}…): {detail}")
        else:
            bad(f"{label} auth", detail)


def check_api_resources(
    base_url: str, auth_hdr: str, hr_specs: list[str], it_specs: list[str]
) -> dict[str, str]:
    """Returns map logical-key → resourceId for the two demo API resources.

    Each spec list is a set of acceptable matchers (display name OR
    identifier) — IS Console exposes both fields and operators commonly
    use either. Returns the first-matched resource per group.
    """
    hdr("Section 5 — API Resources")
    code, body = http_get(
        f"{base_url}/api/server/v1/api-resources?limit=200",
        headers={"Authorization": auth_hdr, "Accept": "application/json"},
    )
    if code != 200:
        bad("API resources list", f"GET /api-resources returned {code}")
        return {}
    try:
        resources = json.loads(body).get("apiResources") or []
    except Exception:
        bad("API resources list", "invalid JSON from /api-resources")
        return {}

    def _find(specs: list[str], group_label: str) -> tuple[str, str] | None:
        for r in resources:
            name = r.get("name") or ""
            ident = r.get("identifier") or ""
            for s in specs:
                if s and (s == name or s == ident):
                    return (name or ident, r.get("id"))
        return None

    out: dict[str, str] = {}
    hr_match = _find(hr_specs, "HR")
    if hr_match:
        ok(f"HR API resource: '{hr_match[0]}' (matched: {hr_specs[0]} or fallback)")
        out["HR"] = hr_match[1]
    else:
        bad("HR API resource", f"not found — tried: {hr_specs}. Register in Console → API Resources")
    it_match = _find(it_specs, "IT")
    if it_match:
        ok(f"IT API resource: '{it_match[0]}'")
        out["IT"] = it_match[1]
    else:
        bad("IT API resource", f"not found — tried: {it_specs}. Register in Console → API Resources")
    return out


def check_scopes(base_url: str, auth_hdr: str, resources: dict[str, str]) -> None:
    hdr("Section 6 — Scopes per resource")

    def _audit(group_label: str, expected: set[str]) -> None:
        rid = resources.get(group_label)
        if not rid:
            warn(f"{group_label} API scopes", "skipped (resource not found in §5)")
            return
        code, body = http_get(
            f"{base_url}/api/server/v1/api-resources/{rid}/scopes",
            headers={"Authorization": auth_hdr, "Accept": "application/json"},
        )
        if code != 200:
            warn(f"{group_label} API scopes", f"GET /scopes returned {code}")
            return
        try:
            scopes = {s.get("name") for s in json.loads(body) if s.get("name")}
        except Exception:
            bad(f"{group_label} API scopes", "invalid JSON")
            return
        missing = expected - scopes
        if not missing:
            ok(f"{group_label} API: {len(expected)} required scope(s) present")
        else:
            bad(
                f"{group_label} API scopes",
                "missing: " + ", ".join(sorted(missing)) + " — register in IS Console",
            )

    _audit("HR", HR_SCOPES)
    _audit("IT", IT_SCOPES)


def check_users(base_url: str, auth_hdr: str) -> None:
    hdr("Section 7 — Demo users (SCIM2)")
    for username in DEMO_USERS:
        url = f"{base_url}/scim2/Users?filter={urllib.parse.quote(f'userName eq {username}')}"
        code, body = http_get(url, headers={"Authorization": auth_hdr, "Accept": "application/scim+json"})
        if code == 200:
            try:
                total = int(json.loads(body).get("totalResults", 0))
            except Exception:
                total = 0
            if total >= 1:
                ok(f"user '{username}' exists ({total} match)")
            else:
                bad(f"user '{username}'", "totalResults=0 — create in Console → User Management")
        else:
            warn(f"user '{username}'", f"GET /scim2/Users returned {code}")


def check_roles(base_url: str, auth_hdr: str) -> dict[str, dict]:
    """Returns map role-name → role-record (SCIM2). Used by §8b for scope check."""
    hdr("Section 8 — Demo roles (SCIM2 v2)")
    out: dict[str, dict] = {}
    for role in DEMO_ROLES:
        url = f"{base_url}/scim2/v2/Roles?filter={urllib.parse.quote(f'displayName eq {role}')}"
        code, body = http_get(url, headers={"Authorization": auth_hdr, "Accept": "application/scim+json"})
        if code != 200:
            warn(f"role '{role}'", f"GET /scim2/v2/Roles returned {code}")
            continue
        try:
            doc = json.loads(body)
            resources = doc.get("Resources") or []
        except Exception:
            bad(f"role '{role}'", "invalid SCIM2 JSON")
            continue
        if not resources:
            bad(f"role '{role}'", "totalResults=0 — create in Console → User Management → Roles")
            continue
        ok(f"role '{role}' exists")
        out[role] = resources[0]
    return out


def check_role_scope_bindings(base_url: str, auth_hdr: str, roles: dict[str, dict]) -> None:
    """Section 8b — verify each role carries its expected scope set.

    The IS SCIM2 v2 Roles API returns `permissions` (list of scope-name
    objects or strings) on the role resource. For maximum compatibility
    across IS versions we accept either shape: the list may contain
    strings (plain scope names) OR dicts with `value` / `display` keys.
    """
    hdr("Section 8b — Role-scope bindings (sprint-4.md §6 matrix)")
    for role_name, expected_scopes in EXPECTED_ROLE_SCOPES.items():
        record = roles.get(role_name)
        if not record:
            warn(f"role '{role_name}' bindings", "skipped (role not found in §8)")
            continue
        # Re-fetch the full role record (the search response is sometimes
        # abbreviated). Use the role id from the listing.
        rid = record.get("id")
        if rid:
            code, body = http_get(
                f"{base_url}/scim2/v2/Roles/{rid}",
                headers={"Authorization": auth_hdr, "Accept": "application/scim+json"},
            )
            if code == 200:
                try:
                    record = json.loads(body)
                except Exception:
                    pass
        # Tolerant extraction — IS variants emit either strings or {value, display}.
        attached: set[str] = set()
        for entry in record.get("permissions", []) or []:
            if isinstance(entry, str):
                attached.add(entry)
            elif isinstance(entry, dict):
                val = entry.get("value") or entry.get("display") or entry.get("name")
                if val:
                    attached.add(val)
        missing = expected_scopes - attached
        extra = attached - expected_scopes
        if not missing:
            ok(f"role '{role_name}': {len(expected_scopes)} expected scope(s) attached")
            if extra:
                # Extra scopes aren't a failure — just note them.
                warn(
                    f"role '{role_name}' extras",
                    f"role also carries {len(extra)} unlisted scope(s): {sorted(extra)} (informational)",
                )
        else:
            bad(
                f"role '{role_name}' bindings",
                f"missing scope(s): {sorted(missing)} — attach in Console → User Management → Roles → "
                f"{role_name} → Permissions tab",
            )


def check_agents(base_url: str, auth_hdr: str) -> None:
    """Section 7b — verify SCIM2 Agents exist (separate from OAuth Apps).

    Each demo agent (orchestrator-agent, hr-agent, it-agent) is registered
    in the SCIM2 Agents registry and has an auto-created Agent Application
    companion (verified in Section 4 via /applications client_ids). This
    section confirms the SCIM2 side is also intact.
    """
    hdr("Section 7b — SCIM2 Agents")
    code, body = http_get(
        f"{base_url}/scim2/Agents",
        headers={"Authorization": auth_hdr, "Accept": "application/scim+json"},
    )
    if code != 200:
        warn(
            "SCIM2 Agents",
            f"GET /scim2/Agents returned {code}; on some IS builds the path is "
            "/scim2/v2/Agents. Verify manually in Console → Agents.",
        )
        return
    try:
        doc = json.loads(body)
        resources = doc.get("Resources") or []
    except Exception:
        bad("SCIM2 Agents", "invalid SCIM2 JSON")
        return
    # WSO2 IS stores agent display name in a nested custom schema:
    #   `urn:scim:wso2:agent:schema`.`DisplayName`
    # The top-level userName is `AGENT/<uuid>` (SCIM convention) and not
    # human-meaningful. Pull from the custom schema; fall back to top
    # level if a future IS version stores it elsewhere.
    AGENT_SCHEMA = "urn:scim:wso2:agent:schema"
    present_names: set[str] = set()
    for r in resources:
        wso2 = r.get(AGENT_SCHEMA) or {}
        if isinstance(wso2, dict):
            dn = wso2.get("DisplayName")
            if dn:
                present_names.add(dn)
        # Also accept the top-level fields if present (future-proof).
        for k in ("displayName", "name"):
            v = r.get(k)
            if v:
                present_names.add(v)
    for agent in DEMO_AGENTS:
        if agent in present_names:
            ok(f"agent '{agent}' registered (SCIM2)")
        else:
            bad(
                f"agent '{agent}'",
                "not found via /scim2/Agents — register in Console → Agents",
            )


def check_user_attributes(base_url: str, auth_hdr: str) -> None:
    """Section 9 — verify demo users have userName + email attributes set.

    The Sprint 4 plumbing requires `username` + `email` claims in the
    user-bearing access token issued at Pattern C login. Minting such a
    token from this script would require browser interaction (auth-code
    flow with PKCE — `orchestrator-mcp-client` does not enable the
    password grant). Instead we verify the prerequisite: the user record
    in IS carries the source attributes for those claims. If the source
    is missing, the OAuth attribute mapping has nothing to project.

    A green PASS here is necessary but NOT sufficient for full
    user-bearing-token verification. After this script passes, do a
    Pattern C sign-in via the SPA and inspect `Session.token_a` claims
    (or grep orchestrator logs for `auth_exchange_success`) to confirm
    the mapping is wired through.
    """
    hdr("Section 9 — Demo user attributes (SCIM2)")
    for username in DEMO_USERS:
        url = f"{base_url}/scim2/Users?filter={urllib.parse.quote(f'userName eq {username}')}"
        code, body = http_get(
            url, headers={"Authorization": auth_hdr, "Accept": "application/scim+json"}
        )
        if code != 200:
            warn(f"user '{username}' attributes", f"SCIM2 returned {code}")
            continue
        try:
            doc = json.loads(body)
            resources = doc.get("Resources") or []
        except Exception:
            bad(f"user '{username}' attributes", "invalid SCIM2 JSON")
            continue
        if not resources:
            bad(f"user '{username}' attributes", "user not found")
            continue
        u = resources[0]
        uname = u.get("userName") or ""
        # SCIM2 userName may be userstore-qualified ("PRIMARY/employee_user").
        uname_bare = uname.split("/", 1)[-1] if uname else ""
        has_username = bool(uname)
        emails = u.get("emails") or []
        email_vals = [
            (e.get("value") if isinstance(e, dict) else e)
            for e in emails
            if (isinstance(e, dict) and e.get("value")) or (isinstance(e, str) and e)
        ]
        has_email = bool(email_vals)
        _ = uname_bare  # (kept for log clarity; no longer compared to the email)
        if has_username and has_email:
            # S5.18: the email IS the `sub` (and the CIBA `login_hint`), resolved
            # to the local user by IS Multi-Attribute Login — userName no longer
            # has to equal the email local-part. We only require: email is set.
            ok(f"user '{username}' has userName + email set (sub/claim/login_hint source available)")
        else:
            missing = []
            if not has_username:
                missing.append("userName")
            if not has_email:
                missing.append("emails[].value")
            bad(
                f"user '{username}' attributes",
                f"missing: {', '.join(missing)} — every demo user needs an email "
                f"(it becomes the OIDC `sub` AND the CIBA `login_hint`). Set in "
                f"Console → User Management → select user → Profile, then re-run.",
            )

    print()
    print(
        f"  {_Y}note:{_R} Section 9 verifies the SCIM2 source attributes only. Two "
        "things it can't check from here:"
    )
    print(
        "    1. Multi-Attribute Login must be ENABLED with the email claim "
        "(Console → Login & Registration → Alternative Login Identifiers; allowed "
        "list must include http://wso2.org/claims/emailaddress) — required so the "
        "email `login_hint` resolves at CIBA. See docs/wso2-is-setup.md §5.6."
    )
    print(
        "    2. The OAuth claim mapping into access tokens — sign in to the SPA "
        "(Pattern C), then: docker compose logs orchestrator | grep auth_exchange_success"
    )


# ─── Main ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Full system-readiness audit for the smart-employee-agent demo IS."
    )
    parser.add_argument("--base-url", help="IS base URL (overrides IS_BASE_URL)")
    parser.add_argument(
        "--env-file",
        default=str(PROJECT_ROOT / "orchestrator" / ".env"),
        help="Path to orchestrator/.env for client-ID sourcing",
    )
    args = parser.parse_args(argv)

    env_file = load_env_file(Path(args.env_file))

    base_url = args.base_url or os.environ.get("IS_BASE_URL") or "https://13.60.190.47:9443"
    admin_user = os.environ.get("IS_ADMIN_USER", "admin")
    admin_pass = os.environ.get("IS_ADMIN_PASS", "admin")

    hr_name = os.environ.get("HR_API_RESOURCE_NAME", "hr_server-api")
    it_name = os.environ.get("IT_API_RESOURCE_NAME", "it_server-api")

    mcp_client_id = _resolved("ORCHESTRATOR_MCP_CLIENT_ID", env_file)

    expected_client_ids = {
        "orchestrator-mcp-client": mcp_client_id,
        "orchestrator-agent-oauth": _resolved("ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID", env_file),
        "hr_agent OAuth App": _resolved("HR_AGENT_OAUTH_CLIENT_ID", env_file),
        "it_agent OAuth App": _resolved("IT_AGENT_OAUTH_CLIENT_ID", env_file),
    }
    expected_client_ids = {k: v for k, v in expected_client_ids.items() if v}

    # Agent 4-value credentials live in each service's own .env (the orchestrator
    # one for orchestrator-agent; hr_agent/.env and it_agent/.env for the rest).
    # Section 4d authenticates whatever's available; missing files → that agent skipped.
    hr_env = load_env_file(PROJECT_ROOT / "hr_agent" / ".env")
    it_env = load_env_file(PROJECT_ROOT / "it_agent" / ".env")
    agents = [
        {
            "label": "orchestrator-agent",
            "agent_id": _resolved("ORCHESTRATOR_AGENT_ID", env_file),
            "agent_secret": _resolved("ORCHESTRATOR_AGENT_SECRET", env_file),
            "oauth_client_id": _resolved("ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID", env_file),
            "oauth_client_secret": _resolved("ORCHESTRATOR_AGENT_OAUTH_CLIENT_SECRET", env_file),
            # Mirrors orchestrator/config.py: the agent's redirect_uri is the MCP client's.
            "redirect_uri": _resolved(
                "ORCHESTRATOR_MCP_CLIENT_REDIRECT_URI", env_file, "http://localhost:8090/agent-callback"
            ),
        },
        {
            "label": "hr-agent",
            "agent_id": _resolved("HR_AGENT_ID", hr_env),
            "agent_secret": _resolved("HR_AGENT_SECRET", hr_env),
            "oauth_client_id": _resolved("HR_AGENT_OAUTH_CLIENT_ID", hr_env),
            "oauth_client_secret": _resolved("HR_AGENT_OAUTH_CLIENT_SECRET", hr_env),
            "redirect_uri": _resolved("HR_AGENT_REDIRECT_URI", hr_env, "http://localhost:9999/agent-callback"),
        },
        {
            "label": "it-agent",
            "agent_id": _resolved("IT_AGENT_ID", it_env),
            "agent_secret": _resolved("IT_AGENT_SECRET", it_env),
            "oauth_client_id": _resolved("IT_AGENT_OAUTH_CLIENT_ID", it_env),
            "oauth_client_secret": _resolved("IT_AGENT_OAUTH_CLIENT_SECRET", it_env),
            "redirect_uri": _resolved("IT_AGENT_REDIRECT_URI", it_env, "http://localhost:9999/agent-callback"),
        },
    ]
    # Drop agents with zero creds at all (e.g. that service's .env is absent).
    agents = [a for a in agents if any(a.get(k) for k in ("agent_id", "agent_secret", "oauth_client_id", "oauth_client_secret"))]

    auth_hdr = _basic_auth_header(admin_user, admin_pass)

    print(f"{_B}IS system-readiness audit{_R}  ({base_url})")

    check_connectivity(base_url)
    check_jwks(base_url)

    if not check_mgmt_api_auth(base_url, auth_hdr):
        # Sections 4-9 all depend on Mgmt API auth; nothing further to do.
        _summarise()
        return 1

    check_applications(base_url, auth_hdr, expected_client_ids)
    check_orchestrator_subscriptions(base_url, auth_hdr, mcp_client_id)
    check_agent_app_subscriptions(
        base_url, auth_hdr,
        _resolved("HR_AGENT_OAUTH_CLIENT_ID", env_file),
        _resolved("IT_AGENT_OAUTH_CLIENT_ID", env_file),
    )
    check_agent_appnative_auth(base_url, agents)
    # API resources matched by display name OR identifier (operators use either).
    hr_specs = [hr_name, "HR API", "urn:hr:api"]
    it_specs = [it_name, "IT API", "urn:it:api"]
    resources = check_api_resources(base_url, auth_hdr, hr_specs, it_specs)
    check_scopes(base_url, auth_hdr, resources)
    check_users(base_url, auth_hdr)
    check_agents(base_url, auth_hdr)
    roles = check_roles(base_url, auth_hdr)
    check_role_scope_bindings(base_url, auth_hdr, roles)
    check_user_attributes(base_url, auth_hdr)

    return _summarise()


def _summarise() -> int:
    hdr("Summary")
    print(f"  {_G}PASS:{_R} {C.passes}  |  {_RD}FAIL:{_R} {C.fails}  |  {_Y}WARN:{_R} {C.warns}")
    if C.fails:
        print(f"  {_RD}Failures:{_R} " + ", ".join(C.failed_checks))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
