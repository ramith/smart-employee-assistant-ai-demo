#!/usr/bin/env python3
"""
C5 — RFC 8707 Resource Indicators (multi-audience token).

Mints a token with TWO `resource` parameters and checks whether the
resulting `aud` claim is an array containing both — or whether WSO2 IS
collapses to a single audience.

This is what the "Audit with delegation chains" slide implied: the
exchanged token's aud was a single MCP server URL, achieved via RFC 8707.
We want to know if multiple audiences in one token are possible.

Approach:
  1. Mint a user_token via password grant.
  2. Token-exchange with TWO `resource` params and the API scopes.
  3. Decode result; inspect `aud` shape.

Pass: aud is a list with at least both requested values.
Acceptable: aud is a single string with one of them (IdP picks the first).
Fail: token exchange errors, or aud is unrelated.
"""

from __future__ import annotations

import sys

from _lib import Config, IdpClient, fail, hr, info, ok, show_jwt, warn


def main() -> int:
    cfg = Config.load()
    client = IdpClient(cfg)

    hr("C5 — RFC 8707 multi-resource token-exchange")

    # ─── 1) user_token (password grant) ─────────────────────────────────
    hr("C5.1 — Mint user_token")
    user_resp = client.password_grant(
        client_id=cfg.probe_client_a_id,
        client_secret=cfg.probe_client_a_secret,
        username=cfg.probe_user_username,
        password=cfg.probe_user_password,
        scope=f"openid {cfg.probe_api_scopes}",
    )
    user_token = user_resp.get("access_token")
    if not user_token:
        fail(f"Password grant failed: {user_resp.get('error')}")
        return 1
    ok("user_token minted")

    # ─── 2) actor_token (client_credentials) ────────────────────────────
    hr("C5.2 — Mint actor_token")
    actor_resp = client.client_credentials(cfg.probe_client_a_id, cfg.probe_client_a_secret)
    actor_token = actor_resp.get("access_token")
    if not actor_token:
        fail(f"client_credentials grant failed: {actor_resp.get('error')}")
        return 1
    ok("actor_token minted")

    # ─── 3) Multi-resource token exchange ───────────────────────────────
    hr("C5.3 — POST /oauth2/token with TWO resource= parameters")
    resources = [cfg.probe_api_audience, cfg.probe_api_secondary_audience]
    info(f"  resources requested: {resources}")
    exchange_resp = client.token_exchange(
        client_id=cfg.probe_client_a_id,
        client_secret=cfg.probe_client_a_secret,
        subject_token=user_token,
        actor_token=actor_token,
        resource=resources,
        scope=cfg.probe_api_scopes,
    )
    exchanged = exchange_resp.get("access_token")
    if not exchanged:
        fail(f"Token exchange failed: {exchange_resp.get('error')}")
        info("  WSO2 IS may reject multiple resource params — note this and proceed.")
        return 1

    payload = show_jwt(exchanged, "C5.4 — Decoded exchanged token")

    # ─── 4) Inspect aud ─────────────────────────────────────────────────
    hr("C5 verdict")
    aud = payload.get("aud")
    info(f"aud type: {type(aud).__name__}")
    info(f"aud value: {aud}")

    if isinstance(aud, list):
        present = [r for r in resources if r in aud]
        if len(present) >= 2:
            ok(f"aud is an array containing both requested resources: {present}")
            hr("C5 PASS — multi-audience tokens are supported")
            return 0
        if len(present) == 1:
            warn(f"aud is an array but only contains {present[0]} of {resources}")
            info("  IdP honored only the first resource. Not a clean multi-audience.")
            return 1
        fail(f"aud is an array but contains neither requested resource: {aud}")
        return 1

    if isinstance(aud, str):
        if aud in resources:
            warn(f"aud is a single string ({aud}) — IdP collapsed to one resource")
            info("  Multi-audience NOT supported. Architectural implication: would need")
            info("  one token per audience (Pattern C per resource).")
            return 1
        fail(f"aud is a single string '{aud}' — not one of the requested resources")
        return 1

    fail(f"aud claim has unexpected shape: {aud}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
