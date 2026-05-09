"""orchestrator/auth/routes.py — FastAPI auth router for Pattern C login flow.

Implements the four auth endpoints described in
``docs/architecture/api-contracts.md`` §1 and the full Pattern C flow from
``docs/use-cases/UC-01-user-login.md``.

Boundary rules (sprint-1-fixes.md)
-----------------------------------
- F-09: ``AuthRouterDeps`` and ``PendingLogin`` are ``@dataclass`` (not Pydantic)
  because they hold runtime state.  ``ExchangeRequest`` and ``ExchangeResponse``
  are Pydantic v2 ``BaseModel`` because they cross HTTP boundaries.
- F-01: the code exchange is performed by ``PatternCExchanger``, which places
  ``actor_token`` in the POST body (not the Authorization header).

SPA callback relay design
--------------------------
``GET /auth/callback`` receives the IS redirect (code + state).  Rather than
redirecting the SPA to a ``/complete-login`` page that must then POST back, this
endpoint returns a small self-contained HTML page that fires a ``fetch`` to
``POST /auth/exchange`` and on success navigates ``window.location`` to ``/``.
This keeps the flow entirely within the orchestrator's domain and requires no
additional SPA route for Sprint 1.

Cookie shape
------------
Name: ``orch_sid``  (``config.session_cookie_name``)
Flags: HttpOnly, SameSite=Lax, Secure=``config.cookie_secure``, Max-Age=``config.session_ttl_seconds``
Value: opaque UUID4 session ID — token-A never leaves the orchestrator.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from orchestrator.auth.logout_handler import LogoutHandler
from orchestrator.auth.pattern_c import PatternCExchanger, build_authorize_url, make_pkce
from orchestrator.auth.session_store import SessionStore
from orchestrator.config import OrchestratorConfig

logger = logging.getLogger(__name__)

__all__ = [
    "AuthRouterDeps",
    "PendingLogin",
    "build_auth_router",
    "ExchangeRequest",
    "ExchangeResponse",
]


# ---------------------------------------------------------------------------
# Dataclasses (F-09 — runtime state, never serialised over HTTP)
# ---------------------------------------------------------------------------


@dataclass
class PendingLogin:
    """Short-lived record associating a PKCE state to its verifier and post-login destination.

    Keyed by ``state`` in ``AuthRouterDeps.pending_logins``.  Consumed (popped) by
    ``POST /auth/exchange`` and discarded.  TTL enforcement is not Sprint 1 scope;
    if the user abandons the flow the entry stays until process restart.

    Attributes:
        code_verifier: RFC 7636 PKCE code verifier generated at ``GET /auth/login``.
        redirect_after_login: URL the SPA should navigate to after the exchange succeeds.
            Defaults to ``"/"`` when no ``next`` query parameter is supplied.
        created_at: UTC timestamp of creation (for future pruning / TTL enforcement).
    """

    code_verifier: str
    redirect_after_login: str
    created_at: datetime


@dataclass
class AuthRouterDeps:
    """Dependency bag injected into ``build_auth_router``.

    Holds everything the auth router needs without reaching into global state.
    Using a dataclass (F-09) rather than Pydantic because ``SessionStore``
    contains asyncio primitives.

    Attributes:
        config: Orchestrator configuration.
        pattern_c: Stateful exchanger that performs the IS code-exchange.
        session_store: In-memory session store; one ``Session`` per authenticated user.
        pending_logins: Short-lived map of ``pkce_state → PendingLogin``.
            Pre-populated with an empty dict by default; replaced with a real shared
            instance at application startup.
    """

    config: OrchestratorConfig
    pattern_c: PatternCExchanger
    session_store: SessionStore
    logout_handler: LogoutHandler
    pending_logins: dict[str, PendingLogin] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pydantic models (F-09 — types that cross HTTP boundaries)
# ---------------------------------------------------------------------------


class ExchangeRequest(BaseModel):
    """Body for ``POST /auth/exchange``.

    The SPA supplies ``code`` and ``state`` only.  ``code_verifier`` was generated
    server-side and stored in ``pending_logins``; the client never holds it.
    """

    code: str
    state: str


class ExchangeResponse(BaseModel):
    """Success response from ``POST /auth/exchange``.

    The session cookie is set separately on the ``Response`` object.  The body
    additionally exposes ``session_id`` because the SPA needs it as a *path*
    parameter on the SSE URL (``/events/{session_id}``); the HttpOnly cookie
    cannot be read by JS, so the SPA stashes ``session_id`` in localStorage to
    survive page reloads.  ``user_display_name`` mirrors ``user_label`` under
    the field name the SPA reads.
    """

    ok: bool = True
    user_label: str
    session_id: str
    user_display_name: str


class LogoutResponse(BaseModel):
    """Success response from ``POST /auth/logout`` (Sprint 3 3A.1).

    The SPA navigates to ``redirect_url`` to land on the IS ``/oidc/logout``
    consent screen (Q3 lock). Q3 + F-19-corrected design: with
    ``id_token_hint`` set, IS walks session participants and fires BCL —
    so ``redirect_url`` is the architectural cornerstone of the cascade,
    not just a UX courtesy.

    When no session existed for the cookie, ``redirect_url`` is ``"/"``
    so the SPA simply returns home.
    """

    ok: bool = True
    redirect_url: str


# ---------------------------------------------------------------------------
# Helper — derive SPA base URL
# ---------------------------------------------------------------------------


def _spa_base_url(config: OrchestratorConfig) -> str:
    """Return the SPA base URL.

    ``OrchestratorConfig`` does not have a dedicated ``spa_base_url`` field in
    Wave 4, so we fall back to the first entry in ``allowed_origins`` sorted
    alphabetically for determinism.  Sprint 2 may add a proper env var.

    Args:
        config: Frozen orchestrator configuration.

    Returns:
        A bare ``scheme://host[:port]`` URL (no trailing slash).
    """
    return sorted(config.allowed_origins)[0]


# ---------------------------------------------------------------------------
# Helper — pick a friendly display name from id_token claims
# ---------------------------------------------------------------------------


_DISPLAY_NAME_CLAIMS = ("given_name", "username", "preferred_username", "email")


def _extract_display_name(id_token: str | None, fallback_sub: str) -> str:
    """Return a human-friendly label, preferring OIDC profile claims.

    Lookup order: given_name, username, preferred_username, email, then
    fallback_sub. Decoded without signature verification — the id_token
    arrived in the same /token response as the already-validated access token.
    """
    if not id_token:
        return fallback_sub
    try:
        payload: dict[str, Any] = pyjwt.decode(
            id_token, options={"verify_signature": False}
        )
    except pyjwt.PyJWTError as exc:
        logger.warning("id_token decode failed; using sub as label | %s", exc)
        return fallback_sub
    for claim in _DISPLAY_NAME_CLAIMS:
        value = payload.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback_sub


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_auth_router(deps: AuthRouterDeps) -> APIRouter:
    """Build and return the FastAPI router for the four auth endpoints.

    Endpoints
    ---------
    ``GET  /auth/login``       — starts Pattern C; redirects browser to IS /authorize
    ``GET  /auth/callback``    — receives IS redirect with code+state; returns HTML relay page
    ``POST /auth/exchange``    — SPA POSTs {code, state}; exchanges code, creates session
    ``POST /auth/logout``      — clears session and cookie

    Args:
        deps: Injected dependencies (config, pattern_c, session_store, pending_logins).

    Returns:
        A ``fastapi.APIRouter`` instance that can be included in the main ``FastAPI`` app.
    """
    router = APIRouter()

    # ------------------------------------------------------------------
    # GET /auth/login
    # ------------------------------------------------------------------

    @router.get("/auth/login")
    async def login(next: str = "/") -> RedirectResponse:
        """Start the Pattern C login flow.

        Generates a fresh PKCE pair and CSRF state, stashes a ``PendingLogin``
        in ``deps.pending_logins``, then redirects the browser to the IS
        ``/oauth2/authorize`` endpoint.

        Args:
            next: Post-login destination for the SPA.  Defaults to ``"/"``.

        Returns:
            A 302 redirect to the IS authorization endpoint.
        """
        state = secrets.token_urlsafe(32)
        code_verifier, _ = make_pkce()

        deps.pending_logins[state] = PendingLogin(
            code_verifier=code_verifier,
            redirect_after_login=next or "/",
            created_at=datetime.now(tz=timezone.utc),
        )
        logger.info(
            "auth_login_initiated | state_prefix=%s redirect_after=%r",
            state[:8],
            next,
        )

        # NOTE: authorize and token-exchange must use the SAME client_id
        # (WSO2 IS rejects cross-client code redemption with invalid_grant).
        # We use the MCP Client App for both — matches c1_pattern_c.py spike.
        authorize_url, _ = build_authorize_url(
            is_authorize_endpoint=f"{deps.config.is_base_url}/oauth2/authorize",
            spa_client_id=deps.config.mcp_client_id,
            redirect_uri=deps.config.mcp_redirect_uri,
            scope="openid profile email orchestrate",
            requested_actor=deps.config.orchestrator_agent.agent_id,
            state=state,
            code_verifier=code_verifier,
        )
        return RedirectResponse(authorize_url, status_code=302)

    # ------------------------------------------------------------------
    # GET /auth/callback
    # ------------------------------------------------------------------

    @router.get("/agent-callback", response_class=HTMLResponse)
    async def callback(
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        """Receive the IS redirect with the authorization code.

        Validates that ``state`` matches a pending login record.  On success,
        returns a self-contained HTML page that immediately fires
        ``fetch("POST /auth/exchange")`` with ``{code, state}`` and on success
        navigates ``window.location`` to ``redirect_after_login``.

        This relay design keeps Sprint 1 self-contained: the SPA does not need
        a dedicated ``/complete-login`` route.

        Args:
            state: CSRF/PKCE state echoed by IS.
            code: Authorization code from IS (absent when ``error`` is set).
            error: IS error string (e.g. ``"access_denied"``).

        Returns:
            ``200 HTMLResponse`` containing the relay page on happy path.

        Raises:
            HTTPException: 400 if ``state`` is missing or unknown; the SPA
                will never see this because the relay page is the SPA's next step.
        """
        # EX-1 — IS returned an error (e.g. user denied consent)
        if error:
            spa_base = _spa_base_url(deps.config)
            redirect_url = f"{spa_base}/login?error={error}"
            logger.warning("auth_callback_error | error=%r state=%s", error, state)
            return HTMLResponse(
                _make_error_redirect_html(redirect_url),
                status_code=200,
            )

        # Validate state
        if not state or state not in deps.pending_logins:
            logger.warning(
                "auth_callback_invalid_state | state=%r known=%d",
                state,
                len(deps.pending_logins),
            )
            raise HTTPException(status_code=400, detail="invalid_state")

        if not code:
            raise HTTPException(status_code=400, detail="missing_code")

        pending = deps.pending_logins[state]
        logger.info(
            "auth_callback_received | state_prefix=%s code_len=%d",
            (state or "")[:8],
            len(code),
        )

        redirect_after = pending.redirect_after_login
        return HTMLResponse(
            _make_exchange_relay_html(code=code, state=state, redirect_after=redirect_after),
            status_code=200,
        )

    # ------------------------------------------------------------------
    # POST /auth/exchange
    # ------------------------------------------------------------------

    @router.post("/auth/exchange", response_model=ExchangeResponse)
    async def exchange(body: ExchangeRequest, response: Response) -> ExchangeResponse:
        """Exchange the authorization code for a session.

        Pops the ``PendingLogin`` associated with ``body.state`` (preventing
        replay), performs the Pattern C code exchange via ``PatternCExchanger``,
        creates a ``Session`` in the store, and sets the ``orch_sid`` cookie.

        Args:
            body: ``{code, state}`` as posted by the relay HTML page.
            response: FastAPI ``Response`` object used to set the session cookie.

        Returns:
            ``{"ok": true, "user_label": "<display name>"}`` on success.

        Raises:
            HTTPException: 400 if ``state`` is unknown (missing or already consumed).
            HTTPException: 502 if the IS code exchange fails.
        """
        pending = deps.pending_logins.pop(body.state, None)
        if pending is None:
            logger.warning("auth_exchange_invalid_state | state=%r", body.state)
            raise HTTPException(status_code=400, detail="invalid_state")

        logger.debug(
            "auth_exchange_entry | state_prefix=%s code_len=%d pending_logins_remaining=%d",
            body.state[:8],
            len(body.code),
            len(deps.pending_logins),
        )

        try:
            result = await deps.pattern_c.exchange(
                code=body.code,
                code_verifier=pending.code_verifier,
                redirect_uri=deps.config.mcp_redirect_uri,
            )
        except Exception as exc:
            logger.error(
                "auth_exchange_failed | exc_type=%s error=%r",
                type(exc).__name__,
                exc,
            )
            raise HTTPException(
                status_code=502, detail="token_exchange_failed"
            ) from exc

        # Extract display name from id_token's OIDC profile claims; fall back
        # progressively to access-token claims, then sub. id_token signature
        # was verified upstream when token-A was validated (same /token response),
        # so we decode without re-verification here.
        claims = result.claims
        user_label: str = _extract_display_name(result.token_a.id_token, claims.sub)

        session = deps.session_store.create(
            user_sub=claims.sub,
            user_label=user_label,
            token_a=result.token_a,
        )
        logger.info(
            "auth_exchange_success | session_id_prefix=%s user_sub=%s",
            session.session_id[:8],
            claims.sub,
        )

        response.set_cookie(
            key=deps.config.session_cookie_name,
            value=session.session_id,
            httponly=True,
            secure=deps.config.cookie_secure,
            samesite="strict",  # 3A.1 FIX-9: tighter than Lax; CSRF defense
            max_age=deps.config.session_ttl_seconds,
        )

        return ExchangeResponse(
            ok=True,
            user_label=session.user_label,
            session_id=session.session_id,
            user_display_name=session.user_label,
        )

    # ------------------------------------------------------------------
    # POST /auth/logout
    # ------------------------------------------------------------------

    @router.post("/auth/logout", response_model=LogoutResponse)
    async def logout(request: Request, response: Response) -> LogoutResponse:
        """Sprint 3 3A.1: orchestrator-driven logout cascade.

        Implements the locked design from
        ``docs/architecture/sprint-3-tech-arch.md`` §1.1 — set
        ``Session.terminating``, cancel pending CIBAs, revoke token-A,
        fan out to receivers (stubbed in 3A.1; wired in 3A.2), delete
        Session. Returns JSON ``{redirect_url}`` so the SPA navigates to
        IS ``/oidc/logout?id_token_hint=…`` — F-19-corrected: with
        ``id_token_hint`` IS fires BCL to all session participants.

        Sprint 3 FIX-9: requires ``X-Request-ID`` header (rejects 400
        without it). Cross-site form POSTs cannot set custom headers,
        which closes the CSRF vector that ``SameSite=Lax`` left open.

        Idempotent: succeeds with ``{redirect_url: "/"}`` if no session
        cookie is present.

        Args:
            request: Incoming ``Request`` — used to read ``orch_sid`` and
                ``X-Request-ID``.
            response: ``Response`` — used to delete the cookie.

        Returns:
            ``LogoutResponse`` with ``redirect_url`` for the SPA to navigate to.

        Raises:
            HTTPException(400): ``X-Request-ID`` header absent (FIX-9 CSRF guard).
        """
        # 3A.1 FIX-9: CSRF defense via required custom header.
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            logger.warning("auth_logout_missing_rid | rejecting per FIX-9")
            raise HTTPException(status_code=400, detail="X-Request-ID required")

        session_id = request.cookies.get(deps.config.session_cookie_name)
        result = None
        if session_id:
            session = deps.session_store.get(session_id)
            if session is not None:
                result = await deps.logout_handler.execute(
                    session=session,
                    request_id=request_id,
                    reason="user_signed_out",
                )
                logger.info(
                    "auth_logout | rid=%s session_id_prefix=%s had_session=%s",
                    request_id,
                    session_id[:8],
                    result.had_session,
                )
            else:
                logger.info(
                    "auth_logout_session_missing | rid=%s session_id_prefix=%s",
                    request_id,
                    session_id[:8],
                )
        else:
            logger.debug("auth_logout_no_cookie | rid=%s", request_id)

        # Clear the cookie unconditionally (best-effort cleanup).
        response.delete_cookie(
            key=deps.config.session_cookie_name,
            httponly=True,
            secure=deps.config.cookie_secure,
            samesite="strict",  # match the set_cookie SameSite (FIX-9)
        )

        redirect_url = (
            result.redirect_url if (result and result.redirect_url) else "/"
        )
        return LogoutResponse(ok=True, redirect_url=redirect_url)

    return router


# ---------------------------------------------------------------------------
# HTML helpers — relay page and error redirect page
# ---------------------------------------------------------------------------


def _make_exchange_relay_html(
    *,
    code: str,
    state: str,
    redirect_after: str,
) -> str:
    """Return a self-contained HTML page that POSTs to ``/auth/exchange``.

    The page has no visible content.  On load it fires a ``fetch`` to
    ``POST /auth/exchange`` with JSON ``{code, state}``.  On success it
    navigates to ``redirect_after``; on failure it navigates to
    ``/login?error=exchange_failed``.

    Inline JavaScript is used intentionally: this page is ephemeral
    (rendered once per login flow, never cached) and has no external
    dependencies.  A CSP header should be applied at the reverse-proxy layer
    for production deployments.

    Args:
        code: Authorization code from IS.
        state: CSRF/PKCE state (used only to correlate; verifier is server-side).
        redirect_after: Where to send the browser on success.

    Returns:
        A complete HTML5 document as a string.
    """
    # Sanitise values that will be inlined into JS string literals.
    safe_code = code.replace("\\", "\\\\").replace("'", "\\'")
    safe_state = state.replace("\\", "\\\\").replace("'", "\\'")
    safe_redirect = redirect_after.replace("\\", "\\\\").replace("'", "\\'")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Completing sign-in…</title>
</head>
<body>
  <p>Completing sign-in, please wait…</p>
  <script>
    (async function() {{
      try {{
        const resp = await fetch('/auth/exchange', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          credentials: 'include',
          body: JSON.stringify({{ code: '{safe_code}', state: '{safe_state}' }})
        }});
        if (resp.ok) {{
          // Persist session_id + user name so SPA's resume-from-localStorage path works.
          // (Cookie alone isn't enough — SPA reads orch_session_id from localStorage on init.)
          try {{
            const data = await resp.json();
            if (data.session_id) localStorage.setItem('orch_session_id', data.session_id);
            if (data.user_display_name) localStorage.setItem('orch_user_name', data.user_display_name);
          }} catch (e) {{ /* fall through; cookie still authenticates */ }}
          window.location.href = '{safe_redirect}';
        }} else {{
          window.location.href = '/login?error=exchange_failed';
        }}
      }} catch (e) {{
        window.location.href = '/login?error=exchange_failed';
      }}
    }})();
  </script>
</body>
</html>"""


def _make_error_redirect_html(redirect_url: str) -> str:
    """Return a minimal HTML page that immediately redirects to ``redirect_url``.

    Used when IS returns ``error=access_denied`` so the user lands back on the
    SPA login page with a friendly error message.

    Args:
        redirect_url: Fully-qualified SPA URL including query string.

    Returns:
        A complete HTML5 document as a string.
    """
    safe_url = redirect_url.replace("\\", "\\\\").replace("'", "\\'")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="0;url={redirect_url}">
  <title>Redirecting…</title>
</head>
<body>
  <script>window.location.href = '{safe_url}';</script>
</body>
</html>"""
