#!/usr/bin/env python3
"""scripts/synth-bcl.py — mint and POST a synthesized OIDC logout_token.

The new WSO2 IS 7.x Console doesn't expose the per-app
``back_channel_logout_uri`` field, so we can't ask IS to fire BCL on
admin-terminate. This script lets the operator simulate the IS-fired
event end-to-end:

  1. Read IS's RS256 signing key from a PEM file.
  2. Fetch IS's JWKS to find the matching ``kid``.
  3. Mint a ``logout_token`` with the right shape (iss/aud/iat/jti/
     events/typ=logout+jwt and either sub or sid).
  4. POST it as ``application/x-www-form-urlencoded`` to the orchestrator's
     ``/backchannel-logout`` endpoint.

This is a manual-verify aid only. It is NOT a production substitute
for IS firing the event itself; it just proves our receiver and cascade
work for an audience.

How to extract the IS signing key from the AWS VM keystore::

    # On the IS VM (default keystore path + creds; adjust if customised):
    cd /opt/wso2is-7.3.0/repository/resources/security
    keytool -importkeystore \\
        -srckeystore wso2carbon.jks -srcstorepass wso2carbon \\
        -destkeystore wso2carbon.p12 -deststoretype PKCS12 \\
        -deststorepass wso2carbon -srcalias wso2carbon \\
        -destkeypass wso2carbon
    openssl pkcs12 -in wso2carbon.p12 -nodes -nocerts \\
        -out /tmp/wso2_private.pem -passin pass:wso2carbon

    # scp /tmp/wso2_private.pem to your laptop, then:

    python3 scripts/synth-bcl.py \\
        --key wso2_private.pem \\
        --sub <user-uuid-from-token-b> \\
        --orchestrator http://localhost:8090

Defaults match the POC config (issuer, aud, orchestrator URL); override
via flags for any environment.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import jwt as pyjwt

DEFAULT_ISSUER = "https://13.60.190.47:9443/oauth2/token"
DEFAULT_JWKS_URL = "https://13.60.190.47:9443/oauth2/jwks"
DEFAULT_AUD = "Ry9Wx_Q7w2FSi27miUpYr3O0xR4a"  # orchestrator-mcp-client
DEFAULT_ORCH = "http://localhost:8090"
BCL_EVENTS_URI = "http://schemas.openid.net/event/backchannel-logout"


def fetch_kid(jwks_url: str, *, insecure: bool = True) -> str:
    """Return the first signing-key kid in IS's JWKS."""
    import ssl  # noqa: PLC0415
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(jwks_url, context=ctx, timeout=10) as resp:
        doc = json.load(resp)
    keys = doc.get("keys") or []
    if not keys:
        raise RuntimeError(f"JWKS at {jwks_url} returned no keys")
    sig_keys = [k for k in keys if k.get("use", "sig") == "sig" and k.get("alg", "RS256") == "RS256"]
    chosen = sig_keys[0] if sig_keys else keys[0]
    kid = chosen.get("kid")
    if not kid:
        raise RuntimeError("JWKS key missing 'kid'")
    return kid


def build_logout_token(
    *,
    private_key_pem: str,
    issuer: str,
    audience: str,
    kid: str,
    sub: str | None,
    sid: str | None,
) -> str:
    """Mint a logout_token with the BLOCK-C 9-check shape."""
    if sub is None and sid is None:
        raise ValueError("either --sub or --sid is required")
    payload: dict = {
        "iss": issuer,
        "aud": audience,
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
        "events": {BCL_EVENTS_URI: {}},
    }
    if sub is not None:
        payload["sub"] = sub
    if sid is not None:
        payload["sid"] = sid
    return pyjwt.encode(
        payload,
        private_key_pem,
        algorithm="RS256",
        headers={"typ": "logout+jwt", "kid": kid},
    )


def post_logout_token(orchestrator_url: str, logout_token: str, rid: str | None) -> tuple[int, str]:
    """POST the form-encoded body to /backchannel-logout. Return (status, body)."""
    body = urllib.parse.urlencode({"logout_token": logout_token}).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if rid:
        headers["X-Request-ID"] = rid
    req = urllib.request.Request(
        f"{orchestrator_url.rstrip('/')}/backchannel-logout",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Mint + POST a synthesized OIDC logout_token to the orchestrator.",
    )
    p.add_argument("--key", required=True, help="Path to IS RS256 private key (PEM)")
    p.add_argument("--sub", help="user_sub to terminate (preferred)")
    p.add_argument("--sid", help="OIDC sid to terminate (alternative to --sub)")
    p.add_argument("--issuer", default=DEFAULT_ISSUER, help=f"iss claim (default: {DEFAULT_ISSUER})")
    p.add_argument("--aud", default=DEFAULT_AUD, help=f"aud claim (default: {DEFAULT_AUD})")
    p.add_argument("--jwks-url", default=DEFAULT_JWKS_URL, help=f"IS JWKS URL (default: {DEFAULT_JWKS_URL})")
    p.add_argument("--kid", help="Override kid; default fetches from JWKS")
    p.add_argument("--orchestrator", default=DEFAULT_ORCH, help=f"Orchestrator base URL (default: {DEFAULT_ORCH})")
    p.add_argument("--rid", help="X-Request-ID to send (for grep-trace.sh)")
    p.add_argument("--print-only", action="store_true", help="Mint but do not POST")
    args = p.parse_args(argv)

    key_path = Path(args.key)
    if not key_path.is_file():
        print(f"key file not found: {key_path}", file=sys.stderr)
        return 2
    private_key_pem = key_path.read_text()

    if args.kid:
        kid = args.kid
    else:
        try:
            kid = fetch_kid(args.jwks_url, insecure=True)
        except Exception as exc:  # noqa: BLE001
            print(f"failed to fetch kid from {args.jwks_url}: {exc}", file=sys.stderr)
            return 2

    try:
        token = build_logout_token(
            private_key_pem=private_key_pem,
            issuer=args.issuer,
            audience=args.aud,
            kid=kid,
            sub=args.sub,
            sid=args.sid,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"failed to mint logout_token: {exc}", file=sys.stderr)
        return 2

    print(f"# logout_token (kid={kid}, sub={args.sub or '-'}, sid={args.sid or '-'}):")
    print(token)
    print()

    if args.print_only:
        return 0

    rid = args.rid or f"bcl-synth-{int(time.time())}"
    print(f"# POST {args.orchestrator}/backchannel-logout (X-Request-ID: {rid})")
    status, body = post_logout_token(args.orchestrator, token, rid)
    print(f"HTTP {status}")
    print(body)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
