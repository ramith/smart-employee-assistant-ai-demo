"""
  IT Support Appointment Management MCP Server

  A secured MCP server that exposes IT support appointment tools with
  scope-based access control. Tools are protected by JWT token validation
  and per-tool scope checking.

  Scopes:
    it_basic   - View support categories, technicians, available time slots
    it_read    - View own appointments and appointment details
    it_manage  - Book, cancel, or reschedule appointments
"""

import os
import contextvars
from dotenv import load_dotenv
from pydantic import AnyHttpUrl

load_dotenv()

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from jwt_validator import JWTValidator
import it_support_data
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Context variable for current request scopes ─────────────────────────────

current_scopes: contextvars.ContextVar[list] = contextvars.ContextVar(
    "current_scopes", default=[]
)
current_token_info: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "current_token_info", default={}
)


def require_scope(scope: str) -> dict | None:
    """Check if the current request has the required scope.
    Returns an error dict if missing, None if OK."""
    scopes = current_scopes.get()
    if scope not in scopes:
        logger.warning(
            f"[SCOPE DENIED] Required: '{scope}' | Present: {scopes}"
        )
        return {
            "error": "insufficient_scope",
            "required_scope": scope,
            "message": (
                f"Access denied. This action requires '{scope}' permission. "
                f"The user needs to authorize this action."
            ),
        }
    return None


def get_actor_description() -> str:
    """Build an actor description from the current token for audit logging."""
    info = current_token_info.get()
    act = info.get("act")
    sub = info.get("sub", "unknown")
    if act:
        return f"AI Agent (on behalf of {sub})"
    return f"AI Agent ({sub})"


# ─── JWT Token Verifier ─────────────────────────────────────────────────────

class JWTTokenVerifier(TokenVerifier):
    """JWT token verifier that extracts scopes and sets context variables."""

    def __init__(self, jwks_url: str, issuer: str, client_id: str):
        self.jwt_validator = JWTValidator(
            jwks_url=jwks_url,
            issuer=issuer,
            audience=client_id,
            ssl_verify=True,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            payload = await self.jwt_validator.validate_token(token)

            expires_at = payload.get("exp")
            scopes = payload.get("scope", "").split() if payload.get("scope") else []
            subject = payload.get("sub")
            audience = payload.get("aud")
            aut = payload.get("aut")
            act = payload.get("act")

            # Extract identity claims for tool-level user identification
            display_name = (
                payload.get("name")
                or payload.get("preferred_username")
                or payload.get("given_name")
                or payload.get("email")
                or subject
            )

            # Set context variables for tool-level scope checking
            current_scopes.set(scopes)
            current_token_info.set({
                "sub": subject,
                "aut": aut,
                "act": act,
                "scopes": scopes,
                "display_name": display_name,
            })

            # Log all token claims for debugging
            logger.info("=" * 60)
            logger.info("[JWT TOKEN CLAIMS]")
            logger.info(f"  sub (subject)  : {subject}")
            logger.info(f"  aud (audience)  : {audience}")
            logger.info(f"  aut (auth type) : {aut}")
            if act:
                logger.info(f"  act (actor)     : {act}")
            logger.info(f"  scope           : {payload.get('scope', 'N/A')}")
            logger.info(f"  scopes (parsed) : {scopes}")
            logger.info(f"  exp (expires)   : {expires_at}")
            for key in ["azp", "client_id", "org_id", "org_name", "iss"]:
                if key in payload:
                    logger.info(f"  {key:16s}: {payload[key]}")
            logger.info("=" * 60)

            return AccessToken(
                token=token,
                client_id=audience if isinstance(audience, str) else self.jwt_validator.audience,
                scopes=scopes,
                expires_at=str(expires_at) if expires_at else None,
            )
        except ValueError as e:
            logger.warning(f"Token validation failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during token validation: {e}")
            return None


# ─── Server Configuration ────────────────────────────────────────────────────

AUTH_ISSUER = os.getenv("AUTH_ISSUER")
CLIENT_ID = os.getenv("CLIENT_ID")
JWKS_URL = os.getenv("JWKS_URL")

if not all([AUTH_ISSUER, CLIENT_ID, JWKS_URL]):
    raise ValueError("Missing required environment variables: AUTH_ISSUER, CLIENT_ID, or JWKS_URL")

mcp = FastMCP(
    "IT Support Appointment Management",
    port=8001,
    token_verifier=JWTTokenVerifier(JWKS_URL, AUTH_ISSUER, CLIENT_ID),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(AUTH_ISSUER),
        resource_server_url=AnyHttpUrl("http://localhost:8001"),
    ),
)


# ─── Tools: it_basic scope ──────────────────────────────────────────────────

@mcp.tool()
async def get_support_categories() -> dict:
    """Get the list of IT support categories available for appointments."""
    # scope_error = require_scope("it_basic")
    # if scope_error:
    #     return scope_error
    return {"categories": it_support_data.get_categories()}


@mcp.tool()
async def get_available_slots(date: str = "", technician_id: str = "") -> dict:
    """Get available appointment time slots, optionally filtered by date and/or technician.

    Args:
        date: Optional date filter in YYYY-MM-DD format (e.g., '2026-02-25').
        technician_id: Optional technician ID to filter by (e.g., 'TECH001').
    """
    # scope_error = require_scope("it_basic")
    # if scope_error:
    #     return scope_error

    slots = it_support_data.get_available_slots_list(
        date=date or None,
        technician_id=technician_id or None,
    )
    if not slots:
        return {"message": "No available slots found for the given criteria.", "slots": []}
    return {"slots": slots}


@mcp.tool()
async def get_technicians(category_name: str = "") -> dict:
    """List IT support technicians, optionally filtered by support category.

    Args:
        category_name: Optional category name to filter technicians by specialization
                       (e.g., 'Hardware Issue', 'Network & Connectivity').
    """
    # scope_error = require_scope("it_basic")
    # if scope_error:
    #     return scope_error

    techs = it_support_data.get_technicians_list(
        category_name=category_name or None,
    )
    return {"technicians": techs}


# ─── Tools: it_read scope ───────────────────────────────────────────────────

@mcp.tool()
async def get_my_appointments(employee_name: str = "") -> dict:
    """Get all IT support appointments for an employee.
    If employee_name is not provided, the identity is automatically
    resolved from the authenticated user's token.

    Args:
        employee_name: Optional name (or partial name) of the employee.
                       If omitted, uses the authenticated user's identity.
    """
    scope_error = require_scope("it_read")
    if scope_error:
        return scope_error

    # If no employee name provided, resolve from the token
    if not employee_name:
        token_info = current_token_info.get()
        employee_name = token_info.get("display_name", "")
        if not employee_name:
            return {"error": "identity_unknown", "message": "Could not determine your identity from the token. Please provide your name."}
        logger.info(f"Resolved employee identity from token: {employee_name}")

    appts = it_support_data.get_appointments_for_employee(employee_name)
    if not appts:
        return {"message": f"No appointments found for '{employee_name}'.", "appointments": []}
    return {"appointments": appts}


@mcp.tool()
async def get_appointment_details(appointment_id: str) -> dict:
    """Get detailed information about a specific IT support appointment.

    Args:
        appointment_id: The appointment ID (e.g., 'APT001').
    """
    scope_error = require_scope("it_read")
    if scope_error:
        return scope_error

    apt = it_support_data.get_appointment(appointment_id)
    if not apt:
        return {"error": "not_found", "message": f"Appointment '{appointment_id}' not found."}
    return apt


# ─── Tools: it_manage scope ─────────────────────────────────────────────────

@mcp.tool()
async def book_appointment(
    employee_name: str,
    category_id: str,
    slot_id: str,
    description: str,
) -> dict:
    """Book a new IT support appointment.

    Args:
        employee_name: Full name of the employee booking the appointment.
        category_id: The support category ID (e.g., 'CAT001' for Hardware Issue).
        slot_id: The time slot ID to book (e.g., 'SLOT001').
        description: A brief description of the IT issue or request.
    """
    scope_error = require_scope("it_manage")
    if scope_error:
        return scope_error

    actor = get_actor_description()
    result = it_support_data.book_appointment(
        employee_name=employee_name,
        category_id=category_id,
        slot_id=slot_id,
        description=description,
        booked_by=actor,
    )

    if result.get("success"):
        logger.info(
            f"[AUDIT] Appointment {result['appointment_id']} booked by {actor} "
            f"for {employee_name}"
        )
    return result


@mcp.tool()
async def cancel_appointment(appointment_id: str) -> dict:
    """Cancel an existing IT support appointment.

    Args:
        appointment_id: The appointment ID to cancel (e.g., 'APT001').
    """
    scope_error = require_scope("it_manage")
    if scope_error:
        return scope_error

    actor = get_actor_description()
    result = it_support_data.cancel_appointment(appointment_id, cancelled_by=actor)
    if not result:
        return {"error": "not_found", "message": f"Appointment '{appointment_id}' not found."}

    if result.get("success"):
        logger.info(f"[AUDIT] Appointment {appointment_id} cancelled by {actor}")
    return result


@mcp.tool()
async def reschedule_appointment(appointment_id: str, new_slot_id: str) -> dict:
    """Reschedule an existing IT support appointment to a different time slot.

    Args:
        appointment_id: The appointment ID to reschedule (e.g., 'APT001').
        new_slot_id: The new time slot ID to move the appointment to (e.g., 'SLOT003').
    """
    scope_error = require_scope("it_manage")
    if scope_error:
        return scope_error

    actor = get_actor_description()
    result = it_support_data.reschedule_appointment(
        appointment_id=appointment_id,
        new_slot_id=new_slot_id,
        rescheduled_by=actor,
    )
    if not result:
        return {"error": "not_found", "message": f"Appointment '{appointment_id}' not found."}

    if result.get("success"):
        logger.info(
            f"[AUDIT] Appointment {appointment_id} rescheduled to {new_slot_id} by {actor}"
        )
    return result


# ─── REST API for Dashboard (ASGI middleware) ────────────────────────────────

from starlette.responses import JSONResponse as StarletteJSONResponse
import uvicorn


class DashboardMiddleware:
    """ASGI middleware that intercepts /api/bookings before reaching the MCP app.

    Validates JWT and checks it_read scope for dashboard access.
    This avoids wrapping the MCP Starlette app in a parent Starlette app,
    which would break its lifespan (session manager task group init).
    """

    def __init__(self, app):
        self.app = app
        self.jwt_validator = JWTValidator(
            jwks_url=JWKS_URL,
            issuer=AUTH_ISSUER,
            audience=CLIENT_ID,
            ssl_verify=True,
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/api/bookings":
            # Extract Authorization header from ASGI scope
            auth_header = ""
            for header_name, header_value in scope.get("headers", []):
                if header_name == b"authorization":
                    auth_header = header_value.decode("utf-8")
                    break

            if not auth_header.startswith("Bearer "):
                response = StarletteJSONResponse(
                    {"error": "unauthorized", "message": "Missing or invalid Authorization header"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return

            token = auth_header[7:]
            try:
                payload = await self.jwt_validator.validate_token(token)
            except ValueError as e:
                response = StarletteJSONResponse(
                    {"error": "unauthorized", "message": str(e)},
                    status_code=401,
                )
                await response(scope, receive, send)
                return

            scopes = payload.get("scope", "").split() if payload.get("scope") else []
            if "it_read" not in scopes:
                response = StarletteJSONResponse(
                    {"error": "forbidden", "message": "Missing required scope: it_read"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

            response = StarletteJSONResponse(
                {"bookings": it_support_data.get_all_appointments()}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp_starlette = mcp.streamable_http_app()
    app = DashboardMiddleware(mcp_starlette)
    uvicorn.run(app, host="0.0.0.0", port=8001)
