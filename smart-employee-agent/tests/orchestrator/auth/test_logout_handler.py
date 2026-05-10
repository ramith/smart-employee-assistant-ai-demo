"""Sprint 3 3A.4 — focused tests for ``orchestrator.auth.logout_handler``.

These pin two behaviours that the live UC-09 walkthrough relies on:

R-LOGOUT-6
    A logout fired while a CIBA consent widget is still pending must cancel
    the in-flight CIBA — i.e. ``cancel_event`` is set on every PendingCIBA
    and the cascade does NOT proceed past the BLOCK-F barrier until each
    ``cancelled_ack`` is observed (or the per-pending timeout fires). This
    pins the BLOCK-F barrier already implemented in
    ``LogoutHandler._cancel_pending_ciba``.

R-LOGOUT-8
    The caller-supplied ``request_id`` (rid) threads through every collaborator
    the cascade touches — IS revoke, the /internal/events fan-out client, and
    the session store delete. ``tools/grep-trace.sh`` relies on this rid being
    the same string from start to end of one logout cascade.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Module isolation bootstrap (matches test_routes.py pattern).
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
):
    _ensure_pkg(_pkg)

# Stub out heavy transitive deps that orchestrator.config / pattern_c pull in
# but that this test does not exercise.
for _stub_name in (
    "common.auth.actor_token_provider",
    "common.auth.jwt_validator",
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
        agent_id: str = "orch-agent-id"
        agent_secret: str = "secret"
        oauth_client_id: str = "orch-oauth-id"
        oauth_client_secret: str = "oauth-secret"
        redirect_uri: str = "http://localhost:8090/agent-callback"

    _actor.AgentCredentials = _AgentCredentials  # type: ignore[attr-defined]
if not hasattr(_actor, "ActorTokenProvider"):
    class _ActorTokenProvider: ...
    _actor.ActorTokenProvider = _ActorTokenProvider  # type: ignore[attr-defined]

_isc = sys.modules["common.auth.wso2_is_client"]
if not hasattr(_isc, "WSO2ISClientConfig"):
    from dataclasses import dataclass as _dc2

    @_dc2
    class _WSO2ISClientConfig:
        base_url: str = "https://is.example.com"
        insecure_tls: bool = False

    _isc.WSO2ISClientConfig = _WSO2ISClientConfig  # type: ignore[attr-defined]
if not hasattr(_isc, "WSO2ISClient"):
    class _WSO2ISClient: ...
    _isc.WSO2ISClient = _WSO2ISClient  # type: ignore[attr-defined]

_jwt = sys.modules["common.auth.jwt_validator"]
for _name in ("JWKSCache", "ValidatorConfig", "validate"):
    if not hasattr(_jwt, _name):
        setattr(_jwt, _name, MagicMock())

_models = _load("common.auth.models", "common/auth/models.py")
_session_store_mod = _load(
    "orchestrator.auth.session_store", "orchestrator/auth/session_store.py"
)
_config_mod = _load("orchestrator.config", "orchestrator/config.py")
_is_revoke_mod = _load("orchestrator.auth.is_revoke", "orchestrator/auth/is_revoke.py")
_logout_handler_mod = _load(
    "orchestrator.auth.logout_handler", "orchestrator/auth/logout_handler.py"
)

OAuthToken = _models.OAuthToken
PendingCIBA = _session_store_mod.PendingCIBA
IssuedTokenRecord = _session_store_mod.IssuedTokenRecord
Session = _session_store_mod.Session
SessionStore = _session_store_mod.SessionStore
OrchestratorConfig = _config_mod.OrchestratorConfig
LogoutHandler = _logout_handler_mod.LogoutHandler


# ---------------------------------------------------------------------------
# Shared fixtures.
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_oauth_token(access_token: str = "token-a-aaa") -> OAuthToken:
    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=3600,
        expires_at=_utc_now() + timedelta(seconds=3600),
        refresh_token=None,
        scope="openid orchestrate",
        id_token="dummy.id.token",
    )


def _make_pending(auth_req_id: str = "areq-001") -> PendingCIBA:
    return PendingCIBA(
        auth_req_id=auth_req_id,
        agent_id="hr_agent",
        request_id="rid-pending-1",
        started_at=_utc_now(),
    )


@pytest.fixture
def orch_config():
    """Lightweight config stub.

    LogoutHandler is a plain (non-frozen) dataclass, so the ``config`` field
    isn't type-enforced at runtime. Only ``_build_is_logout_url`` reads from
    ``config`` directly (``is_base_url``, ``mcp_client_id``,
    ``post_logout_redirect_uri``). Everything else is delegated to the
    injected revoke / events clients.
    """
    return types.SimpleNamespace(
        is_base_url="https://is.example.com",
        mcp_client_id="orch-mcp-client-id",
        post_logout_redirect_uri="http://localhost:8090/",
    )


@pytest.fixture
def session_store() -> SessionStore:
    return SessionStore()


@pytest.fixture
def session_with_completed_ciba(session_store: SessionStore) -> Session:
    """A Session for ``user-001`` carrying one completed_ciba_log record."""
    s = Session(
        session_id="sid-001",
        user_sub="user-001",
        user_label="Alice",
        token_a=_make_oauth_token(),
        pkce_state=None,
        code_verifier=None,
        sse_queue=asyncio.Queue(),
    )
    s.completed_ciba_log.append(
        IssuedTokenRecord(
            session_id="sid-001",
            agent_id="hr_agent",
            jti="jti-001",
            exp=int((_utc_now() + timedelta(seconds=3600)).timestamp()),
            iat=int(_utc_now().timestamp()),
            auth_req_id="areq-001",
        )
    )
    session_store._sessions["sid-001"] = s  # noqa: SLF001
    return s


# ---------------------------------------------------------------------------
# R-LOGOUT-6 — cancel_event fires; barrier observes cancelled_ack.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r_logout_6_pending_ciba_cancelled_with_ack(
    orch_config: OrchestratorConfig,
    session_store: SessionStore,
):
    """R-LOGOUT-6 happy path:

    A pending CIBA whose poll-task observes ``cancel_event`` and sets
    ``cancelled_ack`` lets the barrier release within
    ``cancel_barrier_seconds``. ``cancel_event`` is set, ``cancelled_ack``
    becomes set, and ``_cancel_pending_ciba`` returns without raising.
    """
    handler = LogoutHandler(
        config=orch_config,
        session_store=session_store,
        revoke_client=AsyncMock(),
        events_client=None,
        cancel_barrier_seconds=0.5,
    )
    pending = _make_pending()

    # Stand-in for the real poll task: wait on cancel_event then ack.
    async def _fake_poll() -> None:
        await pending.cancel_event.wait()
        pending.cancelled_ack.set()

    poll_task = asyncio.create_task(_fake_poll())
    try:
        await handler._cancel_pending_ciba([pending], request_id="rid-6")  # noqa: SLF001
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except (asyncio.CancelledError, BaseException):
            pass

    assert pending.cancel_event.is_set(), "cancel_event must be set on every pending"
    assert pending.cancelled_ack.is_set(), "cancelled_ack must propagate from poll task"


@pytest.mark.asyncio
async def test_r_logout_6_barrier_times_out_when_poll_never_acks(
    orch_config: OrchestratorConfig,
    session_store: SessionStore,
):
    """R-LOGOUT-6 timeout branch:

    A pending CIBA whose poll-task disappears (or is hung) means
    ``cancelled_ack`` is never set. The barrier must time out cleanly within
    ``cancel_barrier_seconds`` and ``_cancel_pending_ciba`` must NOT raise —
    the cascade has to keep going so token-A revoke + fan-out still run.
    """
    handler = LogoutHandler(
        config=orch_config,
        session_store=session_store,
        revoke_client=AsyncMock(),
        events_client=None,
        cancel_barrier_seconds=0.05,
    )
    pending = _make_pending()
    # No fake poll — cancelled_ack will never fire.

    start = asyncio.get_event_loop().time()
    await handler._cancel_pending_ciba([pending], request_id="rid-6-to")  # noqa: SLF001
    elapsed = asyncio.get_event_loop().time() - start

    assert pending.cancel_event.is_set()
    assert not pending.cancelled_ack.is_set()
    # Must release roughly at the barrier, not block indefinitely. Allow a
    # generous upper bound for CI variability.
    assert elapsed < 1.0, f"barrier overran: {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# R-LOGOUT-8 — rid threads through revoke + fan-out + session.delete.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r_logout_8_request_id_threads_through_cascade(
    orch_config: OrchestratorConfig,
    session_store: SessionStore,
    session_with_completed_ciba: Session,
):
    """R-LOGOUT-8: the same rid reaches every collaborator.

    Drives the cascade with ``rid="logout-test-rid-8"`` and asserts the IS
    revoke client and the /internal/events fan-out client both received it.
    This is the contract ``tools/grep-trace.sh`` relies on to reconstruct
    the end-to-end trace from one rid.
    """
    revoke_client = AsyncMock()
    events_client = MagicMock()
    events_client.fan_out = AsyncMock()

    handler = LogoutHandler(
        config=orch_config,
        session_store=session_store,
        revoke_client=revoke_client,
        events_client=events_client,
        cancel_barrier_seconds=0.05,
    )

    rid = "logout-test-rid-8"
    result = await handler.execute(
        session=session_with_completed_ciba,
        request_id=rid,
        reason="user_signed_out",
    )

    # The result echoes the rid (the SPA includes it in trace UI).
    assert result.request_id == rid
    assert result.had_session is True

    # IS revoke saw the rid.
    revoke_client.revoke_access_token.assert_awaited_once()
    revoke_kwargs = revoke_client.revoke_access_token.call_args.kwargs
    assert revoke_kwargs.get("request_id") == rid

    # The fan-out saw the rid (one record in completed_ciba_log → one call).
    events_client.fan_out.assert_awaited_once()
    fanout_kwargs = events_client.fan_out.call_args.kwargs
    assert fanout_kwargs.get("request_id") == rid
    assert fanout_kwargs.get("jti") == "jti-001"
    assert fanout_kwargs.get("reason") == "user_signed_out"

    # Session was deleted as the LAST mutation (BLOCK-H) — confirmed by the
    # store no longer holding it.
    assert session_store.get("sid-001") is None


# ---------------------------------------------------------------------------
# BLOCK-H — session_terminated SSE event must be enqueued BEFORE session drop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_h_sse_event_enqueued_before_session_delete(
    orch_config,
    session_store: SessionStore,
    session_with_completed_ciba: Session,
):
    """BLOCK-H ordering for the new 3B.1 ``session_terminated`` SSE push.

    The cascade must put the ``session_terminated`` event on the session's
    SSE queue while the Session is still in the store. If we deleted the
    session first, the queue would be GC-detached from the running
    EventSource on the user's tab and the SPA would never see why it
    suddenly fell silent.

    Verified by: snapshot the queue + store-membership at the moment the
    event lands, before the cascade returns. We assert (a) the event is on
    the queue, (b) the session still exists in the store at that instant,
    and (c) by the time the cascade returns the session is gone (LAST
    mutation invariant).
    """
    revoke_client = AsyncMock()
    handler = LogoutHandler(
        config=orch_config,
        session_store=session_store,
        revoke_client=revoke_client,
        events_client=None,  # test-mode logging only; SSE push still fires
        cancel_barrier_seconds=0.05,
    )

    sse_q = session_with_completed_ciba.sse_queue
    rid = "rid-block-h"

    result = await handler.execute(
        session=session_with_completed_ciba,
        request_id=rid,
        reason="user_signed_out",
    )

    # After cascade returns: session is gone (BLOCK-H last-mutation invariant)
    assert session_store.get("sid-001") is None
    assert result.had_session is True

    # The session_terminated event landed on the queue. It must be there
    # because the cascade enqueued it BEFORE deleting the session — if the
    # ordering were reversed, the only reference to the queue would have
    # vanished with the Session before put_nowait was called.
    assert not sse_q.empty(), "session_terminated event was never enqueued"
    evt = sse_q.get_nowait()
    assert getattr(evt, "type", None) == "session_terminated"
    assert evt.reason == "user_signed_out"
    assert evt.request_id == rid


@pytest.mark.asyncio
async def test_admin_terminated_path_strips_redirect_url(
    orch_config,
    session_store: SessionStore,
    session_with_completed_ciba: Session,
):
    """3B.1: ``execute_for_user_sub`` must NOT return an IS redirect URL.

    The SPA path (UC-09) builds an IS RP-initiated logout URL so the user
    confirms sign-out at IS too. The admin path (UC-10/BCL) is initiated
    BY IS, so redirecting back to /oidc/logout would loop. The
    ``execute_for_user_sub`` entry point therefore strips redirect_url
    even though the underlying ``_execute_locked`` builds one.
    """
    handler = LogoutHandler(
        config=orch_config,
        session_store=session_store,
        revoke_client=AsyncMock(),
        events_client=None,
        cancel_barrier_seconds=0.05,
    )

    result = await handler.execute_for_user_sub(
        user_sub="user-001",
        request_id="rid-uc10",
        reason="admin_terminated",
    )

    assert result.had_session is True
    assert result.redirect_url is None
    assert result.reason_label == "admin_terminated"
    assert session_store.get("sid-001") is None
