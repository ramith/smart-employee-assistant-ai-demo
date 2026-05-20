#!/usr/bin/env python3
"""
C0 — IdP reachability + JWKS validity + substrate creds work.

Pass criteria:
  - JWKS endpoint returns at least one key.
  - Token endpoint responds (any HTTP — even 400/401 proves it's listening).
  - probe-client-a can mint a client_credentials token (proves the app
    exists, the secret matches, and client_credentials grant is enabled).
"""

from __future__ import annotations

import sys

from _lib import Config, IdpClient, fail, hr, info, ok


def main() -> int:
    cfg = Config.load()
    client = IdpClient(cfg)

    hr(f"C0 — Reachability: {cfg.base_url}")

    # 1) JWKS
    hr("C0.1 — JWKS endpoint reachable + valid")
    try:
        jwks = client.jwks()
    except Exception as e:  # noqa: BLE001
        fail(f"Could not reach JWKS endpoint: {e}")
        info(f"  URL: {cfg.jwks_url}")
        info("  Is WSO2 IS actually running on this host/port?")
        return 1

    keys = jwks.get("keys", [])
    if not keys:
        fail(f"JWKS returned no keys: {jwks}")
        return 1
    ok(f"JWKS returned {len(keys)} key(s); first kid={keys[0].get('kid', '?')}")

    # 2) Token endpoint responds
    hr("C0.2 — Token endpoint responding to bad credentials")
    status, body = client.reachable(cfg.token_url)
    if status in (400, 401):
        ok(f"Token endpoint returned HTTP {status} (expected for bad creds)")
    elif status == 0:
        fail(f"Token endpoint not reachable: {body}")
        return 1
    else:
        info(f"  Unexpected HTTP {status} — but endpoint responded. Body: {body[:200]}")
        ok("Token endpoint is reachable")

    # 3) Substrate creds work
    hr("C0.3 — probe-client-a credentials valid (client_credentials grant)")
    resp = client.client_credentials(cfg.probe_client_a_id, cfg.probe_client_a_secret)
    token = resp.get("access_token")
    if not token:
        fail(f"client_credentials grant failed: {resp.get('error')}")
        info("  Check:")
        info("  - probe-client-a exists in WSO2 IS Console → Applications")
        info("  - PROBE_CLIENT_A_ID / PROBE_CLIENT_A_SECRET in .substrate.env are correct")
        info("  - 'Client Credentials' grant is enabled on the app's Protocol tab")
        return 1
    ok(f"probe-client-a got a token (first 40 chars): {token[:40]}...")

    hr("C0 PASS — IdP foundation looks healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
