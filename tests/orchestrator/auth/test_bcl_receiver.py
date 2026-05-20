"""Sprint 3 3B.1 — tests for orchestrator/auth/bcl_receiver.py.

Coverage targets:
    1. Happy path with claims.sub → cascade fires for that user.
    2. Each of the 9 validation checks rejects independently with the
       correct categorical reason and a 400 response.
    3. Replay protection: duplicate jti → 200 idempotent; cascade does
       NOT fire twice.
    4. sub absent + sid present + sid in reverse index → resolved, cascade
       fires for the correct user.
    5. sub absent + sid absent from reverse index → 200 no_session;
       cascade does NOT fire.
    6. Bad Content-Type / missing logout_token form param → 400 early reject.

The validator's signature step is exercised by giving it a real RSA
keypair and a JWKSCache whose ``get_key`` is mocked to hand back the
matching public JWK.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time
import types
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

# ---------------------------------------------------------------------------
# Module isolation bootstrap (matches test_routes.py / test_logout_handler.py).
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted: str) -> None:
    if dotted in sys.modules:
        return
    stub = types.ModuleType(dotted)
    stub.__package__ = dotted
    stub.__path__ = [str(_ROOT / dotted.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted] = stub


def _load(dotted: str, rel: str) -> types.ModuleType:
    if dotted in sys.modules and hasattr(sys.modules[dotted], "__file__"):
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, _ROOT / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = dotted.rsplit(".", 1)[0] if "." in dotted else ""
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


for _pkg in (
    "common",
    "common.auth",
    "common.revocation",
    "orchestrator",
    "orchestrator.auth",
    "orchestrator.agent_registry",
    "orchestrator.events",
):
    _ensure_pkg(_pkg)

# Stub heavy transitive deps that we don't exercise.
for _stub_name in (
    "common.auth.actor_token_provider",
    "common.auth.wso2_is_client",
):
    if _stub_name not in sys.modules:
        _m = types.ModuleType(_stub_name)
        _m.__package__ = _stub_name.rsplit(".", 1)[0]
        sys.modules[_stub_name] = _m

_actor = sys.modules["common.auth.actor_token_provider"]
if not hasattr(_actor, "AgentCredentials"):
    from dataclasses import dataclass as _dc

    @_dc
    class _AgentCredentials:
        agent_id: str = "x"
        agent_secret: str = "x"
        oauth_client_id: str = "x"
        oauth_client_secret: str = "x"
        redirect_uri: str = "http://x"

    _actor.AgentCredentials = _AgentCredentials  # type: ignore[attr-defined]
if not hasattr(_actor, "ActorTokenProvider"):
    class _ATP: ...
    _actor.ActorTokenProvider = _ATP  # type: ignore[attr-defined]

_isc = sys.modules["common.auth.wso2_is_client"]
if not hasattr(_isc, "WSO2ISClientConfig"):
    from dataclasses import dataclass as _dc2

    @_dc2
    class _WSO2C:
        base_url: str = "https://x"
        insecure_tls: bool = False

    _isc.WSO2ISClientConfig = _WSO2C  # type: ignore[attr-defined]
if not hasattr(_isc, "WSO2ISClient"):
    class _W: ...
    _isc.WSO2ISClient = _W  # type: ignore[attr-defined]

# Real loads.
_models = _load("common.auth.models", "common/auth/models.py")
_errors = _load("common.auth.errors", "common/auth/errors.py")
_jwt_validator = _load("common.auth.jwt_validator", "common/auth/jwt_validator.py")
_session_store_mod = _load(
    "orchestrator.auth.session_store", "orchestrator/auth/session_store.py"
)
_config_mod = _load("orchestrator.config", "orchestrator/config.py")
_is_revoke_mod = _load("orchestrator.auth.is_revoke", "orchestrator/auth/is_revoke.py")
_logout_handler_mod = _load(
    "orchestrator.auth.logout_handler", "orchestrator/auth/logout_handler.py"
)
_bcl_mod = _load("orchestrator.auth.bcl_receiver", "orchestrator/auth/bcl_receiver.py")

JWKSCache = _jwt_validator.JWKSCache
SessionStore = _session_store_mod.Session.__module__  # noqa
SessionStoreCls = _session_store_mod.SessionStore
LogoutHandler = _logout_handler_mod.LogoutHandler
LogoutResult = _logout_handler_mod.LogoutResult
SeenLogoutTokens = _bcl_mod.SeenLogoutTokens
BCLReceiverDeps = _bcl_mod.BCLReceiverDeps
build_bcl_router = _bcl_mod.build_bcl_router
validate_logout_token = _bcl_mod.validate_logout_token
BCLValidationError = _bcl_mod.BCLValidationError
BCL_EVENTS_URI = _bcl_mod.BCL_EVENTS_URI
LOGOUT_TOKEN_TYP = _bcl_mod.LOGOUT_TOKEN_TYP


# ---------------------------------------------------------------------------
# Test rig — RSA keypair + token signer + mocked JWKS cache.
# ---------------------------------------------------------------------------

ISSUER = "https://13.60.190.47:9443/oauth2/token"
AUDIENCE = "orch-mcp-client-id"
USER_SUB = "user-uuid-001"
USER_SID = "sid-001"


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    public_jwk: dict[str, Any] = json.loads(
        RSAAlgorithm.to_jwk(private_key.public_key())
    )
    public_jwk["kid"] = "bcl-test-key-1"
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"
    return private_key, public_jwk


def _sign(
    private_key,
    payload: dict[str, Any],
    *,
    typ: str = LOGOUT_TOKEN_TYP,
    alg: str = "RS256",
    kid: str = "bcl-test-key-1",
) -> str:
    headers = {"typ": typ, "alg": alg, "kid": kid}
    return pyjwt.encode(payload, private_key, algorithm=alg, headers=headers)


def _valid_payload() -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "jti": f"jti-{now}-{id(now)}",
        "sub": USER_SUB,
        "events": {BCL_EVENTS_URI: {}},
    }


@pytest.fixture
def jwks_cache(rsa_keypair):
    _, public_jwk = rsa_keypair
    cache = JWKSCache(jwks_url="https://mock/", insecure_tls=True)
    cache.get_key = AsyncMock(return_value=public_jwk)  # type: ignore[method-assign]
    return cache


# ---------------------------------------------------------------------------
# Direct validator tests — fastest path to cover the 9 checks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_claims(rsa_keypair, jwks_cache):
    """Check #1-#9 all pass on a clean payload → LogoutTokenClaims returned."""
    priv, _ = rsa_keypair
    token = _sign(priv, _valid_payload())
    claims = await validate_logout_token(
        token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
    )
    assert claims.sub == USER_SUB
    assert claims.sid is None
    assert claims.iss == ISSUER


@pytest.mark.asyncio
async def test_check_2_alg_not_rs256_rejected(rsa_keypair, jwks_cache):
    """Check #2 — only RS256 is accepted. Anything else → alg_not_allowed."""
    priv, _ = rsa_keypair
    token = pyjwt.encode(
        _valid_payload(),
        "shared-secret",
        algorithm="HS256",
        headers={"typ": LOGOUT_TOKEN_TYP, "alg": "HS256", "kid": "bcl-test-key-1"},
    )
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "alg_not_allowed"


@pytest.mark.asyncio
async def test_check_3_typ_wrong_value_rejected(rsa_keypair, jwks_cache):
    """Check #3 — typ that's present but wrong (e.g. "JWT") is rejected.

    WSO2 IS RC accommodation (2026-05-10) means we accept *absent* typ as
    well as the spec-exact ``logout+jwt``. A typ that's present and
    wrong remains a hard reject — that's how an id_token replay attempt
    would surface (id_tokens carry typ=JWT). Defence remains layered:
    Check #7 (events claim) is the categorical separator.
    """
    priv, _ = rsa_keypair
    token = _sign(priv, _valid_payload(), typ="JWT")
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "typ_not_logout_jwt"


@pytest.mark.asyncio
async def test_check_3_typ_absent_accepted_wso2_is_accommodation(
    rsa_keypair, jwks_cache, caplog
):
    """Check #3 — absent typ header is accepted with a structured warning.

    Pins the 2026-05-10 WSO2 IS 7.x RC accommodation. If a future IS
    upgrade starts emitting typ=logout+jwt, this test should still pass
    (absent is one of two acceptable values). The categorical separator
    between BCL and non-BCL JWTs is Check #7 (events claim), not typ.
    """
    import base64  # noqa: PLC0415
    priv, _ = rsa_keypair
    # PyJWT auto-injects typ=JWT even when omitted from headers, so to
    # simulate WSO2 IS we sign normally then re-encode the header
    # without the typ field. This produces an unsigned-header-tampered
    # JWT whose body signature is still valid (header is part of the
    # signing input, so we must re-sign over the new header).
    payload = _valid_payload()
    # Sign with a known set of headers, then take the underlying JWS and
    # re-encode header sans typ. Easiest: build the JWS manually.
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import padding  # noqa: PLC0415

    header_no_typ = {"alg": "RS256", "kid": "bcl-test-key-1"}
    header_b64 = base64.urlsafe_b64encode(
        json.dumps(header_no_typ, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    signing_input = f"{header_b64}.{payload_b64}".encode()
    sig = priv.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    token = f"{header_b64}.{payload_b64}.{sig_b64}"
    import logging  # noqa: PLC0415
    with caplog.at_level(logging.WARNING, logger="orchestrator.auth.bcl_receiver"):
        claims = await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert claims.sub == USER_SUB
    assert any("bcl_typ_header_absent" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_check_4_iss_mismatch_rejected(rsa_keypair, jwks_cache):
    priv, _ = rsa_keypair
    payload = _valid_payload() | {"iss": "https://attacker.example.com"}
    token = _sign(priv, payload)
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "iss_mismatch"


@pytest.mark.asyncio
async def test_check_5_aud_mismatch_rejected(rsa_keypair, jwks_cache):
    priv, _ = rsa_keypair
    payload = _valid_payload() | {"aud": "some-other-client"}
    token = _sign(priv, payload)
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "aud_mismatch"


@pytest.mark.asyncio
async def test_check_6_iat_too_old_rejected(rsa_keypair, jwks_cache):
    priv, _ = rsa_keypair
    payload = _valid_payload() | {"iat": int(time.time()) - 600}  # 10m old
    token = _sign(priv, payload)
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "iat_too_old"


@pytest.mark.asyncio
async def test_check_6_iat_in_future_rejected(rsa_keypair, jwks_cache):
    priv, _ = rsa_keypair
    payload = _valid_payload() | {"iat": int(time.time()) + 3600}  # 1h ahead
    token = _sign(priv, payload)
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "iat_in_future"


@pytest.mark.asyncio
async def test_check_7_events_claim_missing_rejected(rsa_keypair, jwks_cache):
    priv, _ = rsa_keypair
    payload = _valid_payload()
    payload.pop("events")
    token = _sign(priv, payload)
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "events_claim_missing_bcl_uri"


@pytest.mark.asyncio
async def test_check_8_nonce_present_rejected(rsa_keypair, jwks_cache):
    """Check #8 — nonce MUST be absent (it belongs in id_tokens)."""
    priv, _ = rsa_keypair
    payload = _valid_payload() | {"nonce": "n-1234"}
    token = _sign(priv, payload)
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "nonce_must_be_absent"


@pytest.mark.asyncio
async def test_check_9_sub_and_sid_both_missing_rejected(rsa_keypair, jwks_cache):
    priv, _ = rsa_keypair
    payload = _valid_payload()
    payload.pop("sub")
    token = _sign(priv, payload)
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "sub_and_sid_both_missing"


@pytest.mark.asyncio
async def test_check_9_sid_only_accepted(rsa_keypair, jwks_cache):
    """sid alone (sub absent) should validate — receiver does sid → sub lookup."""
    priv, _ = rsa_keypair
    payload = _valid_payload()
    payload.pop("sub")
    payload["sid"] = USER_SID
    token = _sign(priv, payload)
    claims = await validate_logout_token(
        token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
    )
    assert claims.sub is None
    assert claims.sid == USER_SID


@pytest.mark.asyncio
async def test_jti_missing_rejected(rsa_keypair, jwks_cache):
    """Spec floor: jti present so SeenLogoutTokens can dedup."""
    priv, _ = rsa_keypair
    payload = _valid_payload()
    payload.pop("jti")
    token = _sign(priv, payload)
    with pytest.raises(BCLValidationError) as exc:
        await validate_logout_token(
            token, expected_iss=ISSUER, expected_aud=AUDIENCE, jwks_cache=jwks_cache
        )
    assert exc.value.reason == "jti_missing"


# ---------------------------------------------------------------------------
# SeenLogoutTokens
# ---------------------------------------------------------------------------


def test_seen_logout_tokens_dedup_and_cap():
    seen = SeenLogoutTokens(hard_cap=3, sweep_interval_seconds=999)
    seen.add("a", 1.0)
    seen.add("b", 2.0)
    seen.add("c", 3.0)
    seen.add("d", 4.0)  # forces FIFO eviction of "a"
    assert "a" not in seen
    assert {"b", "c", "d"} <= set(seen._items.keys())  # noqa: SLF001
    # idempotent re-add
    seen.add("d", 4.0)
    assert len(seen) == 3


def test_seen_logout_tokens_sweep_drops_old():
    seen = SeenLogoutTokens(iat_tolerance_seconds=10, sweep_interval_seconds=999)
    now = 1000.0
    seen.add("old", now - 100)
    seen.add("fresh", now - 5)
    removed = seen.sweep_once(now=now)
    assert removed == 1
    assert "old" not in seen
    assert "fresh" in seen


# ---------------------------------------------------------------------------
# Route-level integration via TestClient.
# ---------------------------------------------------------------------------


def _make_app(
    rsa_keypair,
    *,
    sid_to_sub: dict[str, str] | None = None,
) -> tuple[FastAPI, AsyncMock, SessionStoreCls]:
    """Build a minimal FastAPI app with the BCL router mounted.

    Returns the app, a MagicMock for the LogoutHandler.execute_for_user_sub
    method (so tests can assert call args), and the SessionStore (so tests
    can poke the sid → sub reverse index).
    """
    _, public_jwk = rsa_keypair
    cache = JWKSCache(jwks_url="https://mock/", insecure_tls=True)
    cache.get_key = AsyncMock(return_value=public_jwk)  # type: ignore[method-assign]

    seen = SeenLogoutTokens()
    store = SessionStoreCls()
    if sid_to_sub:
        for sid, sub in sid_to_sub.items():
            store.register_sid(sid, sub)

    handler = MagicMock(spec=LogoutHandler)
    handler.execute_for_user_sub = AsyncMock(
        return_value=LogoutResult(
            had_session=True,
            redirect_url=None,
            request_id="rid-x",
            reason_label="admin_terminated",
        )
    )

    deps = BCLReceiverDeps(
        expected_iss=ISSUER,
        expected_aud=AUDIENCE,
        jwks_cache=cache,
        seen_logout_tokens=seen,
        session_store=store,
        logout_handler=handler,
    )
    app = FastAPI()
    app.include_router(build_bcl_router(deps))
    return app, handler, store


def test_route_happy_path_runs_cascade(rsa_keypair):
    priv, _ = rsa_keypair
    app, handler, _ = _make_app(rsa_keypair)
    client = TestClient(app)

    token = _sign(priv, _valid_payload())
    resp = client.post(
        "/backchannel-logout",
        content=f"logout_token={token}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    handler.execute_for_user_sub.assert_awaited_once()
    kwargs = handler.execute_for_user_sub.call_args.kwargs
    assert kwargs["user_sub"] == USER_SUB
    assert kwargs["reason"] == "admin_terminated"


def test_route_rejects_bad_content_type(rsa_keypair):
    priv, _ = rsa_keypair
    app, handler, _ = _make_app(rsa_keypair)
    client = TestClient(app)

    token = _sign(priv, _valid_payload())
    resp = client.post(
        "/backchannel-logout",
        json={"logout_token": token},  # wrong content type
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad_content_type"
    handler.execute_for_user_sub.assert_not_awaited()


def test_route_rejects_missing_logout_token(rsa_keypair):
    app, handler, _ = _make_app(rsa_keypair)
    client = TestClient(app)
    resp = client.post(
        "/backchannel-logout",
        content="other_field=xx",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing_logout_token"
    handler.execute_for_user_sub.assert_not_awaited()


def test_route_rejects_invalid_signature(rsa_keypair):
    """Forged token from a different RSA keypair → 400, no cascade. R-LOGOUT-EX-3."""
    other_priv = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    app, handler, _ = _make_app(rsa_keypair)
    client = TestClient(app)

    token = _sign(other_priv, _valid_payload())
    resp = client.post(
        "/backchannel-logout",
        content=f"logout_token={token}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_logout_token"
    handler.execute_for_user_sub.assert_not_awaited()


def test_route_dedup_idempotent_on_duplicate_jti(rsa_keypair):
    """Same jti POSTed twice → both 200, but cascade runs only once."""
    priv, _ = rsa_keypair
    app, handler, _ = _make_app(rsa_keypair)
    client = TestClient(app)

    token = _sign(priv, _valid_payload())
    body = f"logout_token={token}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    r1 = client.post("/backchannel-logout", content=body, headers=headers)
    r2 = client.post("/backchannel-logout", content=body, headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("deduped") is True
    assert handler.execute_for_user_sub.await_count == 1


def test_route_resolves_sid_via_reverse_index(rsa_keypair):
    """sub absent + sid present + sid pre-registered → cascade fires for that sub."""
    priv, _ = rsa_keypair
    app, handler, _ = _make_app(rsa_keypair, sid_to_sub={USER_SID: USER_SUB})
    client = TestClient(app)

    payload = _valid_payload()
    payload.pop("sub")
    payload["sid"] = USER_SID
    token = _sign(priv, payload)

    resp = client.post(
        "/backchannel-logout",
        content=f"logout_token={token}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    handler.execute_for_user_sub.assert_awaited_once()
    assert handler.execute_for_user_sub.call_args.kwargs["user_sub"] == USER_SUB


def test_route_unknown_sid_returns_no_session(rsa_keypair):
    """sid not in reverse index → 200 no_session, cascade NOT triggered."""
    priv, _ = rsa_keypair
    app, handler, _ = _make_app(rsa_keypair)  # empty reverse index
    client = TestClient(app)

    payload = _valid_payload()
    payload.pop("sub")
    payload["sid"] = "unknown-sid"
    token = _sign(priv, payload)

    resp = client.post(
        "/backchannel-logout",
        content=f"logout_token={token}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    assert resp.json().get("no_session") is True
    handler.execute_for_user_sub.assert_not_awaited()
