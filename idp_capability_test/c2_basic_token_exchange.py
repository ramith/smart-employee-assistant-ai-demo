#!/usr/bin/env python3
"""
C2 — Basic RFC 8693 token-exchange (depth-1 act chain).

This is the headline test that was BLOCKED on Asgardeo SaaS with
`unauthorized_client`. If it passes here, the architecture works.

Approach:
  1. Mint user_token via Resource Owner Password grant
     (probe-client-a + probe.user) → carries sub=<probe.user>.
  2. Mint actor_token via client_credentials grant (probe-client-a) →
     represents the agent identity (sub=<probe-client-a azp>).
  3. POST /oauth2/token with grant_type=urn:ietf:params:oauth:grant-type:token-exchange,
     subject_token=user_token, actor_token=actor_token.
  4. Decode the result. Expect sub=<probe.user>, act.sub=<probe-client-a>.

Pass: response has access_token; decoded JWT has both `sub` and `act.sub`.
Fail: any /token call errors, OR result has no act claim.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _lib import Config, IdpClient, decode_jwt, fail, hr, info, ok, show_jwt, warn


C2_TOKEN_PATH = Path("/tmp/c2_exchanged_token.txt")


def diagnose_exchange_error(error: str) -> None:
    """Print a targeted hint based on the OAuth error code."""
    info("")
    if error == "unauthorized_client":
        info("→ The Token Exchange grant is NOT enabled on probe-client-a.")
        info("  Console → Applications → probe-client-a → Protocol tab →")
        info("  ensure `urn:ietf:params:oauth:grant-type:token-exchange` is checked.")
        info("  If the checkbox is missing or greyed-out, this is the SAME blocker")
        info("  as Asgardeo SaaS — escalate to WSO2 expert before continuing.")
    elif error == "invalid_request":
        info("→ Request format issue. Check that subject_token, actor_token,")
        info("  resource, and scope are all present and well-formed.")
    elif error in {"invalid_grant", "invalid_token"}:
        info("→ subject_token or actor_token rejected.")
        info("  Possible causes:")
        info("  - Token already expired (re-run; tokens default to 3600s)")
        info("  - WSO2 IS requires a Trusted Token Issuer (Identity Provider)")
        info("    even for same-tenant exchange. Try registering a self-trust IDP")
        info("    pointing at https://13.60.190.47:9443/oauth2/token.")
    elif error == "invalid_scope":
        info("→ Requested scopes not granted to probe-client-a's API subscription.")
        info("  Console → Applications → probe-client-a → API Authorization")
    else:
        info(f"→ Unexpected error '{error}' — read the full response body above.")


def main() -> int:
    cfg = Config.load()
    client = IdpClient(cfg)

    hr("C2 — Basic RFC 8693 token-exchange (depth-1 act)")

    # ─── 1) user_token (password grant) ─────────────────────────────────
    hr("C2.1 — Mint user_token via password grant")
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
        info("  Check:")
        info("  - 'Password' grant is enabled on probe-client-a's Protocol tab")
        info("  - probe.user exists with the password set in .substrate.env")
        return 1
    user_payload = decode_jwt(user_token)
    ok(f"user_token minted; sub={user_payload.get('sub')}")

    # ─── 2) actor_token (client_credentials) ────────────────────────────
    hr("C2.2 — Mint actor_token via client_credentials")
    actor_resp = client.client_credentials(cfg.probe_client_a_id, cfg.probe_client_a_secret)
    actor_token = actor_resp.get("access_token")
    if not actor_token:
        fail(f"client_credentials grant failed: {actor_resp.get('error')}")
        return 1
    actor_payload = decode_jwt(actor_token)
    ok(f"actor_token minted; sub={actor_payload.get('sub')}")

    # ─── 3) RFC 8693 exchange ───────────────────────────────────────────
    hr("C2.3 — POST /oauth2/token with grant_type=token-exchange")
    exchange_resp = client.token_exchange(
        client_id=cfg.probe_client_a_id,
        client_secret=cfg.probe_client_a_secret,
        subject_token=user_token,
        actor_token=actor_token,
        resource=cfg.probe_api_audience,
        scope=cfg.probe_api_scopes,
    )
    exchanged = exchange_resp.get("access_token")
    if not exchanged:
        error = exchange_resp.get("error", "unknown")
        fail(f"Token exchange did NOT return an access_token (error={error})")
        diagnose_exchange_error(error)
        return 1

    C2_TOKEN_PATH.write_text(exchanged)
    ok(f"exchanged token saved to {C2_TOKEN_PATH} (for C3 to chain)")

    # ─── 4) Validate claims ─────────────────────────────────────────────
    payload = show_jwt(exchanged, "C2.4 — Decoded exchanged token")

    hr("C2 verdict")
    sub = payload.get("sub")
    act = payload.get("act")
    aud = payload.get("aud")

    if not act:
        fail("act claim is ABSENT in the exchanged token")
        info("→ WSO2 IS issued a token but didn't capture delegation.")
        info("  This is an architectural blocker — escalate.")
        return 1

    act_sub = act.get("sub") if isinstance(act, dict) else None
    if not act_sub:
        fail(f"act exists but act.sub is empty/malformed: {act}")
        return 1

    ok(f"sub={sub}")
    ok(f"act.sub={act_sub}")
    if act_sub == cfg.probe_client_a_id:
        ok("act.sub equals probe-client-a's client_id — depth-1 chain confirmed")
    else:
        warn(f"act.sub does not match probe-client-a's client_id ({cfg.probe_client_a_id})")
        info("  This could be an internal user UUID for the client; not a failure, but worth noting.")
    info(f"aud={aud}")

    hr("C2 PASS — RFC 8693 produces depth-1 act chain on this WSO2 IS install")
    return 0


if __name__ == "__main__":
    sys.exit(main())
