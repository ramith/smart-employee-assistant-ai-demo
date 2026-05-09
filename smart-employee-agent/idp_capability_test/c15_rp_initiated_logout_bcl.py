#!/usr/bin/env python3
"""
C15 — RP-Initiated Logout with id_token_hint: does WSO2 IS fan out
Back-Channel Logout to session-participant SPs?

This re-runs F-19 with the OIDC-spec-compliant /oidc/logout shape (the
original spike used a bare /oidc/logout?post_logout_redirect_uri=… with
NO id_token_hint, which produced ServiceProviderName=null in the audit
log — see docs/spikes/sprint-3-is-audit-log-analysis.md §1).

Audit-log evidence shows CIBA flows update the user's IS-side session
(StoreSession + UpdateSession on the same sessionContextId across
orchestrator-mcp-client and hr-agent SPs). The remaining question is:
when /oidc/logout is called with id_token_hint pointing at the
orchestrator-mcp-client's id_token, does IS walk the participants and
fire BCL to the registered backchannel_logout_uri on each?

Verdict matrix:

  BCL listener captures POST(s) for orchestrator-mcp-client AND
    agent apps (hr-agent / it-agent)
                                    → F-19 was a probe artifact.
                                      Option C (hybrid) becomes viable.
                                      3B.1 can lean on IS-driven BCL as
                                      defense-in-depth.

  BCL listener captures POST(s) ONLY for orchestrator-mcp-client (auth_code SP)
                                    → IS fires BCL to auth_code participants
                                      but NOT to CIBA-only grants. Option A
                                      stands; 3B.1 admin-terminate works on
                                      the orchestrator path. F-19 partially
                                      stands.

  BCL listener captures NOTHING
                                    → F-19 fully stands. Probe artifact
                                      hypothesis falsified. Current design
                                      unchanged.

────────────────────────────────────────────────────────────────────────────
Operator workflow:

  1. Bring up the C12 BCL listener rig:
       ./scripts/spike-bcl-up.sh

  2. In IS Console, confirm `backchannel_logout_uri` is set on:
       - orchestrator-mcp-client → http://localhost:8123/bcl
       - HR-AGENT-... (already set per F-19 setup; harmless)
       - IT-AGENT-... (already set per F-19 setup; harmless)

  3. Sign in to the SPA as employee_user. Trigger an HR query and
     approve consent (so HR-AGENT has a CIBA grant in the user session
     that IS could fan BCL to).

  4. Capture the orchestrator-mcp-client's id_token. Recipe — same as
     C13 capture, but reach for `result.token_a.id_token` (NOT the
     access_token):

       # in orchestrator/auth/routes.py near session create:
       print(f"C15_PROBE_DEBUG_ID_TOKEN {result.token_a.id_token}", flush=True)

     Rebuild orchestrator, re-login, capture from logs:

       docker compose logs orchestrator | grep C15_PROBE_DEBUG_ID_TOKEN | tail -1

  5. Run this probe with the captured id_token:

       cd idp_capability_test
       ID_TOKEN=<paste> python3 c15_rp_initiated_logout_bcl.py

  6. Re-check the BCL listener log:

       cat tools/_bcl_log/bcl_received.log

  7. Tear down: ./scripts/spike-bcl-down.sh

────────────────────────────────────────────────────────────────────────────

Document the verdict in `docs/architecture/sprint-1-fixes.md` as F-19
addendum (PASS/PARTIAL/FAIL retest).
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


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    orch_env = _load_env_file(repo_root / "orchestrator" / ".env")

    is_base = orch_env.get("WSO2_IS_BASE_URL", "").rstrip("/")
    orch_client_id = orch_env.get("ORCHESTRATOR_MCP_CLIENT_ID", "")

    if not is_base:
        fail("WSO2_IS_BASE_URL missing in orchestrator/.env.")
        return 2
    if not orch_client_id:
        fail("ORCHESTRATOR_MCP_CLIENT_ID missing in orchestrator/.env.")
        return 2

    id_token = os.environ.get("ID_TOKEN", "").strip()
    if not id_token:
        fail("ID_TOKEN env var not set.")
        info("Capture the orchestrator-mcp-client id_token via temporary debug print")
        info("in orchestrator/auth/routes.py — see module docstring.")
        return 2

    bcl_log_path = repo_root / "tools" / "_bcl_log" / "bcl_received.log"
    pre_count = 0
    if bcl_log_path.exists():
        pre_count = sum(1 for _ in bcl_log_path.open("r"))

    s = requests.Session()
    s.verify = False  # noqa: S501

    hr_("C15 — RP-Initiated Logout with id_token_hint, watch BCL listener")
    info(f"  IS base URL                : {is_base}")
    info(f"  Logout client_id (orch-MCP): {orch_client_id}")
    info(f"  id_token len               : {len(id_token)}")
    info(f"  BCL log pre-count          : {pre_count} lines at {bcl_log_path}")

    post_logout = "http://localhost:8090/?reason=signed_out"
    logout_url = (
        f"{is_base}/oidc/logout"
        f"?id_token_hint={id_token}"
        f"&post_logout_redirect_uri={post_logout}"
        f"&client_id={orch_client_id}"
    )

    hr_("C15.1 — GET /oidc/logout?id_token_hint=…&client_id=…")
    info(f"  URL : {logout_url[:120]}…")
    # Don't follow redirects — IS may render a confirmation page that
    # requires a click. We just want IS to receive the request and
    # decide whether to fire BCL.
    r = s.get(logout_url, allow_redirects=False)
    info(f"  HTTP status : {r.status_code}")
    info(f"  Location    : {r.headers.get('Location', '(no redirect)')[:120]}")
    body_preview = r.text[:300].replace("\n", " ").replace("\r", " ")
    info(f"  body preview: {body_preview}")
    if r.status_code in (200, 302, 303):
        ok("IS accepted the logout request (a 200 may be the consent screen).")
    else:
        warn(f"Unexpected HTTP {r.status_code}; investigate.")

    # IS may need a moment to fan out BCL POSTs to the listener.
    settle_s = float(os.environ.get("BCL_SETTLE_SECONDS", "8"))
    hr_(f"C15.2 — Wait {settle_s}s for IS to fire BCL")
    time.sleep(settle_s)

    # ── Inspect BCL listener log ──────────────────────────────────────────────
    hr_("C15.3 — Read the BCL listener log")
    if not bcl_log_path.exists():
        warn(f"BCL log file does not exist at {bcl_log_path}.")
        info("  Did the C12 spike rig come up clean? Run ./scripts/spike-bcl-up.sh first.")
        return 1

    lines = bcl_log_path.read_text().splitlines()
    new_lines = lines[pre_count:]
    info(f"  Total lines now : {len(lines)}")
    info(f"  New since start : {len(new_lines)}")
    for ln in new_lines:
        info(f"    {ln[:220]}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    hr_("C15 verdict")
    if not new_lines:
        fail("BCL listener captured NOTHING.")
        info("  → F-19 stands. /oidc/logout with id_token_hint still does not")
        info("    trigger BCL fan-out for any registered SP. Document as F-19")
        info("    addendum (probe-artifact hypothesis falsified).")
        info("  Current design (Option A orchestrator-driven) unchanged.")
        return 0

    # Try to identify which audiences received POSTs by parsing the listener log
    saw_orch = any(orch_client_id in ln for ln in new_lines)
    saw_hr = any("HR-AGENT" in ln or "hr-agent" in ln for ln in new_lines)
    saw_it = any("IT-AGENT" in ln or "it-agent" in ln for ln in new_lines)

    info(f"  saw orchestrator-mcp-client : {saw_orch}")
    info(f"  saw hr-agent app            : {saw_hr}")
    info(f"  saw it-agent app            : {saw_it}")

    if saw_hr or saw_it:
        ok("BCL fan-out reached agent apps (CIBA grants ARE session participants).")
        info("  → F-19 PROBE ARTIFACT confirmed. Option C (hybrid) is viable.")
        info("  3B.1 admin-terminate can leverage IS-driven BCL as defense-in-depth.")
        info("  Update Stage 4 + tech-arch §5: introspection-backstop story can be")
        info("  partially restored for D3.2 paths (NOT user-driven /oauth2/revoke,")
        info("  which is still F-21 FAIL).")
        return 0

    if saw_orch:
        warn("BCL fan-out reached orchestrator-mcp-client only (auth_code SP).")
        info("  → F-19 PARTIAL stand: IS fires BCL for auth_code grants but")
        info("    NOT for CIBA-only agent grants. Architecturally same as F-19.")
        info("  Sprint 3 design unchanged. Demo narrative tightened.")
        return 0

    warn("BCL captured but couldn't identify the audience from the listener log.")
    info("  Manually inspect tools/_bcl_log/bcl_received.log entries above.")
    info("  → INDETERMINATE; manual classification required.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
