"""In-memory session store for the orchestrator.

Boundary rule (F-09): this module holds asyncio.Task, asyncio.Event, and
asyncio.Queue instances — it MUST use @dataclass, never Pydantic BaseModel.

Design notes
------------
- Sessions are keyed by an opaque ``session_id`` cookie value (UUID4 string).
- The single-process constraint (Q5) means a plain dict + asyncio.Lock is
  sufficient; no Redis or external state.
- ``PendingCIBA`` lives on the ``Session`` so the orchestrator can cancel a
  specific flow (``POST /api/ciba/cancel``) without a full session scan.
- ``IssuedTokenRecord`` (S1.11a) accumulates one record per successful CIBA
  completion.  Sprint 3 token-revocation walks ``Session.completed_ciba_log``.
- ``find_pending_ciba()`` is intentionally sync (no lock) because it is only
  called from the chat route while holding the broader request context and
  the dict mutation is always from the same event-loop thread.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from common.auth.models import OAuthToken, OBOToken  # noqa: F401 — public re-export

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=timezone.utc)


# S5.6: how many recent chat turns (user + assistant messages, interleaved) to
# keep per session and replay into the LLM router/composer prompts. 12 ≈ 6
# exchanges — enough for follow-ups like "it will be a sick leave", bounded so
# the prompt and the session memory stay small.
_MAX_CHAT_HISTORY = 12


# ---------------------------------------------------------------------------
# PendingCIBA
# ---------------------------------------------------------------------------


@dataclass
class PendingCIBA:
    """In-flight CIBA consent tracked by the orchestrator.

    Lifecycle
    ---------
    1. Created when the specialist returns a ``ConsentRequiredPayload``.
    2. ``poll_task`` is attached by ``chat/routes.py`` (Wave 7) after the SSE
       ``ciba_url`` event is pushed to the SPA.
    3. On completion/cancellation ``poll_task`` is set to ``None`` (F-10 rule 3)
       and ``status`` is updated to ``"done"`` / ``"denied"`` / ``"expired"`` /
       ``"cancelled"``.
    4. ``cancel_event`` is set by ``POST /api/ciba/cancel``; the poll task
       monitors it and raises ``CIBATimeoutError("cancelled")``.

    Attributes:
        auth_req_id: IS-issued identifier from ``/oauth2/ciba`` initiation.
        agent_id: Specialist agent identifier (e.g. ``"hr_agent"``).
        request_id: Orchestrator-level correlation ID (``X-Request-ID``).
        started_at: UTC timestamp when this pending flow was created.
        poll_task: Background asyncio Task polling for the OBO token.  Set by
            ``chat/routes.py``; nulled after completion per F-10 rule 3.
        cancel_event: Fires when the user cancels via ``POST /api/ciba/cancel``.
        status: Lifecycle state — ``"pending"`` | ``"done"`` | ``"denied"`` |
            ``"expired"`` | ``"cancelled"``.
    """

    auth_req_id: str
    agent_id: str
    request_id: str
    started_at: datetime
    poll_task: asyncio.Task | None = None  # set by chat/routes; nulled after done (F-10)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled_ack: asyncio.Event = field(default_factory=asyncio.Event)  # 3A.1 BLOCK-F: poll loop sets in finally
    status: str = "pending"  # pending | done | denied | expired | cancelled


# ---------------------------------------------------------------------------
# IssuedTokenRecord  (S1.11a Sprint 3 hook)
# ---------------------------------------------------------------------------


@dataclass
class IssuedTokenRecord:
    """Session-map entry recording one successful CIBA completion.

    Sprint 3 revocation fans out cache-bust signals by walking
    ``Session.completed_ciba_log``.  This module merely defines the shape;
    ``chat/routes.py`` (Wave 7) appends records.

    Attributes:
        session_id: Cookie value of the session that approved consent.
        agent_id: Specialist that performed the CIBA flow.
        jti: JWT ID of the issued OBO token (required — see F-08).
        exp: Token expiry as Unix epoch seconds (int).
        iat: Token issuance time as Unix epoch seconds (int).
        auth_req_id: IS auth_req_id that produced this token.
    """

    session_id: str
    agent_id: str
    jti: str
    exp: int   # Unix epoch seconds
    iat: int   # Unix epoch seconds
    auth_req_id: str


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """State for one authenticated browser session.

    One ``Session`` object exists per ``orch_sid`` cookie value.  It holds
    all mutable state that the orchestrator needs to serve the user across
    the three main request types: Pattern-C exchange, SSE streaming, and
    chat/CIBA dispatch.

    Attributes:
        session_id: Cookie value; also used as the SSE path param.
        user_sub: The user's OIDC ``sub`` claim from token-A. With the OAuth
            apps set to email-subject (S5.12) this is the user's email for
            users with an ``emailaddress`` attribute, the user-id UUID
            otherwise — and it matches the ``sub`` on the per-agent CIBA/OBO
            tokens (token-C), so per-user state keys consistently.
        user_label: Display name extracted from the id_token.
        token_a: Raw orchestrator session token (Pattern C result).
        pkce_state: CSRF-prevention state set during ``GET /auth/login``;
            cleared after ``POST /auth/exchange`` succeeds.
        code_verifier: PKCE code verifier paired with ``pkce_state``.
        sse_queue: asyncio.Queue for pushing SSE events to the SPA via
            ``GET /events/{session_id}``.
        pending_ciba: In-flight CIBA flows keyed by ``auth_req_id``.
        cached_obo: Keyed by ``(agent_id, frozenset(scopes))``; stores
            previously-obtained OBO tokens so re-CIBA is skipped while valid.
        completed_ciba_log: S1.11a list — one ``IssuedTokenRecord`` per
            successful CIBA completion.  Sprint 3 revocation hook.
        created_at: UTC timestamp of session creation.
        last_seen_at: UTC timestamp of most recent ``touch()`` call.
    """

    session_id: str
    user_sub: str
    user_label: str
    token_a: OAuthToken
    pkce_state: str | None
    code_verifier: str | None
    sse_queue: asyncio.Queue
    pending_ciba: dict[str, PendingCIBA] = field(default_factory=dict)
    cached_obo: dict[tuple[str, frozenset[str]], OBOToken] = field(default_factory=dict)
    completed_ciba_log: list[IssuedTokenRecord] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utc_now)
    last_seen_at: datetime = field(default_factory=_utc_now)
    terminating: bool = False  # 3A.1 BLOCK-G: first mutation in logout cascade — gates new chat/CIBA
    # 3B.2 FIX-17: reason this session's predecessor ended ("user_signed_out"
    # / "admin_terminated"). Set by Pattern C exchange via
    # ``SessionStore.consume_pending_logout_reason``; consumed once by
    # the chat fan-out path on the next CIBA, then nulled.
    last_logout_reason: str | None = None
    # S5.6: rolling transcript for LLM-mode follow-ups ("it will be a sick
    # leave" needs the prior turns to make sense). List of (role, text) where
    # role is "user" | "assistant"; trimmed to the last _MAX_CHAT_HISTORY
    # entries by record_chat_turn(). Not used by the keyword router. The
    # reply text is plain (rendered via textContent on the SPA), so storing
    # it is safe; it never leaves the orchestrator except into the OpenAI
    # prompts in llm-mode (same trust boundary as the user's messages).
    chat_history: list[tuple[str, str]] = field(default_factory=list)

    def touch(self) -> None:
        """Update ``last_seen_at`` to the current UTC time."""
        self.last_seen_at = _utc_now()

    def record_chat_turn(self, role: str, text: str) -> None:
        """Append a chat turn and trim to the last ``_MAX_CHAT_HISTORY`` entries."""
        self.chat_history.append((role, text))
        if len(self.chat_history) > _MAX_CHAT_HISTORY:
            del self.chat_history[:-_MAX_CHAT_HISTORY]

    def history_snapshot(self) -> list[tuple[str, str]]:
        """Return a shallow copy of the current chat history (for passing to the LLM)."""
        return list(self.chat_history)

    def is_expired(self, max_idle_seconds: int) -> bool:
        """Return ``True`` if the session has been idle longer than *max_idle_seconds*.

        Args:
            max_idle_seconds: Allowed seconds of inactivity before expiry.

        Returns:
            ``True`` when ``(now - last_seen_at) > max_idle_seconds``.
        """
        idle = (_utc_now() - self.last_seen_at).total_seconds()
        return idle > max_idle_seconds


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


@dataclass
class SessionStore:
    """In-memory session store keyed by ``session_id`` cookie value.

    Single-process per Q5 — a plain asyncio.Lock guards all mutations.
    Adapted from ``_archive/agent.before-v3/session.py:SessionStore``; key
    changed from ``user_sub`` to ``session_id``, OBO fields replaced with CIBA
    fields.

    Attributes:
        max_idle_seconds: Sessions idle longer than this are eligible for
            ``prune_expired()``.  Defaults to 3600 (1 hour).
        _sessions: Internal dict mapping ``session_id`` → ``Session``.
        _lock: asyncio.Lock protecting all mutations.
    """

    max_idle_seconds: int = 3600
    # Hardening sprint: bounds for _pending_logout_reasons (post-3B.3 retro
    # finding — the original "bounded by user count" claim doesn't survive
    # a long-running process).
    _PENDING_LOGOUT_REASONS_HARD_CAP: int = 10_000
    _PENDING_LOGOUT_REASONS_TTL_SECONDS: int = 86_400  # 24 h
    _sessions: dict[str, Session] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 3A.1 FIX-12: per-user_sub lock serialises concurrent UC-09 and UC-10 cascades.
    # Created lazily on first request via get_user_lock().
    _user_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # 3B.1 BLOCK-C #9: OIDC ``id_token.sid`` → ``user_sub`` reverse index. The
    # BCL receiver consults this when ``logout_token`` carries ``sid`` but no
    # ``sub``. Populated at code-exchange time from the freshly-issued
    # id_token's ``sid`` claim, evicted on session delete.
    _sid_to_user_sub: dict[str, str] = field(default_factory=dict)
    # 3B.2 FIX-17: ``user_sub`` → (logout_reason, recorded_at_epoch).
    # Set by the logout cascade just before session deletion, consumed
    # once by the next Pattern C exchange so the next CIBA can render a
    # reason-aware binding message.
    #
    # Hardening sprint (post-3B.3 retro): the original "bounded by user
    # count, no explicit eviction" claim doesn't survive a long-running
    # process where users sign out and never return. Now hard-capped +
    # FIFO-evicted + age-swept on the same model as
    # ``common.revocation.SeenLogoutTokens``. Cleared on consume.
    _pending_logout_reasons: "OrderedDict[str, tuple[str, float]]" = field(
        default_factory=OrderedDict
    )

    # ------------------------------------------------------------------
    # Sync helpers (read-only; safe without lock on a single event loop)
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        user_sub: str,
        user_label: str,
        token_a: OAuthToken,
    ) -> Session:
        """Create and register a new ``Session``; return it.

        A new UUID4 ``session_id`` is generated.  ``pkce_state`` and
        ``code_verifier`` start as ``None``; ``sse_queue`` is a fresh
        ``asyncio.Queue``.

        Args:
            user_sub: The user's OIDC ``sub`` claim from token-A (email-form
                for users with an ``emailaddress`` attribute, user-id UUID
                otherwise — see ``docs/architecture/identity-subject-mismatch.md``).
            user_label: Display name for SSE ``session_ready`` event.
            token_a: Pattern-C token-A for this session.

        Returns:
            The newly created ``Session`` instance.
        """
        session_id = str(uuid.uuid4())
        session = Session(
            session_id=session_id,
            user_sub=user_sub,
            user_label=user_label,
            token_a=token_a,
            pkce_state=None,
            code_verifier=None,
            sse_queue=asyncio.Queue(),
        )
        self._sessions[session_id] = session
        logger.info(
            "[SESSION] created session_id=%s user_sub=%s (total=%d)",
            session_id,
            user_sub,
            len(self._sessions),
        )
        return session

    def get(self, session_id: str) -> Session | None:
        """Return the ``Session`` for *session_id*, or ``None`` if not found.

        Does NOT call ``touch()`` — use ``get_or_404()`` for request handlers
        that should refresh idle timers.

        Args:
            session_id: Cookie value to look up.

        Returns:
            The ``Session`` if present, otherwise ``None``.
        """
        return self._sessions.get(session_id)

    def find_pending_ciba(
        self, auth_req_id: str
    ) -> tuple[Session, PendingCIBA] | None:
        """Search all sessions for a ``PendingCIBA`` matching *auth_req_id*.

        Used by ``POST /api/ciba/cancel`` to locate which session owns the
        pending flow without knowing the session_id upfront.

        Args:
            auth_req_id: IS-issued CIBA identifier to search for.

        Returns:
            ``(Session, PendingCIBA)`` if found; ``None`` otherwise.
        """
        for session in self._sessions.values():
            pending = session.pending_ciba.get(auth_req_id)
            if pending is not None:
                return session, pending
        return None

    # ------------------------------------------------------------------
    # Async mutating operations (protected by _lock)
    # ------------------------------------------------------------------

    async def get_or_404(self, session_id: str) -> Session:
        """Return the session for *session_id*, touching it; raise ``KeyError`` if absent.

        async because the lock is acquired to guarantee visibility in a
        future multi-writer scenario and to keep the API honest.

        Args:
            session_id: Cookie value to look up.

        Returns:
            The live ``Session`` with ``last_seen_at`` updated.

        Raises:
            KeyError: If *session_id* is not in the store.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            session.touch()
            return session

    async def delete(self, session_id: str) -> bool:
        """Remove the session for *session_id*.

        Args:
            session_id: Cookie value to remove.

        Returns:
            ``True`` if the session existed and was removed; ``False`` if it
            was not found.
        """
        async with self._lock:
            existed = session_id in self._sessions
            if existed:
                del self._sessions[session_id]
                logger.info("[SESSION] deleted session_id=%s", session_id)
            return existed

    def get_user_lock(self, user_sub: str) -> asyncio.Lock:
        """Return the per-user_sub asyncio.Lock for the revocation cascade.

        Creates one lazily on first call. Same Lock is returned on subsequent
        calls for the same user_sub. Used by ``logout_handler`` (UC-09) and
        ``bcl_receiver`` (UC-10, Sprint 3B) to serialise cascades for the
        same user — see Stage 4 FIX-12.

        BLOCK-C (mid-sprint review): use ``setdefault`` so two coroutines
        racing on the same ``user_sub`` cannot both create a Lock and end up
        with one of them holding an orphan. ``setdefault`` is a single
        atomic dict op under CPython's GIL — safe on a single event loop.

        Args:
            user_sub: User UUID (claims.sub) to acquire a lock for.

        Returns:
            The asyncio.Lock for *user_sub*.
        """
        return self._user_locks.setdefault(user_sub, asyncio.Lock())

    def find_sessions_for_user(self, user_sub: str) -> list[Session]:
        """Return all sessions owned by *user_sub* (multi-browser case).

        Sync — read-only iteration over the dict; safe on the single
        event-loop thread per Q5. Used by the logout cascade to fan out
        across all open sessions for the same user.

        Args:
            user_sub: User UUID to search for.

        Returns:
            List of ``Session`` objects (possibly empty).
        """
        return [s for s in self._sessions.values() if s.user_sub == user_sub]

    # ------------------------------------------------------------------
    # 3B.1 BLOCK-C #9 — sid → user_sub reverse index for BCL receiver
    # ------------------------------------------------------------------

    def register_sid(self, sid: str, user_sub: str) -> None:
        """Map an OIDC ``id_token.sid`` to the owning ``user_sub``.

        Called by ``POST /auth/exchange`` immediately after a session is
        created. Many IS deployments include ``sid`` in id_tokens; some
        BCL ``logout_token`` events carry ``sid`` only (no ``sub``). The
        receiver consults this index to translate.

        Idempotent — registering the same sid twice for the same user is
        a no-op; for a different user it is an error (collision should be
        impossible given IS issues globally-unique sids per session).

        Args:
            sid: The ``sid`` claim from the freshly issued id_token.
            user_sub: The session's ``user_sub`` (id_token ``sub`` claim).
        """
        existing = self._sid_to_user_sub.get(sid)
        if existing is not None and existing != user_sub:
            logger.error(
                "[SESSION] sid_collision sid=%s prev_user=%s new_user=%s — refusing",
                sid,
                existing,
                user_sub,
            )
            return
        self._sid_to_user_sub[sid] = user_sub

    def resolve_sid(self, sid: str) -> str | None:
        """Return the ``user_sub`` for a given OIDC ``sid``, or ``None``.

        Sync — read-only dict lookup, safe on the single event loop. Used
        by the BCL receiver when ``logout_token.sub`` is absent.
        """
        return self._sid_to_user_sub.get(sid)

    # ------------------------------------------------------------------
    # 3B.2 FIX-17 — logout-reason hand-off across re-login
    # ------------------------------------------------------------------

    def record_pending_logout_reason(self, user_sub: str, reason: str) -> None:
        """Stash the logout reason so the user's next re-login can read it.

        Called by the logout cascade just before session deletion.
        Overwrites any previous unconsumed entry for the same user
        (admin-terminate after user-signed-out → admin-terminate wins —
        most specific reason).

        Hard-capped + FIFO-evicted + age-swept (hardening sprint after
        3B.3). The cap protects against unbounded growth in a long-
        running process where users sign out and never return.

        Args:
            user_sub: User UUID being logged out.
            reason: ``"user_signed_out"`` or ``"admin_terminated"``.
                Other values are accepted but ignored downstream.
        """
        # Refresh recency ordering when overwriting (move-to-end).
        if user_sub in self._pending_logout_reasons:
            self._pending_logout_reasons.move_to_end(user_sub, last=True)
            self._pending_logout_reasons[user_sub] = (reason, time.time())
            return
        if len(self._pending_logout_reasons) >= self._PENDING_LOGOUT_REASONS_HARD_CAP:
            evicted_sub, _ = self._pending_logout_reasons.popitem(last=False)
            logger.warning(
                "pending_logout_reasons_evicted_for_capacity | user_sub_prefix=%s "
                "reason=hard_cap cap=%d",
                evicted_sub[:8],
                self._PENDING_LOGOUT_REASONS_HARD_CAP,
            )
        self._pending_logout_reasons[user_sub] = (reason, time.time())

    def consume_pending_logout_reason(self, user_sub: str) -> str | None:
        """Pop and return the pending logout reason for *user_sub*.

        Called once by the Pattern C exchange when a fresh session is
        being created. Returns ``None`` if no reason was recorded
        (e.g. first-ever login, or pending entry already consumed,
        or the entry aged out via the periodic sweep).
        """
        entry = self._pending_logout_reasons.pop(user_sub, None)
        return entry[0] if entry is not None else None

    def sweep_pending_logout_reasons(self, *, now: float | None = None) -> int:
        """Drop entries older than ``_PENDING_LOGOUT_REASONS_TTL_SECONDS``.

        A logout reason that's never consumed (user never returned) is
        still useful copy for ~24 h, but persisting beyond that adds
        nothing — by then the next sign-in is a fresh story. Returns
        the count removed for observability.
        """
        cutoff = (now if now is not None else time.time()) - self._PENDING_LOGOUT_REASONS_TTL_SECONDS
        expired = [k for k, (_, t) in self._pending_logout_reasons.items() if t < cutoff]
        for k in expired:
            del self._pending_logout_reasons[k]
        return len(expired)

    async def prune_expired(self) -> int:
        """Remove all sessions that have been idle longer than ``max_idle_seconds``.

        Returns:
            Number of sessions pruned.
        """
        async with self._lock:
            stale = [
                sid
                for sid, s in self._sessions.items()
                if s.is_expired(self.max_idle_seconds)
            ]
            for sid in stale:
                del self._sessions[sid]
            if stale:
                logger.info(
                    "[SESSION] pruned %d expired session(s) (max_idle=%ds)",
                    len(stale),
                    self.max_idle_seconds,
                )
            return len(stale)
