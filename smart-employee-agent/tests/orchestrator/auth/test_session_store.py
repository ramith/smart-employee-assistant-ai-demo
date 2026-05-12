"""Tests for orchestrator/auth/session_store.py — Wave 5, Sprint 1.

Coverage targets (13 tests)
----------------------------
 1. ``create()`` returns a Session with all mandatory fields populated; the
    new ``session_id`` is a UUID4 string of length 36.
 2. ``get(unknown)`` returns ``None``.
 3. ``get_or_404()`` raises ``KeyError`` for an unknown session_id.
 4. ``touch()`` updates ``last_seen_at``.
 5. ``is_expired(60)`` is ``False`` right after creation; ``True`` after
    ``last_seen_at`` is backdated by 120 seconds.
 6. ``delete()`` returns ``True`` for an existing session_id and ``False``
    for an unknown one.
 7. ``prune_expired()`` removes only expired sessions; returns count pruned.
 8. ``find_pending_ciba()`` locates a ``PendingCIBA`` by ``auth_req_id``
    across multiple sessions.
 9. ``find_pending_ciba()`` returns ``None`` when no session holds the id.
10. ``Session.cached_obo`` keying: ``(agent_id, frozenset({"openid", "hr.read"}))``
    stores and retrieves the correct token.
11. Concurrent ``create()`` calls under ``asyncio.gather`` produce all distinct
    ``session_id``s with no race condition.
12. ``IssuedTokenRecord`` dataclass round-trips through field assignment.
13. ``get_or_404()`` touches the session (``last_seen_at`` advances) when found.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# Module isolation helpers — same pattern used throughout the test suite
# ---------------------------------------------------------------------------

_ROOT = pathlib.Path(__file__).parent.parent.parent.parent  # smart-employee-agent/


def _ensure_pkg(dotted_name: str) -> None:
    """Register a bare package stub in sys.modules if not already present."""
    if dotted_name in sys.modules:
        return
    stub = types.ModuleType(dotted_name)
    stub.__package__ = dotted_name
    stub.__path__ = [str(_ROOT / dotted_name.replace(".", "/"))]  # type: ignore[assignment]
    sys.modules[dotted_name] = stub


def _load_module(dotted_name: str, rel_path: str) -> types.ModuleType:
    """Load a single .py file into sys.modules under *dotted_name*."""
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]
    file_path = _ROOT / rel_path
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {file_path}"
    module = importlib.util.module_from_spec(spec)
    module.__package__ = dotted_name.rsplit(".", 1)[0] if "." in dotted_name else ""
    sys.modules[dotted_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# Ensure all intermediate package namespaces exist before loading modules.
for _pkg in ("common", "common.auth", "orchestrator", "orchestrator.auth"):
    _ensure_pkg(_pkg)

# Load dependencies in dependency order.
_models_mod = _load_module("common.auth.models", "common/auth/models.py")
_store_mod = _load_module(
    "orchestrator.auth.session_store",
    "orchestrator/auth/session_store.py",
)

# Bind public names for test use.
OAuthToken = _models_mod.OAuthToken
OBOToken = _models_mod.OBOToken
PendingCIBA = _store_mod.PendingCIBA
IssuedTokenRecord = _store_mod.IssuedTokenRecord
Session = _store_mod.Session
SessionStore = _store_mod.SessionStore

# ---------------------------------------------------------------------------
# Shared factory helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_token_a(
    access_token: str = "tok-aaa",
    expires_in: int = 3600,
) -> OAuthToken:
    """Construct a minimal OAuthToken for test use."""
    now = _utc_now()
    return OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=expires_in,
        expires_at=now + timedelta(seconds=expires_in),
        refresh_token=None,
        scope="openid orchestrate",
        id_token=None,
    )


def _make_obo_token(
    access_token: str = "obo-tok",
    sub: str = "user-sub-001",
    act_sub: str = "hr_agent-id",
    jti: str = "jti-abc123",
) -> OBOToken:
    """Construct a minimal OBOToken for test use."""
    now = _utc_now()
    raw = OAuthToken(
        access_token=access_token,
        token_type="Bearer",
        expires_in=3600,
        expires_at=now + timedelta(seconds=3600),
        refresh_token=None,
        scope="openid hr.read",
        id_token=None,
    )
    return OBOToken(
        raw=raw,
        sub=sub,
        act_sub=act_sub,
        aud="hr_agent-client-id",
        iss="https://api.asgardeo.io/t/ddademo/oauth2/token",
        iat=now,
        jti=jti,
    )


def _make_store(max_idle_seconds: int = 3600) -> SessionStore:
    """Return a fresh SessionStore."""
    return SessionStore(max_idle_seconds=max_idle_seconds)


def _create_session(store: SessionStore, user_sub: str = "user-001") -> Session:
    """Convenience wrapper around store.create()."""
    return store.create(
        user_sub=user_sub,
        user_label="Alice",
        token_a=_make_token_a(),
    )


# ---------------------------------------------------------------------------
# Test 1 — create() returns a fully-populated Session; session_id is UUID4
# ---------------------------------------------------------------------------


def test_create_returns_fully_populated_session() -> None:
    """create() must return a Session with all mandatory fields set; session_id is UUID4."""
    store = _make_store()
    token_a = _make_token_a(access_token="tok-111")

    session = store.create(
        user_sub="user-sub-001",
        user_label="Alice Liddell",
        token_a=token_a,
    )

    # session_id is a UUID4 string (8-4-4-4-12 format, length 36)
    assert isinstance(session.session_id, str)
    assert len(session.session_id) == 36
    assert session.session_id.count("-") == 4

    assert session.user_sub == "user-sub-001"
    assert session.user_label == "Alice Liddell"
    assert session.token_a is token_a

    # PKCE fields start clear
    assert session.pkce_state is None
    assert session.code_verifier is None

    # async types are initialised
    assert isinstance(session.sse_queue, asyncio.Queue)

    # Mutable collections start empty
    assert session.pending_ciba == {}
    assert session.cached_obo == {}
    assert session.completed_ciba_log == []

    # Timestamps are timezone-aware UTC
    assert session.created_at.tzinfo is not None
    assert session.last_seen_at.tzinfo is not None

    # Session is retrievable immediately
    assert store.get(session.session_id) is session


# ---------------------------------------------------------------------------
# Test 2 — get(unknown) returns None
# ---------------------------------------------------------------------------


def test_get_unknown_returns_none() -> None:
    """get() for a session_id that was never created must return None."""
    store = _make_store()
    _create_session(store)

    assert store.get("not-a-real-session-id") is None
    assert store.get("") is None


# ---------------------------------------------------------------------------
# Test 3 — get_or_404() raises KeyError on unknown session_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_404_raises_key_error_on_unknown() -> None:
    """get_or_404() must raise KeyError for a session_id that does not exist."""
    store = _make_store()

    with pytest.raises(KeyError):
        await store.get_or_404("totally-unknown-id")


# ---------------------------------------------------------------------------
# Test 4 — touch() updates last_seen_at
# ---------------------------------------------------------------------------


def test_touch_updates_last_seen_at() -> None:
    """touch() must advance last_seen_at to approximately now."""
    store = _make_store()
    session = _create_session(store)

    # Backdate last_seen_at so we can see movement
    original_ts = session.last_seen_at
    session.last_seen_at = original_ts - timedelta(seconds=60)
    backdated = session.last_seen_at

    session.touch()

    assert session.last_seen_at > backdated
    # The updated timestamp must be very close to now (within 2 s)
    delta = (_utc_now() - session.last_seen_at).total_seconds()
    assert delta < 2.0


# ---------------------------------------------------------------------------
# Test 5 — is_expired() logic
# ---------------------------------------------------------------------------


def test_is_expired_false_immediately_after_creation() -> None:
    """is_expired(60) is False right after a session is created."""
    store = _make_store()
    session = _create_session(store)
    assert session.is_expired(60) is False


def test_is_expired_true_when_backdated_beyond_threshold() -> None:
    """is_expired(60) is True when last_seen_at is 120 seconds in the past."""
    store = _make_store()
    session = _create_session(store)
    session.last_seen_at = _utc_now() - timedelta(seconds=120)
    assert session.is_expired(60) is True


# ---------------------------------------------------------------------------
# Test 6 — delete() return value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_existing_returns_true() -> None:
    """delete() for an existing session_id must return True."""
    store = _make_store()
    session = _create_session(store)

    result = await store.delete(session.session_id)

    assert result is True
    assert store.get(session.session_id) is None


@pytest.mark.asyncio
async def test_delete_unknown_returns_false() -> None:
    """delete() for a session_id that does not exist must return False."""
    store = _make_store()

    result = await store.delete("ghost-session-id")

    assert result is False


# ---------------------------------------------------------------------------
# Test 7 — prune_expired() removes only expired sessions; returns count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_expired_removes_only_expired() -> None:
    """prune_expired() must remove sessions beyond max_idle and leave live ones."""
    store = _make_store(max_idle_seconds=60)

    live = _create_session(store, user_sub="live-user")
    expired1 = _create_session(store, user_sub="expired-user-1")
    expired2 = _create_session(store, user_sub="expired-user-2")

    # Backdate the two expired sessions
    expired1.last_seen_at = _utc_now() - timedelta(seconds=120)
    expired2.last_seen_at = _utc_now() - timedelta(seconds=90)

    pruned = await store.prune_expired()

    assert pruned == 2
    assert store.get(live.session_id) is live
    assert store.get(expired1.session_id) is None
    assert store.get(expired2.session_id) is None


@pytest.mark.asyncio
async def test_prune_expired_returns_zero_when_nothing_to_prune() -> None:
    """prune_expired() must return 0 when all sessions are still live."""
    store = _make_store(max_idle_seconds=3600)
    _create_session(store, user_sub="u1")
    _create_session(store, user_sub="u2")

    pruned = await store.prune_expired()

    assert pruned == 0


# ---------------------------------------------------------------------------
# Test 8 — find_pending_ciba() finds across multiple sessions
# ---------------------------------------------------------------------------


def test_find_pending_ciba_finds_correct_session() -> None:
    """find_pending_ciba() must locate a PendingCIBA keyed by auth_req_id across sessions."""
    store = _make_store()
    session_a = _create_session(store, user_sub="user-a")
    session_b = _create_session(store, user_sub="user-b")

    pending_b = PendingCIBA(
        auth_req_id="ciba-req-b-001",
        agent_id="hr_agent",
        request_id="req-b-001",
        started_at=_utc_now(),
    )
    session_a.pending_ciba["ciba-req-a-001"] = PendingCIBA(
        auth_req_id="ciba-req-a-001",
        agent_id="it_agent",
        request_id="req-a-001",
        started_at=_utc_now(),
    )
    session_b.pending_ciba["ciba-req-b-001"] = pending_b

    result = store.find_pending_ciba("ciba-req-b-001")

    assert result is not None
    found_session, found_pending = result
    assert found_session is session_b
    assert found_pending is pending_b
    assert found_pending.auth_req_id == "ciba-req-b-001"


# ---------------------------------------------------------------------------
# Test 9 — find_pending_ciba() returns None for missing auth_req_id
# ---------------------------------------------------------------------------


def test_find_pending_ciba_returns_none_when_not_found() -> None:
    """find_pending_ciba() must return None when no session holds the auth_req_id."""
    store = _make_store()
    session = _create_session(store)
    session.pending_ciba["ciba-abc"] = PendingCIBA(
        auth_req_id="ciba-abc",
        agent_id="hr_agent",
        request_id="req-001",
        started_at=_utc_now(),
    )

    result = store.find_pending_ciba("ciba-does-not-exist")

    assert result is None


def test_find_pending_ciba_returns_none_on_empty_store() -> None:
    """find_pending_ciba() on an empty store must return None."""
    store = _make_store()
    assert store.find_pending_ciba("ciba-xyz") is None


# ---------------------------------------------------------------------------
# Test 10 — cached_obo keying with frozenset
# ---------------------------------------------------------------------------


def test_cached_obo_keyed_by_agent_and_frozenset_scopes() -> None:
    """cached_obo keyed by (agent_id, frozenset(scopes)) stores and retrieves correctly."""
    store = _make_store()
    session = _create_session(store)

    obo = _make_obo_token()
    key: tuple[str, frozenset[str]] = ("hr_agent", frozenset({"openid", "hr.read"}))
    session.cached_obo[key] = obo

    # Retrieve with same key (different frozenset object, same contents)
    lookup_key: tuple[str, frozenset[str]] = ("hr_agent", frozenset({"hr.read", "openid"}))
    retrieved = session.cached_obo.get(lookup_key)

    assert retrieved is obo

    # Different scope set returns None
    wrong_key: tuple[str, frozenset[str]] = ("hr_agent", frozenset({"openid"}))
    assert session.cached_obo.get(wrong_key) is None

    # Different agent with same scopes returns None
    diff_agent_key: tuple[str, frozenset[str]] = ("it_agent", frozenset({"openid", "hr.read"}))
    assert session.cached_obo.get(diff_agent_key) is None


# ---------------------------------------------------------------------------
# Test 11 — concurrent create() calls produce distinct session_ids (no race)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_creates_produce_distinct_session_ids() -> None:
    """Concurrent create() calls under asyncio.gather must produce all distinct session_ids."""
    store = _make_store()
    n = 50

    async def _create(idx: int) -> Session:
        return store.create(
            user_sub=f"user-{idx:04d}",
            user_label=f"User {idx}",
            token_a=_make_token_a(access_token=f"tok-{idx}"),
        )

    sessions = await asyncio.gather(*[_create(i) for i in range(n)])

    session_ids = [s.session_id for s in sessions]
    # All session_ids are distinct
    assert len(set(session_ids)) == n
    # Every session is reachable in the store
    for s in sessions:
        assert store.get(s.session_id) is s


# ---------------------------------------------------------------------------
# Test 12 — IssuedTokenRecord dataclass round-trips via field assignment
# ---------------------------------------------------------------------------


def test_issued_token_record_field_assignment_round_trip() -> None:
    """IssuedTokenRecord must accept direct field values and expose them correctly."""
    record = IssuedTokenRecord(
        session_id="sess-uuid-abc",
        agent_id="hr_agent",
        jti="jti-deadbeef",
        exp=1_700_000_060,
        iat=1_700_000_000,
        auth_req_id="ciba-req-001",
    )

    assert record.session_id == "sess-uuid-abc"
    assert record.agent_id == "hr_agent"
    assert record.jti == "jti-deadbeef"
    assert record.exp == 1_700_000_060
    assert record.iat == 1_700_000_000
    assert record.auth_req_id == "ciba-req-001"

    # Mutation is allowed (not frozen)
    record.jti = "jti-updated"
    assert record.jti == "jti-updated"

    # Can be appended to a completed_ciba_log list
    store = _make_store()
    session = _create_session(store)
    session.completed_ciba_log.append(record)
    assert len(session.completed_ciba_log) == 1
    assert session.completed_ciba_log[0] is record


# ---------------------------------------------------------------------------
# Test 13 — get_or_404() touches session when found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_404_touches_session_when_found() -> None:
    """get_or_404() must update last_seen_at on the returned session."""
    store = _make_store()
    session = _create_session(store)

    # Backdate so we can observe the touch
    session.last_seen_at = _utc_now() - timedelta(seconds=300)
    backdated = session.last_seen_at

    retrieved = await store.get_or_404(session.session_id)

    assert retrieved is session
    assert session.last_seen_at > backdated
    delta = (_utc_now() - session.last_seen_at).total_seconds()
    assert delta < 2.0


# ---------------------------------------------------------------------------
# Test 14 — pending logout reasons: record + consume
# ---------------------------------------------------------------------------


def test_pending_logout_reason_record_and_consume_round_trip() -> None:
    """record + consume returns the same string; consume clears the entry."""
    store = _make_store()
    store.record_pending_logout_reason("user-001", "admin_terminated")
    assert store.consume_pending_logout_reason("user-001") == "admin_terminated"
    # Second consume returns None — single-use semantics.
    assert store.consume_pending_logout_reason("user-001") is None


def test_pending_logout_reason_overwrite_keeps_latest() -> None:
    """Recording twice for the same user keeps the latest reason."""
    store = _make_store()
    store.record_pending_logout_reason("user-001", "user_signed_out")
    store.record_pending_logout_reason("user-001", "admin_terminated")
    assert store.consume_pending_logout_reason("user-001") == "admin_terminated"


def test_pending_logout_reason_hard_cap_evicts_oldest() -> None:
    """Hardening: hard cap prevents unbounded growth; FIFO-evicts on overflow."""
    store = _make_store()
    # Shrink the cap for the test so we don't have to add 10k entries.
    store._PENDING_LOGOUT_REASONS_HARD_CAP = 3  # noqa: SLF001 — test-only
    store.record_pending_logout_reason("u1", "user_signed_out")
    store.record_pending_logout_reason("u2", "user_signed_out")
    store.record_pending_logout_reason("u3", "user_signed_out")
    store.record_pending_logout_reason("u4", "admin_terminated")  # evicts u1
    assert store.consume_pending_logout_reason("u1") is None  # evicted
    assert store.consume_pending_logout_reason("u4") == "admin_terminated"


def test_pending_logout_reason_sweep_drops_old_entries() -> None:
    """Hardening: sweeper drops entries older than the TTL."""
    import time as _time
    store = _make_store()
    store._PENDING_LOGOUT_REASONS_TTL_SECONDS = 60  # noqa: SLF001 — test-only
    store.record_pending_logout_reason("user-001", "admin_terminated")
    # Simulate the entry being old by sweeping with a now value 2 hrs in the future.
    removed = store.sweep_pending_logout_reasons(now=_time.time() + 7200)
    assert removed == 1
    assert store.consume_pending_logout_reason("user-001") is None


# ---------------------------------------------------------------------------
# S5.6 — Session.record_chat_turn / history_snapshot
# ---------------------------------------------------------------------------


def test_record_chat_turn_appends_in_order() -> None:
    store = _make_store()
    s = _create_session(store)
    assert s.chat_history == []
    s.record_chat_turn("user", "how much leave do I have")
    s.record_chat_turn("assistant", "you have 20 annual days")
    assert s.chat_history == [
        ("user", "how much leave do I have"),
        ("assistant", "you have 20 annual days"),
    ]
    # history_snapshot is a copy — mutating it doesn't touch the session.
    snap = s.history_snapshot()
    snap.append(("user", "leak"))
    assert s.chat_history[-1] == ("assistant", "you have 20 annual days")


def test_record_chat_turn_trims_to_last_max() -> None:
    cap = _store_mod._MAX_CHAT_HISTORY
    store = _make_store()
    s = _create_session(store)
    total = cap + 10
    for i in range(total):
        s.record_chat_turn("user" if i % 2 == 0 else "assistant", f"msg-{i}")
    assert len(s.chat_history) == cap
    # The oldest were dropped; the newest `cap` are kept, in order.
    assert [t[1] for t in s.chat_history] == [f"msg-{i}" for i in range(total - cap, total)]
