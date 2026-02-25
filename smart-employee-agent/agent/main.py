"""
 Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.

  This software is the property of WSO2 LLC. and its suppliers, if any.
  Dissemination of any information or reproduction of any material contained
  herein is strictly forbidden, unless permitted by WSO2 in accordance with
  the WSO2 Commercial License available at http://wso2.com/licenses.
  For specific language governing the permissions and limitations under
  this license, please see the license as well as any agreement you've
  entered into with WSO2 governing the purchase of this software and any


  Smart Employee Agent - Web Server

  A FastAPI web server that hosts a LangChain AI agent with dynamic privilege
  escalation. The agent starts with a low-privilege agent token and escalates
  to an On-Behalf-Of (OBO) token when manager authorization is needed.
"""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from asgardeo import AsgardeoConfig
from asgardeo_ai import AgentConfig, AgentAuthManager

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI

import uvicorn

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Asgardeo Configuration ─────────────────────────────────────────────────

ASGARDEO_CONFIG = AsgardeoConfig(
    base_url=os.getenv("ASGARDEO_BASE_URL"),
    client_id=os.getenv("CLIENT_ID"),
    redirect_uri=os.getenv("REDIRECT_URI"),
)

AGENT_CONFIG = AgentConfig(
    agent_id=os.getenv("AGENT_ID"),
    agent_secret=os.getenv("AGENT_SECRET"),
)

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
IT_SUPPORT_SERVER_URL = os.getenv("IT_SUPPORT_SERVER_URL", "http://127.0.0.1:8001/mcp")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.5-flash")

SYSTEM_PROMPT = """You are the Smart Employee Assistant, an AI agent for the HR & Leave Management system.

You help managers with HR-related queries. You have access to the following tools:
- get_company_holidays: View the company holiday calendar
- get_employee_status: Check if an employee is in-office or out-of-office
- get_team_leave_requests: View pending leave requests for the team
- get_leave_request_details: Get detailed info about a specific leave request
- approve_leave_request: Approve a pending leave request
- reject_leave_request: Reject a leave request with a reason

IMPORTANT: Some tools require elevated manager privileges. If a tool returns an
"insufficient_scope" error, inform the user that you need their authorization
to perform this action. Do not retry the tool - the system will handle
the authorization process.

When reporting results, be clear and concise. Include relevant details like
employee names, dates, and leave types."""

# ─── Session State ───────────────────────────────────────────────────────────

session = {
    "agent_token": None,
    "obo_token": None,
    "code_verifier": None,
    "pkce_state": None,
    "chat_history": [],
    "pending_message": None,
    "mcp_client": None,
}

# ─── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(title="Smart Employee Agent")

STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
async def startup():
    """Authenticate the agent on startup and connect to MCP server."""
    logger.info("Authenticating agent with Asgardeo...")
    async with AgentAuthManager(ASGARDEO_CONFIG, AGENT_CONFIG) as auth_manager:
        session["agent_token"] = await auth_manager.get_agent_token(["openid", "hr_basic"])
    logger.info("Agent authenticated successfully (scopes: hr_basic)")

    await _connect_mcp(session["agent_token"].access_token)
    logger.info("Connected to HR MCP server with agent token")


async def _connect_mcp(access_token: str):
    """Create an MCP client connection with the given token."""
    session["mcp_client"] = MultiServerMCPClient(
        {
            "hr_server": {
                "transport": "streamable_http",
                "url": MCP_SERVER_URL,
                "headers": {"Authorization": f"Bearer {access_token}"},
            },
            "it_server": {
                "transport": "streamable_http",
                "url": IT_SUPPORT_SERVER_URL,
                "headers": {"Authorization": f"Bearer {access_token}"},
            }
        }
    )


def _extract_text(content) -> str:
    """Extract plain text from a LangChain message content.
    Content can be a string, or a list of content blocks like
    [{"type": "text", "text": "..."}]."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _needs_escalation(response) -> bool:
    """Check if any tool call in the response returned an insufficient_scope error."""
    for message in response.get("messages", []):
        if hasattr(message, "type") and message.type == "tool":
            content = str(message.content)
            if "insufficient_scope" in content:
                return True
    return False


async def _invoke_agent(user_message: str) -> dict:
    """Invoke the LangChain agent with the given message."""
    client = session["mcp_client"]

    tools = await client.get_tools()

    llm = ChatGoogleGenerativeAI(
        model=MODEL_NAME,
        temperature=0.7,
    )
    agent = create_agent(llm, tools)

    # Build messages with system prompt and chat history
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(session["chat_history"])
    messages.append({"role": "user", "content": user_message})

    response = await agent.ainvoke({"messages": messages})

    return response


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    """Serve the chat UI."""
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text())


@app.post("/api/chat")
async def chat(request: Request):
    """Process a user message through the AI agent."""
    body = await request.json()
    user_message = body.get("message", "").strip()
    if not user_message:
        return JSONResponse({"type": "error", "message": "Message cannot be empty."}, status_code=400)

    # Use OBO token if available, otherwise agent token
    current_token = session["obo_token"] or session["agent_token"]
    await _connect_mcp(current_token.access_token)

    try:
        response = await _invoke_agent(user_message)
    except Exception as e:
        logger.error(f"Agent invocation failed: {e}")
        return JSONResponse({"type": "error", "message": f"Agent error: {str(e)}"}, status_code=500)

    # Check if escalation is needed
    if _needs_escalation(response) and not session["obo_token"]:
        session["pending_message"] = user_message
        agent_reply = _extract_text(response["messages"][-1].content)
        return JSONResponse({
            "type": "auth_required",
            "message": agent_reply,
        })

    # Successful response
    agent_reply = _extract_text(response["messages"][-1].content)

    # Update chat history
    session["chat_history"].append({"role": "user", "content": user_message})
    session["chat_history"].append({"role": "assistant", "content": agent_reply})

    return JSONResponse({"type": "response", "message": agent_reply})


@app.get("/api/auth/url")
async def get_auth_url():
    """Generate a PKCE authorization URL for the OBO flow."""
    async with AgentAuthManager(ASGARDEO_CONFIG, AGENT_CONFIG) as auth_manager:
        auth_url, state, code_verifier = auth_manager.get_authorization_url_with_pkce(
            ["openid", "hr_basic", "hr_read", "hr_approve"]
        )

    session["code_verifier"] = code_verifier
    session["pkce_state"] = state

    logger.info("Generated PKCE authorization URL for OBO flow")
    return JSONResponse({"auth_url": auth_url})


@app.get("/oauth/callback")
async def oauth_callback(code: str = None, state: str = None, error: str = None):
    """Handle the OAuth2 redirect from Asgardeo after user login."""
    if error:
        logger.warning(f"OAuth error: {error}")
        return HTMLResponse(content=_callback_html(success=False, error=error))

    if not code:
        return HTMLResponse(content=_callback_html(success=False, error="Missing authorization code"))

    try:
        async with AgentAuthManager(ASGARDEO_CONFIG, AGENT_CONFIG) as auth_manager:
            agent_token = await auth_manager.get_agent_token(["openid", "hr_basic"])
            obo_token = await auth_manager.get_obo_token(
                code,
                agent_token=agent_token,
                code_verifier=session["code_verifier"],
            )

        session["obo_token"] = obo_token
        session["code_verifier"] = None
        session["pkce_state"] = None

        logger.info("OBO token obtained successfully - manager privileges activated")
        return HTMLResponse(content=_callback_html(success=True))

    except Exception as e:
        logger.error(f"OBO token exchange failed: {e}")
        return HTMLResponse(content=_callback_html(success=False, error=str(e)))


@app.get("/api/auth/status")
async def auth_status():
    """Check if the OBO token (manager authorization) is available."""
    return JSONResponse({"authorized": session["obo_token"] is not None})


@app.get("/api/auth/pending")
async def get_pending():
    """Get the pending message that triggered the auth flow."""
    msg = session.get("pending_message")
    return JSONResponse({"pending_message": msg})


def _callback_html(success: bool, error: str = None) -> str:
    """Generate the HTML for the OAuth callback popup page."""
    if success:
        return """<!DOCTYPE html>
<html>
<head><title>Authorization Successful</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; background: #f0fdf4; }
  .card { text-align: center; padding: 2rem; }
  .icon { font-size: 3rem; margin-bottom: 1rem; }
  h2 { color: #166534; margin-bottom: 0.5rem; }
  p { color: #6b7280; }
</style>
</head>
<body>
  <div class="card">
    <div class="icon">&#10003;</div>
    <h2>Authorization Successful</h2>
    <p>You can close this window. The assistant will now process your request.</p>
  </div>
  <script>
    if (window.opener) {
      window.opener.postMessage({ type: 'auth_success' }, '*');
    }
    setTimeout(() => window.close(), 2000);
  </script>
</body>
</html>"""
    else:
        return f"""<!DOCTYPE html>
<html>
<head><title>Authorization Failed</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; background: #fef2f2; }}
  .card {{ text-align: center; padding: 2rem; }}
  .icon {{ font-size: 3rem; margin-bottom: 1rem; }}
  h2 {{ color: #991b1b; margin-bottom: 0.5rem; }}
  p {{ color: #6b7280; }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon">&#10007;</div>
    <h2>Authorization Failed</h2>
    <p>{error or 'Unknown error'}</p>
    <p>You can close this window and try again.</p>
  </div>
  <script>
    if (window.opener) {{
      window.opener.postMessage({{ type: 'auth_failed', error: '{error or "Unknown error"}' }}, '*');
    }}
  </script>
</body>
</html>"""


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5001)
