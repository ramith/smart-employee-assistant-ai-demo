"""FastAPI router factory for the two-phase A2A server endpoints.

Shared by hr_agent and it_agent.  Mount the returned ``APIRouter`` on the
specialist's FastAPI app; the three endpoints implement the two-call A2A pattern
defined in sprint-1-fixes.md F-01.

Protocol summary (F-01)
-----------------------
  POST /a2a/message/send  — validate token-A, dispatch to specialist, return
                             ConsentRequiredPayload (or ResultPayload / ErrorPayload)
  POST /a2a/await         — long-poll the in-process asyncio.Event until the CIBA
                             polling task completes; return ResultPayload or ErrorPayload
  POST /a2a/cancel        — signal the background poll_task to abort

Boundary rule (F-09)
--------------------
- ``A2APendingState`` holds ``asyncio.Task`` and ``asyncio.Event`` so it MUST be a
  ``@dataclass``, NOT a Pydantic ``BaseModel``.
- ``A2ARouterConfig`` is also a ``@dataclass`` (frozen) for the same reason.
- All HTTP-boundary types (ConsentRequiredPayload, ResultPayload, ErrorPayload,
  CancelResponse, …) are Pydantic ``BaseModel``s defined in ``common/a2a/models.py``.

asyncio.Task defensive rules (F-10)
------------------------------------
- ``add_done_callback`` wiring is the dispatcher's responsibility; this module only
  reads ``state.completion`` and ``state.result`` / ``state.error``.
- ``CancelledError`` is ``BaseException``; this module never catches it.
- ``asyncio.wait_for`` is used with an explicit timeout; ``asyncio.TimeoutError`` is
  caught and mapped to ``ErrorPayload(error_id="ERR-CIBA-010")``.

Token validation errors are mapped per the JSON-RPC error code table in
``common/a2a/jsonrpc.py``:
  -32002  ERR_INVALID_TOKEN_A  — JWT validation failure
  -32001  ERR_PEER_NOT_TRUSTED — act chain peer not trusted
  -32600  INVALID_REQUEST      — bad JSON-RPC envelope or missing params
  -32004  ERR_TOOL_NOT_FOUND   — auth_req_id not in pending map

F-16 (auto-generate missing X-Request-ID with WARN) is implemented; Sprint 2 may
tighten to reject on missing header.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Request
from pydantic import ValidationError

from common.a2a.jsonrpc import (
    ERR_INVALID_TOKEN_A,
    ERR_PEER_NOT_TRUSTED,
    ERR_TOOL_NOT_FOUND,
    INVALID_REQUEST,
    make_error,
    make_success,
    parse_request,
)
from common.a2a.models import (
    A2AMessageResponse,
    AwaitRequest,
    CancelRequest,
    CancelResponse,
    ConsentRequiredPayload,
    ErrorPayload,
    MessageSendParams,
    ResultPayload,
)
from common.auth.errors import JWTValidationError, PeerTrustError
from common.auth.jwt_validator import JWKSCache, ValidatorConfig, validate
from common.auth.models import JWTClaims
from common.auth.peer_trust import validate_chain

logger = logging.getLogger(__name__)

__all__ = [
    "A2APendingState",
    "DispatchProtocol",
    "A2ARouterConfig",
    "build_a2a_router",
]


# ---------------------------------------------------------------------------
# In-flight CIBA state (dataclass — holds asyncio types, F-09)
# ---------------------------------------------------------------------------


@dataclass
class A2APendingState:
    """In-process map entry for an in-flight CIBA-driven A2A request.

    Owned by the specialist process; this module manages insertion (via
    ``pending_register``) and deletion (after a successful await).  The
    specialist's ``add_done_callback`` is responsible for populating
    ``result`` / ``error`` and calling ``completion.set()``.

    Attributes:
        auth_req_id: Opaque CIBA request identifier issued by IS; used as the map key.
        request_id: X-Request-ID echo from the originating ``message/send`` call.
        started_at: Wall-clock time when this entry was created.
        poll_task: The background asyncio.Task[OAuthToken] owned by the specialist;
            ``None`` until the dispatcher wires it up, and reset to ``None`` inside
            the done-callback (F-10 rule 3).
        completion: Set by ``add_done_callback`` when the poll task finishes.
        cancel_event: Set by ``/a2a/cancel`` to signal the poll_task to abort.
        result: Populated by the done-callback on task success.
        error: Populated by the done-callback on task failure.
    """

    auth_req_id: str
    request_id: str
    started_at: datetime
    poll_task: asyncio.Task[Any] | None  # asyncio.Task[OAuthToken] in practice
    completion: asyncio.Event
    cancel_event: asyncio.Event
    result: ResultPayload | None = None
    error: ErrorPayload | None = None


# ---------------------------------------------------------------------------
# DispatchProtocol — implemented by each specialist
# ---------------------------------------------------------------------------


class DispatchProtocol(Protocol):
    """Interface the specialist must implement and pass as ``A2ARouterConfig.dispatch``.

    The router calls this once per ``message/send`` request after token-A has
    been validated and the peer chain verified.  The dispatcher decides whether
    to:

    - Return a ``ResultPayload`` immediately (rare; only when no consent is needed).
    - Initiate a CIBA flow, register an ``A2APendingState`` via ``pending_register``,
      start a background asyncio.Task, and return ``ConsentRequiredPayload`` (typical).
    - Return ``ErrorPayload`` on hard failure (e.g. CIBA initiation error).

    Args:
        tool: MCP tool name extracted from ``MessageSendParams``.
        args: Tool keyword arguments.
        user_sub: ``sub`` claim from the validated token-A (the acting user).
        orchestrator_act_sub: ``act.sub`` from token-A (the delegating orchestrator
            agent UUID).
        request_id: X-Request-ID correlation value from the HTTP header.
        pending_register: Callable that inserts an ``A2APendingState`` into the
            specialist's ``pending`` dict when a CIBA flow is started.

    Returns:
        One of ``ConsentRequiredPayload``, ``ResultPayload``, or ``ErrorPayload``.
        The discriminant ``type`` field identifies which variant was returned.
    """

    async def __call__(
        self,
        *,
        tool: str,
        args: dict,
        user_sub: str,
        orchestrator_act_sub: str,
        request_id: str,
        pending_register: Callable[[A2APendingState], None],
        last_logout_reason: str | None = None,
    ) -> A2AMessageResponse: ...


# ---------------------------------------------------------------------------
# Router configuration (frozen dataclass — holds asyncio state via pending)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class A2ARouterConfig:
    """Configuration bundle injected into ``build_a2a_router``.

    Attributes:
        validator_config: JWT validator config for token-A signature / iss / exp / aud.
        trusted_orchestrator_subs: Peer-trust allowlist for ``token-A.act.sub``; must
            contain Asgardeo Agent identity UUIDs (not display names).
        pending: Mutable dict of in-flight CIBA states keyed by ``auth_req_id``;
            shared with the specialist for add/remove operations.
        dispatch: Specialist-provided callable; called from ``/a2a/message/send``.
        await_max_wait_seconds: Maximum seconds the ``/a2a/await`` handler will block
            on ``state.completion.wait()``.  Default 330s outlasts the IS default CIBA
            ``expires_in`` of 300s.
    """

    validator_config: ValidatorConfig
    trusted_orchestrator_subs: frozenset[str]
    pending: dict[str, A2APendingState]
    dispatch: DispatchProtocol
    await_max_wait_seconds: float = 330.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_bearer(authorization: str | None) -> str | None:
    """Return the raw token string from an ``Authorization: Bearer <token>`` header.

    Returns ``None`` when the header is absent or not a Bearer scheme.
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def _validate_token_and_peer(
    authorization: str | None,
    config: A2ARouterConfig,
    request_id: str | None,
    *,
    jwks_cache: JWKSCache | None,
) -> tuple[JWTClaims, None] | tuple[None, Any]:
    """Validate Bearer token-A and its act-chain peer trust.

    Returns ``(claims, None)`` on success or ``(None, error_response)`` on any
    failure.  Callers must check the second element before using the first.

    Validation order (F-01 / F-04):
    1. Extract Bearer token; missing → JSON-RPC -32002.
    2. ``jwt_validator.validate()``; JWTValidationError → -32002.
    3. ``peer_trust.validate_chain()``; PeerTrustError → -32001.
    """
    token = _extract_bearer(authorization)
    if token is None:
        return None, make_error(
            request_id,
            ERR_INVALID_TOKEN_A,
            "Authorization header missing or not Bearer scheme",
        ).model_dump()

    try:
        claims = await validate(token, config.validator_config, jwks_cache=jwks_cache)
    except JWTValidationError as exc:
        logger.warning("a2a_token_validation_failed error_id=%s", exc.error_id)
        return None, make_error(
            request_id,
            ERR_INVALID_TOKEN_A,
            f"Token validation failed: {exc.message}",
            data={"error_id": exc.error_id},
        ).model_dump()

    try:
        validate_chain(
            claims,
            allowed_peers=config.trusted_orchestrator_subs,
            max_depth=1,
        )
    except PeerTrustError as exc:
        logger.warning("a2a_peer_trust_failed details=%s", exc.details)
        return None, make_error(
            request_id,
            ERR_PEER_NOT_TRUSTED,
            f"Peer trust validation failed: {exc.message}",
            data={"error_id": exc.error_id},
        ).model_dump()

    return claims, None


def _resolve_request_id(request: Request) -> str:
    """Return the X-Request-ID header value; auto-generate with WARN if absent (F-16)."""
    rid = request.headers.get("X-Request-ID") or request.headers.get("x-request-id")
    if not rid:
        rid = str(uuid.uuid4())
        logger.warning(
            "a2a_missing_x_request_id generated=%s path=%s",
            rid,
            request.url.path,
        )
    return rid


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_a2a_router(
    config: A2ARouterConfig,
    *,
    jwks_cache: JWKSCache | None = None,
) -> APIRouter:
    """Build a FastAPI ``APIRouter`` hosting the three A2A specialist endpoints.

    All three routes share the same token-A validation + peer-trust check;
    individual handler logic follows the F-01 two-phase protocol.

    Args:
        config: Router configuration including validator, allowlist, pending map,
            and dispatch callable.
        jwks_cache: Optional pre-built ``JWKSCache``; primarily for testing.
            When ``None`` the shared cache registry inside ``jwt_validator`` is used.

    Returns:
        Configured ``APIRouter`` ready to be included in a specialist FastAPI app.

    Endpoints:
        ``POST /a2a/message/send``  — step 1: validate + dispatch
        ``POST /a2a/await``         — step 2: block until CIBA completes
        ``POST /a2a/cancel``        — abort in-flight CIBA by auth_req_id
    """
    router = APIRouter()

    # ── POST /a2a/message/send ─────────────────────────────────────────────────

    @router.post("/a2a/message/send")
    async def message_send(request: Request) -> dict:  # type: ignore[return]
        """Validate token-A, parse the JSON-RPC envelope, and dispatch to the specialist.

        Returns a JSON-RPC 2.0 response whose ``result`` is one of:
        - ``ConsentRequiredPayload`` — CIBA flow started; orchestrator should
          push ``auth_url`` to the SPA then call ``/a2a/await``.
        - ``ResultPayload`` — immediate result (no consent required).
        - ``ErrorPayload`` — hard failure (e.g. CIBA initiation error).
        """
        request_id = _resolve_request_id(request)

        # ── Step 1: parse JSON-RPC envelope ───────────────────────────────────
        try:
            body: dict = await request.json()
        except Exception:
            return make_error(None, INVALID_REQUEST, "Request body is not valid JSON").model_dump()

        try:
            rpc_req = parse_request(body)
        except (ValidationError, TypeError, Exception):
            return make_error(
                body.get("id") if isinstance(body, dict) else None,
                INVALID_REQUEST,
                "Invalid JSON-RPC 2.0 request envelope",
            ).model_dump()

        if rpc_req.method != "message/send":
            return make_error(
                rpc_req.id,
                INVALID_REQUEST,
                f"Unsupported method: {rpc_req.method!r}; expected 'message/send'",
            ).model_dump()

        # ── Steps 2–3: token-A validation + peer trust ────────────────────────
        claims, err = await _validate_token_and_peer(
            request.headers.get("Authorization"),
            config,
            rpc_req.id,
            jwks_cache=jwks_cache,
        )
        if err is not None:
            return err

        # ── Step 4: parse params ───────────────────────────────────────────────
        params = rpc_req.params
        if not isinstance(params, dict):
            return make_error(
                rpc_req.id,
                INVALID_REQUEST,
                "params must be an object (dict), not a positional list",
            ).model_dump()

        try:
            msg_params = MessageSendParams.model_validate(params)
        except ValidationError as exc:
            return make_error(
                rpc_req.id,
                INVALID_REQUEST,
                f"Invalid message/send params: {exc}",
            ).model_dump()

        # ── Step 5: extract sub + act.sub ──────────────────────────────────────
        assert claims is not None  # guarded above
        user_sub: str = claims.sub
        # claims.act is a dict | None per JWTClaims; act_sub is not a property.
        orchestrator_act_sub: str = (
            claims.act.get("sub", "") if isinstance(claims.act, dict) else ""
        )

        # ── Step 6: build pending_register callback ────────────────────────────
        def pending_register(state: A2APendingState) -> None:
            config.pending[state.auth_req_id] = state

        # ── Step 7: dispatch ───────────────────────────────────────────────────
        dispatch_result = await config.dispatch(
            tool=msg_params.tool,
            args=msg_params.args,
            user_sub=user_sub,
            orchestrator_act_sub=orchestrator_act_sub,
            request_id=request_id,
            pending_register=pending_register,
            # 3B.2 FIX-17: pass through the orchestrator's recorded logout
            # reason. Specialist's CIBA dispatcher uses it to render a
            # reason-aware binding message.
            last_logout_reason=msg_params.last_logout_reason,
        )

        # ── Step 8: wrap in JSON-RPC response ────────────────────────────────
        return make_success(rpc_req.id, dispatch_result.model_dump()).model_dump()

    # ── POST /a2a/await ────────────────────────────────────────────────────────

    @router.post("/a2a/await")
    async def a2a_await(request: Request) -> dict:  # type: ignore[return]
        """Block until the CIBA polling task for ``auth_req_id`` completes.

        The orchestrator calls this after receiving ``ConsentRequiredPayload`` from
        ``/a2a/message/send``.  This handler long-polls ``state.completion`` for up
        to ``await_max_wait_seconds`` seconds.

        Returns a JSON-RPC 2.0 response whose ``result`` is either:
        - ``ResultPayload`` — polling succeeded; OBO token and MCP result.
        - ``ErrorPayload`` — denial, expiry, cancellation, or server timeout.

        On successful return the pending-state entry is removed from the map.
        On ``asyncio.TimeoutError`` the entry is also removed (no retry semantics
        in Sprint 1; caller should start a new CIBA flow).
        """
        request_id = _resolve_request_id(request)

        # ── Parse envelope ─────────────────────────────────────────────────────
        try:
            body: dict = await request.json()
        except Exception:
            return make_error(None, INVALID_REQUEST, "Request body is not valid JSON").model_dump()

        try:
            rpc_req = parse_request(body)
        except (ValidationError, TypeError, Exception):
            return make_error(
                body.get("id") if isinstance(body, dict) else None,
                INVALID_REQUEST,
                "Invalid JSON-RPC 2.0 request envelope",
            ).model_dump()

        # ── Token-A validation + peer trust ────────────────────────────────────
        claims, err = await _validate_token_and_peer(
            request.headers.get("Authorization"),
            config,
            rpc_req.id,
            jwks_cache=jwks_cache,
        )
        if err is not None:
            return err

        # ── Parse AwaitRequest from params ────────────────────────────────────
        params = rpc_req.params
        if not isinstance(params, dict):
            return make_error(rpc_req.id, INVALID_REQUEST, "params must be an object").model_dump()

        try:
            await_req = AwaitRequest.model_validate(params)
        except ValidationError as exc:
            return make_error(rpc_req.id, INVALID_REQUEST, f"Invalid await params: {exc}").model_dump()

        auth_req_id = await_req.auth_req_id

        # ── Look up pending state ──────────────────────────────────────────────
        state = config.pending.get(auth_req_id)
        if state is None:
            return make_error(
                rpc_req.id,
                ERR_TOOL_NOT_FOUND,
                f"No pending CIBA flow found for auth_req_id={auth_req_id!r}",
                data={"auth_req_id": auth_req_id},
            ).model_dump()

        # ── Long-poll with timeout ────────────────────────────────────────────
        try:
            await asyncio.wait_for(
                state.completion.wait(),
                timeout=config.await_max_wait_seconds,
            )
        except asyncio.TimeoutError:
            config.pending.pop(auth_req_id, None)
            timeout_payload = ErrorPayload(
                error_id="ERR-CIBA-010",
                reason="server_await_timeout",
            )
            return make_success(rpc_req.id, timeout_payload.model_dump()).model_dump()

        # ── Completion: read result or error ──────────────────────────────────
        config.pending.pop(auth_req_id, None)

        if state.result is not None:
            return make_success(rpc_req.id, state.result.model_dump()).model_dump()

        if state.error is not None:
            return make_success(rpc_req.id, state.error.model_dump()).model_dump()

        # Defensive fallback: completion was set but neither result nor error populated.
        fallback_err = ErrorPayload(
            error_id="ERR-AGENT-001",
            reason="completion_set_but_no_result",
        )
        return make_success(rpc_req.id, fallback_err.model_dump()).model_dump()

    # ── POST /a2a/cancel ──────────────────────────────────────────────────────

    @router.post("/a2a/cancel")
    async def a2a_cancel(request: Request) -> dict:  # type: ignore[return]
        """Signal the background CIBA polling task for ``auth_req_id`` to abort.

        Sets ``state.cancel_event`` which the specialist's ``poll_for_token`` loop
        observes; the done-callback will then write ``ErrorPayload`` into
        ``state.error`` and set ``state.completion``.

        Returns ``CancelResponse(cancelled=True)`` when the flow was found and
        signalled, or ``CancelResponse(cancelled=False, reason="not_found")`` when
        the ``auth_req_id`` is unknown (already finished, never started, or wrong id).

        Note: The response is NOT wrapped in a JSON-RPC envelope because this is a
        fire-and-forget side-channel, not a request–response tool call.  The
        orchestrator's ``POST /api/ciba/cancel`` handler does not need the JSON-RPC
        framing.
        """
        request_id = _resolve_request_id(request)

        # ── Token-A validation + peer trust ────────────────────────────────────
        try:
            body: dict = await request.json()
        except Exception:
            return CancelResponse(cancelled=False, reason="invalid_json").model_dump()

        # Attempt JSON-RPC envelope parse; cancel may be sent bare or wrapped.
        # We support both bare ``{"auth_req_id": "..."}`` and JSON-RPC wrapped.
        # If it has "jsonrpc" key → JSON-RPC envelope; extract params.
        if "jsonrpc" in body:
            try:
                rpc_req = parse_request(body)
            except Exception:
                return CancelResponse(cancelled=False, reason="invalid_rpc_envelope").model_dump()
            rpc_id = rpc_req.id
            raw_params: dict = rpc_req.params if isinstance(rpc_req.params, dict) else {}
        else:
            rpc_id = None
            raw_params = body

        # Token validation applies regardless of envelope style.
        claims, err = await _validate_token_and_peer(
            request.headers.get("Authorization"),
            config,
            rpc_id,
            jwks_cache=jwks_cache,
        )
        if err is not None:
            # Return auth failure as CancelResponse for consistent caller contract.
            return CancelResponse(cancelled=False, reason="auth_failed").model_dump()

        try:
            cancel_req = CancelRequest.model_validate(raw_params)
        except ValidationError:
            return CancelResponse(cancelled=False, reason="invalid_params").model_dump()

        auth_req_id = cancel_req.auth_req_id
        state = config.pending.get(auth_req_id)
        if state is None:
            logger.info("a2a_cancel_not_found auth_req_id=%s", auth_req_id)
            return CancelResponse(cancelled=False, reason="not_found").model_dump()

        state.cancel_event.set()
        logger.info("a2a_cancel_signalled auth_req_id=%s request_id=%s", auth_req_id, request_id)
        return CancelResponse(cancelled=True, reason="signal_sent").model_dump()

    return router
