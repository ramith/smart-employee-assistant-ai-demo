"""
 Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.

  This software is the property of WSO2 LLC. and its suppliers, if any.
  Dissemination of any information or reproduction of any material contained
  herein is strictly forbidden, unless permitted by WSO2 in accordance with
  the WSO2 Commercial License available at http://wso2.com/licenses.
  For specific language governing the permissions and limitations under
  this license, please see the license as well as any agreement you've
  entered into with WSO2 governing the purchase of this software and any


  HR & Leave Management MCP Server

  A secured MCP server that exposes HR tools with scope-based access control.
  Tools are protected by JWT token validation and per-tool scope checking.

  Scopes:
    hr_basic   - Company holidays, employee status
    hr_read    - Team leave requests, leave request details
    hr_approve - Approve/reject leave requests
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
import hr_data
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
                f"The user (manager) needs to authorize this action."
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

            # Set context variables for tool-level scope checking
            current_scopes.set(scopes)
            current_token_info.set({
                "sub": subject,
                "aut": aut,
                "act": act,
                "scopes": scopes,
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
            # Log any other interesting claims
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
    "HR & Leave Management",
    token_verifier=JWTTokenVerifier(JWKS_URL, AUTH_ISSUER, CLIENT_ID),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(AUTH_ISSUER),
        resource_server_url=AnyHttpUrl("http://localhost:8000"),
    ),
)


# ─── Tools: hr_basic scope ──────────────────────────────────────────────────

@mcp.tool()
async def get_company_holidays() -> dict:
    """Get the company holiday calendar for the year."""
    scope_error = require_scope("hr_basic")
    if scope_error:
        return scope_error
    return {"holidays": hr_data.get_holidays()}


@mcp.tool()
async def get_employee_status(employee_name: str) -> dict:
    """Check if an employee is currently in-office or out-of-office.

    Args:
        employee_name: The name (or partial name) of the employee to look up.
    """
    scope_error = require_scope("hr_basic")
    if scope_error:
        return scope_error

    emp = hr_data.find_employee_by_name(employee_name)
    if not emp:
        return {"error": "not_found", "message": f"Employee '{employee_name}' not found."}

    return {
        "employee": emp["name"],
        "status": emp["status"],
        "department": emp["department"],
        "role": emp["role"],
    }


# ─── Tools: hr_read scope ───────────────────────────────────────────────────

@mcp.tool()
async def get_team_leave_requests() -> dict:
    """List all pending leave requests for the manager's team."""
    scope_error = require_scope("hr_read")
    if scope_error:
        return scope_error

    pending = hr_data.get_pending_leave_requests()
    return {"pending_requests": pending}


@mcp.tool()
async def get_leave_request_details(request_id: str) -> dict:
    """Get detailed information about a specific leave request.

    Args:
        request_id: The leave request ID (e.g., 'LR001').
    """
    scope_error = require_scope("hr_read")
    if scope_error:
        return scope_error

    lr = hr_data.get_leave_request(request_id)
    if not lr:
        return {"error": "not_found", "message": f"Leave request '{request_id}' not found."}

    return lr


# ─── Tools: hr_approve scope ────────────────────────────────────────────────

@mcp.tool()
async def approve_leave_request(request_id: str) -> dict:
    """Approve a pending leave request. Requires manager authorization.

    Args:
        request_id: The leave request ID to approve (e.g., 'LR001').
    """
    scope_error = require_scope("hr_approve")
    if scope_error:
        return scope_error

    actor = get_actor_description()
    result = hr_data.approve_request(request_id, approved_by=actor)
    if not result:
        return {"error": "not_found", "message": f"Leave request '{request_id}' not found."}

    logger.info(f"[AUDIT] Leave {request_id} approved by {actor}")
    return result


@mcp.tool()
async def reject_leave_request(request_id: str, reason: str) -> dict:
    """Reject a pending leave request with a reason. Requires manager authorization.

    Args:
        request_id: The leave request ID to reject (e.g., 'LR001').
        reason: The reason for rejecting the leave request.
    """
    scope_error = require_scope("hr_approve")
    if scope_error:
        return scope_error

    actor = get_actor_description()
    result = hr_data.reject_request(request_id, reason=reason, rejected_by=actor)
    if not result:
        return {"error": "not_found", "message": f"Leave request '{request_id}' not found."}

    logger.info(f"[AUDIT] Leave {request_id} rejected by {actor} - Reason: {reason}")
    return result


# ─── REST API for Dashboard (ASGI middleware) ────────────────────────────────

from starlette.responses import JSONResponse as StarletteJSONResponse
import uvicorn


class DashboardMiddleware:
    """ASGI middleware that intercepts /api/leaves before reaching the MCP app.

    Validates JWT and checks hr_read scope for dashboard access.
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
        if scope["type"] == "http" and scope["path"] == "/api/leaves":
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
            if "hr_read" not in scopes:
                response = StarletteJSONResponse(
                    {"error": "forbidden", "message": "Missing required scope: hr_read"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

            response = StarletteJSONResponse(
                {"leaves": hr_data.get_all_leave_requests()}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp_starlette = mcp.streamable_http_app()
    app = DashboardMiddleware(mcp_starlette)
    uvicorn.run(app, host="0.0.0.0", port=8000)
