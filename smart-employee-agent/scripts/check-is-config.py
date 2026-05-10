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
    Section 5 — API Resources     hr_server-api + it_server-api exist.
    Section 6 — Scopes            full Sprint 1-4 scope inventory present.
    Section 7 — Users (SCIM2)     employee_user + hr_admin_user exist.
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

    DEMO_USERNAME                employee_user        # ROPC user for §9
    DEMO_PASSWORD                NewsMax@1234         # per wso2-is-setup.md

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

# Locked scope inventory (Sprint 4 close — sprint-4.md §6 + scope-policy.md §2)
HR_SCOPES = {
    "hr_basic_rest",
    "hr_self_rest",
    "hr_apply_rest",
    "hr_read_rest",
    "hr_approve_rest",
    "hr_assets_write_rest",  # Sprint 4 NEW
}
IT_SCOPES = {
    "it_assets_read_rest",
    "it_assets_self_rest",  # Sprint 4 NEW
    "it_assets_write_rest",
}
DEMO_USERS = ["employee_user", "hr_admin_user"]
DEMO_ROLES = ["Employee", "HR Admin"]
REQUIRED_CLAIMS = ["username", "email"]

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
    """Verify each known client_id is present in the Applications list."""
    hdr("Section 4 — OAuth Applications")
    if not expected_client_ids:
        warn(
            "applications",
            "no expected client IDs (orchestrator/.env not found and no env vars set)",
        )
        return
    code, body = http_get(
        f"{base_url}/api/server/v1/applications?limit=200",
        headers={"Authorization": auth_hdr, "Accept": "application/json"},
    )
    if code != 200:
        warn("applications", f"GET /applications returned {code}; verify manually in IS Console")
        return
    try:
        apps = json.loads(body).get("applications") or []
    except Exception:
        bad("applications", "invalid JSON from /applications")
        return
    present_client_ids = {a.get("clientId") for a in apps if a.get("clientId")}
    present_names = {a.get("name") for a in apps if a.get("name")}
    for label, client_id in expected_client_ids.items():
        if client_id and client_id in present_client_ids:
            ok(f"{label} ({client_id[:8]}…) registered")
        elif label in present_names:
            ok(f"{label} registered (matched by name)")
        else:
            bad(f"{label}", f"client_id '{client_id}' not in /applications response")


def check_api_resources(
    base_url: str, auth_hdr: str, hr_name: str, it_name: str
) -> dict[str, str]:
    """Returns map name → resourceId for the two demo API resources."""
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
    by_name = {r.get("name"): r.get("id") for r in resources if r.get("name")}
    out: dict[str, str] = {}
    for label in (hr_name, it_name):
        if label in by_name:
            ok(f"API resource '{label}' exists")
            out[label] = by_name[label]
        else:
            bad(f"API resource '{label}'", "not found — register in IS Console → API Resources")
    return out


def check_scopes(base_url: str, auth_hdr: str, resources: dict[str, str], hr_name: str, it_name: str) -> None:
    hdr("Section 6 — Scopes per resource")

    def _audit(resource_name: str, expected: set[str]) -> None:
        rid = resources.get(resource_name)
        if not rid:
            warn(f"{resource_name} scopes", "skipped (resource not found in §5)")
            return
        code, body = http_get(
            f"{base_url}/api/server/v1/api-resources/{rid}/scopes",
            headers={"Authorization": auth_hdr, "Accept": "application/json"},
        )
        if code != 200:
            warn(f"{resource_name} scopes", f"GET /scopes returned {code}")
            return
        try:
            scopes = {s.get("name") for s in json.loads(body) if s.get("name")}
        except Exception:
            bad(f"{resource_name} scopes", "invalid JSON")
            return
        missing = expected - scopes
        if not missing:
            ok(f"{resource_name}: {len(expected)} required scope(s) present")
        else:
            bad(
                f"{resource_name} scopes",
                "missing: " + ", ".join(sorted(missing)) + " — register in IS Console",
            )

    _audit(hr_name, HR_SCOPES)
    _audit(it_name, IT_SCOPES)


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


def check_roles(base_url: str, auth_hdr: str) -> None:
    hdr("Section 8 — Demo roles (SCIM2 v2)")
    for role in DEMO_ROLES:
        url = f"{base_url}/scim2/v2/Roles?filter={urllib.parse.quote(f'displayName eq {role}')}"
        code, body = http_get(url, headers={"Authorization": auth_hdr, "Accept": "application/scim+json"})
        if code == 200:
            try:
                total = int(json.loads(body).get("totalResults", 0))
            except Exception:
                total = 0
            if total >= 1:
                ok(f"role '{role}' exists")
            else:
                bad(f"role '{role}'", "totalResults=0 — create in Console → User Management → Roles")
        else:
            warn(f"role '{role}'", f"GET /scim2/v2/Roles returned {code} — check manually")


def check_token_claims(
    base_url: str,
    *,
    mcp_client_id: str,
    mcp_client_secret: str,
    demo_username: str,
    demo_password: str,
) -> None:
    """Section 9 — verify a USER-BEARING token carries username + email.

    Uses the password grant (ROPC) against the demo user with the
    existing orchestrator-mcp-client as the OAuth client. client_credentials
    won't work here — that grant returns an app-identity token with no
    user claims, which can't verify the Sprint 4 plumbing requirement
    that hr_server / it_server validate_token reads claims.username
    from a user-bearing access token.
    """
    hdr("Section 9 — Sample-token claim audit (ROPC)")
    if not mcp_client_id or not mcp_client_secret:
        warn(
            "sample token",
            "ORCHESTRATOR_MCP_CLIENT_ID / _SECRET not in orchestrator/.env — skipping",
        )
        return
    code, body = http_post_form(
        f"{base_url}/oauth2/token",
        data={
            "grant_type": "password",
            "username": demo_username,
            "password": demo_password,
            "scope": "openid profile email",
        },
        headers={"Authorization": _basic_auth_header(mcp_client_id, mcp_client_secret)},
    )
    if code != 200:
        # Two common causes: (a) password grant not enabled on the
        # orchestrator-mcp-client app; (b) demo password rotated. Surface
        # both with a clear remediation hint.
        warn(
            "sample token issuance",
            f"POST /oauth2/token returned {code}. Common causes: "
            "(1) Password grant not enabled on orchestrator-mcp-client — "
            "enable in IS Console → Applications → orchestrator-mcp-client → "
            "Protocol tab → Allowed Grant Types → check 'Password'. "
            "(2) DEMO_PASSWORD env var doesn't match the demo user's actual "
            f"password (default per wso2-is-setup.md is NewsMax@1234). "
            f"Body excerpt: {body[:160]}",
        )
        return
    try:
        access_token = json.loads(body).get("access_token") or ""
    except Exception:
        bad("sample token issuance", "invalid JSON from /token")
        return
    payload = b64url_decode_payload(access_token)
    if not payload:
        bad(
            "sample token format",
            "could not decode JWT payload (opaque token? — switch the app to JWT access tokens "
            "in IS Console → orchestrator-mcp-client → Protocol → Access Token Type = JWT)",
        )
        return
    missing = [c for c in REQUIRED_CLAIMS if c not in payload]
    if not missing:
        ok(f"user-bearing token for '{demo_username}' carries username + email claims")
    else:
        bad(
            "sample token claims",
            f"missing: {', '.join(missing)} — map them into the access-token claim set in "
            "IS Console → API Resources → (your resource) → Scopes / OIDC, ensure the "
            "OAuth app's requested scopes include 'profile email', and re-issue.",
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
    mcp_client_secret = _resolved("ORCHESTRATOR_MCP_CLIENT_SECRET", env_file)
    demo_username = os.environ.get("DEMO_USERNAME", "employee_user")
    demo_password = os.environ.get("DEMO_PASSWORD", "NewsMax@1234")

    expected_client_ids = {
        "orchestrator-mcp-client": mcp_client_id,
        "orchestrator-agent-oauth": _resolved("ORCHESTRATOR_AGENT_OAUTH_CLIENT_ID", env_file),
        "hr_agent OAuth App": _resolved("HR_AGENT_OAUTH_CLIENT_ID", env_file),
        "it_agent OAuth App": _resolved("IT_AGENT_OAUTH_CLIENT_ID", env_file),
    }
    expected_client_ids = {k: v for k, v in expected_client_ids.items() if v}

    auth_hdr = _basic_auth_header(admin_user, admin_pass)

    print(f"{_B}IS system-readiness audit{_R}  ({base_url})")

    check_connectivity(base_url)
    check_jwks(base_url)

    if not check_mgmt_api_auth(base_url, auth_hdr):
        # Skip downstream sections that depend on Mgmt API; still try token check.
        check_token_claims(
            base_url,
            mcp_client_id=mcp_client_id,
            mcp_client_secret=mcp_client_secret,
            demo_username=demo_username,
            demo_password=demo_password,
        )
        _summarise()
        return 1

    check_applications(base_url, auth_hdr, expected_client_ids)
    resources = check_api_resources(base_url, auth_hdr, hr_name, it_name)
    check_scopes(base_url, auth_hdr, resources, hr_name, it_name)
    check_users(base_url, auth_hdr)
    check_roles(base_url, auth_hdr)
    check_token_claims(
        base_url,
        mcp_client_id=mcp_client_id,
        mcp_client_secret=mcp_client_secret,
        demo_username=demo_username,
        demo_password=demo_password,
    )

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
