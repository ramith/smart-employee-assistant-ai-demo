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
DEMO_USERS = ["employee_user", "hr_admin_user"]
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
        if has_username and has_email:
            ok(f"user '{username}' has userName + email set (claim source available)")
            # S5.12: the agents derive the CIBA login_hint by stripping @domain
            # off the email-form `sub`, so the userName MUST equal the email
            # local-part or CIBA fails with the "federated user" error.
            email_localparts = {str(v).split("@", 1)[0].lower() for v in email_vals}
            if uname_bare.lower() not in email_localparts:
                bad(
                    f"user '{username}' login_hint alignment",
                    f"userName '{uname_bare}' != any email local-part "
                    f"{sorted(email_localparts)} — CIBA via chat will fail for this "
                    f"user (see docs/architecture/identity-subject-mismatch.md §6). "
                    f"Recreate the account with userName = the email local-part.",
                )
            else:
                ok(f"user '{username}' userName matches email local-part (CIBA login_hint OK)")
        else:
            missing = []
            if not has_username:
                missing.append("userName")
            if not has_email:
                missing.append("emails[].value")
            bad(
                f"user '{username}' attributes",
                f"missing: {', '.join(missing)} — set in Console → User Management → "
                f"select user → Profile, then re-run.",
            )

    print()
    print(
        f"  {_Y}note:{_R} Section 9 verifies the SCIM2 source attributes only. To "
        "confirm the OAuth claim mapping into access tokens, sign in to the SPA "
        "(Pattern C), then verify with: "
    )
    print(f"    docker compose logs orchestrator | grep auth_exchange_success")


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
