#!/usr/bin/env python3
"""
BCL listener — captures OIDC Back-Channel Logout POSTs from WSO2 IS during
the C12 capability spike. Runs inside the docker-compose stack under the
``spike-bcl`` profile. The container is bound to ``127.0.0.1:8123`` on the
host; a reverse SSH tunnel from the laptop forwards the AWS VM's
``localhost:8123`` to this listener so WSO2 IS (running on the AWS VM) can
reach it without a public endpoint.

The listener:
  * accepts ``POST /bcl`` (or any path) with body
    ``application/x-www-form-urlencoded`` containing ``logout_token=<JWT>``
  * decodes the JWT header + payload (no signature verification — this is a
    capture tool, not an enforcer)
  * prints the decoded record to stdout AND appends it to
    ``/data/bcl_received.log`` (volume-mounted to ``./tools/_bcl_log/``)
  * always responds ``200 OK`` so IS doesn't retry / log failures

Operator workflow (full walkthrough in docs/spikes/c12-bcl-spike-setup.md)::

    ./scripts/spike-bcl-prep-mac.sh   # one-time
    ./scripts/spike-bcl-up.sh          # start listener + autossh tunnel
    docker compose --profile spike-bcl logs -f bcl-listener
    cat tools/_bcl_log/bcl_received.log
    ./scripts/spike-bcl-down.sh        # tear down
"""
from __future__ import annotations

import base64
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

LOG_DIR = "/data"
LOG_FILE = f"{LOG_DIR}/bcl_received.log"
LISTEN_PORT = int(os.environ.get("BCL_LISTEN_PORT", "8123"))


def _b64url_padded_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _try_decode_jwt(token: str) -> tuple[dict | None, dict | None]:
    parts = token.split(".")
    if len(parts) < 2:
        return None, None
    try:
        header = json.loads(_b64url_padded_decode(parts[0]).decode())
    except Exception:
        header = None
    try:
        payload = json.loads(_b64url_padded_decode(parts[1]).decode())
    except Exception:
        payload = None
    return header, payload


class BCLHandler(BaseHTTPRequestHandler):
    def _do_anything(self, method: str) -> None:
        n = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(n).decode("utf-8", errors="replace") if n else ""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Parse application/x-www-form-urlencoded → extract logout_token
        params = parse_qs(body, keep_blank_values=True)
        logout_token = (params.get("logout_token") or [None])[0]
        decoded_header: dict | None = None
        decoded_payload: dict | None = None
        if logout_token and logout_token.count(".") >= 2:
            decoded_header, decoded_payload = _try_decode_jwt(logout_token)

        record = {
            "ts": ts,
            "method": method,
            "path": self.path,
            "client": f"{self.client_address[0]}:{self.client_address[1]}",
            "headers": {k: v for k, v in self.headers.items()},
            "body_raw_len": len(body),
            "body_preview": body[:500],
            "logout_token_present": bool(logout_token),
            "logout_token_header": decoded_header,
            "logout_token_payload": decoded_payload,
        }
        line = json.dumps(record, indent=2, default=str)
        print(f"\n=== {method} {self.path} (BCL listener) ===\n{line}\n=== end ===", flush=True)

        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n---\n")
        except OSError as exc:
            print(f"  [warn] could not append to {LOG_FILE}: {exc}", flush=True)

        # Always 200 — never retry, never fail.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"ok\n")

    def do_POST(self) -> None:
        self._do_anything("POST")

    def do_GET(self) -> None:
        # Useful for tunnel smoke-tests and manual sanity hits.
        if self.path in ("/", "/healthz", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"BCL listener up\n")
            return
        self._do_anything("GET")

    def log_message(self, *_args, **_kwargs) -> None:
        # Silence the default per-request access-log line; we emit our own.
        pass


def main() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    print(f"[bcl_listener] listening on 0.0.0.0:{LISTEN_PORT}", flush=True)
    print(f"[bcl_listener] logging captures to {LOG_FILE}", flush=True)
    HTTPServer(("0.0.0.0", LISTEN_PORT), BCLHandler).serve_forever()


if __name__ == "__main__":
    main()
