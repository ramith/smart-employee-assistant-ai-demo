"""
Shared helpers for IdP capability tests against WSO2 IS 7.2.0.

Imported by every cN_*.py. Loads .substrate.env, exposes a thin OAuth client
with verbose logging, and provides JWT inspection helpers.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import urllib3

HERE = Path(__file__).resolve().parent

# ─── Colored output (no deps) ─────────────────────────────────────────────
_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"


def hr(msg: str) -> None:
    print(f"\n{_BOLD}{_CYAN}── {msg} ──{_RESET}")


def ok(msg: str) -> None:
    print(f"{_BOLD}{_GREEN}✓ PASS{_RESET}  {msg}")


def fail(msg: str) -> None:
    print(f"{_BOLD}{_RED}✗ FAIL{_RESET}  {msg}")


def warn(msg: str) -> None:
    print(f"{_BOLD}{_YELLOW}! WARN{_RESET}  {msg}")


def info(msg: str) -> None:
    print(f"  {msg}")


def dim(msg: str) -> None:
    print(f"{_DIM}{msg}{_RESET}")


# ─── .substrate.env loader ────────────────────────────────────────────────
def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        fail(f"{path} not found")
        info("Copy `.substrate.env.example` → `.substrate.env` and fill in values.")
        sys.exit(2)
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        # strip optional quotes
        v = v.strip().strip("'").strip('"')
        env[k.strip()] = v
    return env


@dataclass(frozen=True)
class Config:
    base_url: str
    token_url: str
    authz_url: str
    authn_url: str
    jwks_url: str
    introspect_url: str
    insecure_tls: bool

    probe_client_a_id: str
    probe_client_a_secret: str
    probe_client_b_id: str
    probe_client_b_secret: str

    probe_user_username: str
    probe_user_password: str

    probe_api_audience: str
    probe_api_secondary_audience: str
    probe_api_scopes: str  # space-separated

    raw: dict[str, str]

    @classmethod
    def load(cls) -> "Config":
        env = _load_env_file(HERE / ".substrate.env")
        # Allow process-level overrides
        env = {**env, **{k: v for k, v in os.environ.items() if k in env}}

        def need(name: str) -> str:
            v = env.get(name, "").strip()
            if not v:
                fail(f"{name} not set in .substrate.env")
                sys.exit(2)
            return v

        return cls(
            base_url=need("WSO2_IS_BASE_URL"),
            token_url=need("WSO2_IS_TOKEN_URL"),
            authz_url=need("WSO2_IS_AUTHZ_URL"),
            authn_url=need("WSO2_IS_AUTHN_URL"),
            jwks_url=need("WSO2_IS_JWKS_URL"),
            introspect_url=need("WSO2_IS_INTROSPECT_URL"),
            insecure_tls=env.get("INSECURE_TLS", "1") == "1",
            probe_client_a_id=need("PROBE_CLIENT_A_ID"),
            probe_client_a_secret=need("PROBE_CLIENT_A_SECRET"),
            probe_client_b_id=env.get("PROBE_CLIENT_B_ID", ""),
            probe_client_b_secret=env.get("PROBE_CLIENT_B_SECRET", ""),
            probe_user_username=need("PROBE_USER_USERNAME"),
            probe_user_password=need("PROBE_USER_PASSWORD"),
            probe_api_audience=env.get("PROBE_API_AUDIENCE", "urn:probe:api"),
            probe_api_secondary_audience=env.get(
                "PROBE_API_SECONDARY_AUDIENCE", "urn:probe:other"
            ),
            probe_api_scopes=env.get("PROBE_API_SCOPES", "probe.read probe.write"),
            raw=env,
        )


# ─── Thin OAuth client ────────────────────────────────────────────────────
class IdpClient:
    """Minimal OAuth client with verbose logging. Every request prints what
    it sent and what came back, so debugging is trivial."""

    def __init__(self, cfg: Config, verbose: bool = True) -> None:
        self.cfg = cfg
        self.verbose = verbose
        self.session = requests.Session()
        self.session.verify = not cfg.insecure_tls
        if cfg.insecure_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _log_request(self, label: str, url: str, data: dict[str, str], auth: tuple[str, str] | None) -> None:
        if not self.verbose:
            return
        dim(f"  → POST {url}")
        dim(f"    auth: {auth[0] if auth else '(none)'}:{'***' if auth else ''}")
        for k, v in data.items():
            redacted = v if k not in {"client_secret", "password"} else "***"
            short = redacted if len(redacted) < 80 else redacted[:60] + f"...({len(redacted)} chars)"
            dim(f"    {k}={short}")

    def _log_response(self, resp: requests.Response) -> None:
        if not self.verbose:
            return
        dim(f"  ← HTTP {resp.status_code}")
        try:
            body = resp.json()
            for line in json.dumps(body, indent=2).splitlines():
                dim(f"    {line}")
        except (ValueError, json.JSONDecodeError):
            dim(f"    {resp.text[:500]}")

    def _post_token(self, data: dict[str, str], auth: tuple[str, str], label: str) -> dict[str, Any]:
        self._log_request(label, self.cfg.token_url, data, auth)
        resp = self.session.post(
            self.cfg.token_url,
            data=data,
            auth=auth,
            headers={"Accept": "application/json"},
        )
        self._log_response(resp)
        try:
            return resp.json()
        except ValueError:
            return {"error": "non_json_response", "_status": resp.status_code, "_text": resp.text}

    # ─── grants ──────────────────────────────────────────────────────────
    def client_credentials(self, client_id: str, client_secret: str, scope: str | None = None) -> dict[str, Any]:
        data = {"grant_type": "client_credentials"}
        if scope:
            data["scope"] = scope
        return self._post_token(data, (client_id, client_secret), "client_credentials")

    def password_grant(
        self,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        scope: str = "openid",
    ) -> dict[str, Any]:
        data = {
            "grant_type": "password",
            "username": username,
            "password": password,
            "scope": scope,
        }
        return self._post_token(data, (client_id, client_secret), "password")

    def token_exchange(
        self,
        client_id: str,
        client_secret: str,
        subject_token: str,
        actor_token: str | None = None,
        resource: str | list[str] | None = None,
        scope: str | None = None,
        subject_token_type: str = "urn:ietf:params:oauth:token-type:access_token",
        actor_token_type: str = "urn:ietf:params:oauth:token-type:access_token",
    ) -> dict[str, Any]:
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "subject_token_type": subject_token_type,
        }
        if actor_token:
            data["actor_token"] = actor_token
            data["actor_token_type"] = actor_token_type
        if resource:
            if isinstance(resource, list):
                # requests handles list values in `data` as repeated keys
                # by passing tuples list — convert dict→list-of-tuples.
                pass  # see below; we build an alternative below
            else:
                data["resource"] = resource
        if scope:
            data["scope"] = scope

        if isinstance(resource, list):
            payload: list[tuple[str, str]] = [(k, v) for k, v in data.items()]
            for r in resource:
                payload.append(("resource", r))
            self._log_request("token_exchange", self.cfg.token_url, dict(payload), (client_id, client_secret))
            resp = self.session.post(
                self.cfg.token_url,
                data=payload,
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
            )
            self._log_response(resp)
            try:
                return resp.json()
            except ValueError:
                return {"error": "non_json_response", "_status": resp.status_code, "_text": resp.text}

        return self._post_token(data, (client_id, client_secret), "token_exchange")

    # ─── diagnostic ──────────────────────────────────────────────────────
    def jwks(self) -> dict[str, Any]:
        resp = self.session.get(self.cfg.jwks_url)
        return resp.json()

    def reachable(self, url: str) -> tuple[int, str]:
        try:
            resp = self.session.post(url, data={"grant_type": "client_credentials"}, auth=("nope", "nope"))
            return resp.status_code, resp.text[:200]
        except requests.RequestException as e:
            return 0, str(e)


# ─── JWT inspection (no signature verification — diagnostic only) ─────────
def decode_jwt(jwt: str) -> dict[str, Any]:
    try:
        _, payload, _ = jwt.split(".")
    except ValueError as e:
        raise ValueError(f"Not a JWT (expected 3 parts): {e}") from e
    pad = "=" * (-len(payload) % 4)
    raw = base64.urlsafe_b64decode(payload + pad)
    return json.loads(raw)


def show_jwt(jwt: str, label: str = "Decoded JWT payload") -> dict[str, Any]:
    payload = decode_jwt(jwt)
    hr(label)
    print(json.dumps(payload, indent=2))
    return payload


def act_chain(payload: dict[str, Any]) -> list[str]:
    """Walk the nested act claim and return [outermost_actor, ..., innermost_actor]."""
    chain: list[str] = []
    cur = payload.get("act")
    while isinstance(cur, dict):
        sub = cur.get("sub")
        if sub:
            chain.append(sub)
        cur = cur.get("act")
    return chain
