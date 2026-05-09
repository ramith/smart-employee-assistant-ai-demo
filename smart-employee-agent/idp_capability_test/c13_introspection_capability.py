#!/usr/bin/env python3
"""
C13 — Does WSO2 IS introspect CIBA-issued OBO tokens as inactive after the
parent (Pattern C) token-A is revoked?

This probe answers Stage 4 BLOCK-A in the Sprint 3 design review. The §5
error matrix in `docs/architecture/sprint-3-tech-arch.md` claims that a
missed fan-out leg (R-LOGOUT-7) or an orchestrator crash mid-cascade
(EX-4) is backstopped within ≤20 s by introspection. That claim depends
on revoking token-A causing token-B to introspect as `active=false`.

F-19 already showed that WSO2 IS 7.2 does NOT treat CIBA-issued grants as
session participants for OIDC Back-Channel Logout. C13 closes the parallel
question for introspection.

Verdict matrix:

  active=false (within ≤5 s of revoke)  → F-21 PASS — backstop story holds
  active=true  (after 5 s)              → F-21 FAIL — denylist is the only
                                           security boundary; SECURITY-DEGRADED
                                           label fires on half-fan-out + crash
                                           rows in §5.

────────────────────────────────────────────────────────────────────────────
Operator workflow (manual capture — automated end-to-end would require us to
re-implement Pattern C login here, which is needlessly complex for a probe):

  1. `make demo-up` — bring up the demo stack against live IS at
     `13.60.190.47:9443`.

  2. Sign in to the SPA as `employee_user` (or `hr_admin_user`). Issue at
     least one HR query and approve consent so HR-AGENT mints a token-B.

  3. Capture token-A. Easiest path: in the SPA, open browser DevTools →
     Network tab → click any chat request → check the request headers OR
     orchestrator response headers / cookies. Token-A is the access_token
     used by the orchestrator session. Alternative: look in
     `docker compose logs orchestrator` for a line containing
     `token_a.access_token=…` (Sprint 1+2 logging emits this on session
     establishment).

  4. Capture token-B. After triggering an HR query and approving consent,
     look in `docker compose logs hr-agent` for the line that emits the
     OBO token after a successful CIBA flow (typically formatted as
     `obo_token_minted access_token=eyJ…`). Or use a debug Python REPL:

       docker compose exec hr-agent python3
       >>> # Inspect the dispatcher's cache; structure depends on Sprint 2.1b layout.
       >>> # Look for an attribute named token_cache / _token_cache / _CachedToken
       >>> import hr_agent  # then dir() / vars() to find the singleton

     If neither path works, the simplest fallback is to add a one-shot
     `print(token.access_token)` near the cache write in
     `hr_agent/ciba/orchestrator.py` and rebuild — capture the token from
     logs on the next CIBA, then revert the print.

  5. Run this probe:

       cd idp_capability_test
       TOKEN_A="<paste>" TOKEN_B="<paste>" python3 c13_introspection_capability.py

────────────────────────────────────────────────────────────────────────────

Document the verdict in `docs/architecture/sprint-1-fixes.md` as F-21:
PASS or FAIL plus the introspection responses captured.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Helpers (same style as c11_role_denial.py) ───────────────────────────────

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


def _introspect(
    s: requests.Session,
    is_base: str,
    client_id: str,
    client_secret: str,
    token: str,
) -> dict:
    r = s.post(
        f"{is_base}/oauth2/introspect",
        auth=(client_id, client_secret),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"token": token},
    )
    if r.status_code != 200:
        fail(f"/oauth2/introspect HTTP {r.status_code}: {r.text[:200]}")
        return {}
    try:
        return r.json()
    except ValueError:
        fail(f"/oauth2/introspect non-JSON body: {r.text[:200]}")
        return {}


def _revoke(
    s: requests.Session,
    is_base: str,
    client_id: str,
    client_secret: str,
    token: str,
) -> tuple[int, str]:
    r = s.post(
        f"{is_base}/oauth2/revoke",
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"token": token},
    )
    return r.status_code, r.text[:200]


def _print_introspection(label: str, body: dict) -> None:
    if not body:
        warn(f"{label}: empty body")
        return
    active = body.get("active")
    info(f"{label}.active   = {active}")
    if "client_id" in body:
        info(f"{label}.client_id = {body['client_id']}")
    if "scope" in body:
        info(f"{label}.scope     = {body['scope']}")
    if "exp" in body:
        info(f"{label}.exp       = {body['exp']}")
    if "jti" in body:
        info(f"{label}.jti       = {body['jti']}")
    if "act" in body:
        info(f"{label}.act       = {body['act']}")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent

    # We need credentials to call /oauth2/introspect + /oauth2/revoke.
    # Use the orchestrator-mcp-client (the confidential Pattern C client) since
    # it is the one that revokes token-A in the locked design (see
    # docs/architecture/sprint-3-tech-arch.md §3.1).
    orch_env = _load_env_file(repo_root / "orchestrator" / ".env")
    is_base = orch_env.get("WSO2_IS_BASE_URL", "").rstrip("/")
    orch_client_id = orch_env.get("ORCHESTRATOR_MCP_CLIENT_ID", "")
    orch_client_secret = orch_env.get("ORCHESTRATOR_MCP_CLIENT_SECRET", "")

    # Fall back: HR Agent's confidential client can also call introspect.
    hr_env = _load_env_file(repo_root / "hr_agent" / ".env")
    hr_client_id = hr_env.get("HR_AGENT_OAUTH_CLIENT_ID", "")
    hr_client_secret = hr_env.get("HR_AGENT_OAUTH_CLIENT_SECRET", "")

    if not is_base:
        fail("WSO2_IS_BASE_URL not in orchestrator/.env.")
        return 2
    if not (orch_client_id and orch_client_secret):
        fail("ORCHESTRATOR_MCP_CLIENT_ID/SECRET missing — can't call /oauth2/revoke for token-A.")
        info("Check orchestrator/.env for the confidential-client credentials.")
        return 2
    if not (hr_client_id and hr_client_secret):
        warn("HR_AGENT_OAUTH_CLIENT_ID/SECRET missing — will use orchestrator client for both.")
        hr_client_id = orch_client_id
        hr_client_secret = orch_client_secret

    token_a = os.environ.get("TOKEN_A", "").strip()
    token_b = os.environ.get("TOKEN_B", "").strip()
    if not token_a or not token_b:
        fail("TOKEN_A and TOKEN_B env vars are required.")
        info("See module docstring for capture recipe (docker compose exec …).")
        return 2

    settle_s = float(os.environ.get("SETTLE_SECONDS", "5"))

    s = requests.Session()
    s.verify = False  # noqa: S501

    hr_("C13 — IS introspection of OBO tokens after parent token revoke")
    info(f"  IS base URL                  : {is_base}")
    info(f"  Revoking client (token-A)    : {orch_client_id}")
    info(f"  Introspecting client (token-B): {hr_client_id}")
    info(f"  Settle window after revoke   : {settle_s} s")
    info(f"  Token-A length               : {len(token_a)}")
    info(f"  Token-B length               : {len(token_b)}")

    # ── Step 1: sanity introspect token-A ─────────────────────────────────────
    hr_("C13.1 — Introspect token-A (sanity, expect active=true)")
    a_before = _introspect(s, is_base, orch_client_id, orch_client_secret, token_a)
    _print_introspection("token_a", a_before)
    if a_before.get("active") is not True:
        warn("token-A introspects as inactive ALREADY. Probe cannot proceed cleanly.")
        info("Re-capture a fresh token-A and rerun.")
        return 1

    # ── Step 2: sanity introspect token-B ─────────────────────────────────────
    hr_("C13.2 — Introspect token-B (sanity, expect active=true)")
    b_before = _introspect(s, is_base, hr_client_id, hr_client_secret, token_b)
    _print_introspection("token_b", b_before)
    if b_before.get("active") is not True:
        warn("token-B introspects as inactive ALREADY. Probe cannot proceed cleanly.")
        info("Re-capture a fresh token-B (after a successful CIBA flow) and rerun.")
        return 1

    # Optional sanity: verify the act.sub / aud / scope on token-B look OBO-shaped.
    if "act" in b_before:
        ok("token-B carries 'act' claim — looks like an OBO grant.")
    else:
        warn("token-B does NOT carry 'act' claim. Probe will continue but the verdict")
        warn("may not generalise to CIBA-issued OBO tokens.")

    # ── Step 3: revoke token-A ────────────────────────────────────────────────
    hr_("C13.3 — Revoke token-A via /oauth2/revoke")
    revoke_status, revoke_body = _revoke(s, is_base, orch_client_id, orch_client_secret, token_a)
    info(f"  HTTP status : {revoke_status}")
    info(f"  body        : {revoke_body or '(empty — typical for /oauth2/revoke 200)'}")
    if revoke_status not in (200, 204):
        fail("Revoke did not return 2xx; cannot determine F-21 cleanly.")
        info("Investigate. Common causes: client lacks revocation permission;")
        info("token already revoked; IS misconfig.")
        return 1
    ok("Revoke accepted.")

    # ── Step 4: confirm token-A introspects inactive (sanity) ────────────────
    hr_(f"C13.4 — Sleep {settle_s}s, then introspect token-A again (expect active=false)")
    time.sleep(settle_s)
    a_after = _introspect(s, is_base, orch_client_id, orch_client_secret, token_a)
    _print_introspection("token_a_after", a_after)
    if a_after.get("active") is True:
        warn("token-A still active after revoke. IS-side revoke propagation is slow")
        warn("or broken. Consider increasing SETTLE_SECONDS and retrying.")

    # ── Step 5: the question — does token-B introspect as inactive? ──────────
    hr_("C13.5 — Introspect token-B AFTER revoke of token-A (the question)")
    b_after = _introspect(s, is_base, hr_client_id, hr_client_secret, token_b)
    _print_introspection("token_b_after", b_after)

    hr_("C13 verdict")
    b_after_active = b_after.get("active")
    if b_after_active is False:
        ok("token-B introspects as INACTIVE after revoking token-A.")
        info("  → F-21 PASS.")
        info("  Sprint 3 backstop story holds: half-fan-out + orchestrator-crash")
        info("  scenarios are recovered within the introspection cache TTL.")
        info("  No design changes required. Document outcome in")
        info("  docs/architecture/sprint-1-fixes.md §F-21.")
        return 0
    if b_after_active is True:
        fail("token-B STILL ACTIVE after revoking token-A.")
        info("  → F-21 FAIL.")
        info("")
        info("  Implication: WSO2 IS 7.2 does NOT propagate token-A revocation to")
        info("  CIBA-issued OBO tokens. Introspection cannot be relied on as a")
        info("  backstop for missed fan-out or orchestrator crashes. Captured")
        info("  token-B remains valid until natural TTL (1h default).")
        info("")
        info("  Required follow-ups (per Stage 4 §5 SECURITY-DEGRADED labelling):")
        info("    1. Patch tech-arch §5 rows 1, 2, 3 to mark them SECURITY-DEGRADED.")
        info("    2. Add operator-action note in docs/demo-runbook.md: on partial")
        info("       fan-out (logout_fanout_partial WARN), restart affected receivers")
        info("       within demo window.")
        info("    3. R-LOGOUT-7b acceptance: assert SECURITY_DEGRADED label is")
        info("       emitted on all-legs-failure (no behavioural change to fan-out).")
        info("    4. Document the verdict in docs/architecture/sprint-1-fixes.md §F-21.")
        info("")
        info("  Demo path is still sound — denylist is the primary security boundary.")
        info("  Sprint 3 ships per Stage 5 L-2 lock (with SECURITY-DEGRADED labels).")
        return 0
    warn(f"token-B introspect returned active={b_after_active!r} — not the expected bool.")
    info("Investigate the introspection response shape before concluding.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
