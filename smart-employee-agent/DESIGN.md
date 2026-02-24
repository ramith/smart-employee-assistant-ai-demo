# Smart Employee Agent - System Design

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Browser (User)                               │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                    Chat Web UI                                │  │
│  │  - Message input & chat bubbles                              │  │
│  │  - Shows "Authorize" button when escalation needed           │  │
│  │  - Opens Asgardeo login in popup for OBO authorization       │  │
│  │  - Auto-retries pending request after auth completes         │  │
│  └────────────┬─────────────────────────┬────────────────────────┘  │
│               │ POST /api/chat          │ Popup → /oauth/callback   │
└───────────────┼─────────────────────────┼───────────────────────────┘
                │                         │
                ▼                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Agent Web Server (FastAPI)                        │
│                    http://localhost:5000                             │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Endpoints                                                   │   │
│  │  GET  /              → Serve chat UI (static HTML/JS/CSS)   │   │
│  │  POST /api/chat      → Process user message via LLM agent   │   │
│  │  GET  /api/auth/url  → Generate PKCE auth URL for OBO       │   │
│  │  GET  /oauth/callback→ Handle Asgardeo redirect, get OBO    │   │
│  │  GET  /api/auth/status→ Check if OBO token is available     │   │
│  └──────────┬──────────────────────────┬───────────────────────┘   │
│             │                          │                           │
│  ┌──────────▼──────────┐  ┌───────────▼──────────────────────┐    │
│  │  LangChain Agent    │  │  Session State (in-memory)       │    │
│  │  + Gemini LLM       │  │  - agent_token (startup)         │    │
│  │  + MCP Tools        │  │  - obo_token (after user auth)   │    │
│  │  + Chat History     │  │  - PKCE state & code_verifier    │    │
│  └──────────┬──────────┘  │  - chat_history                  │    │
│             │              │  - pending_message               │    │
│  ┌──────────▼──────────┐  └──────────────────────────────────┘    │
│  │  MCP Client         │                                          │
│  │  (streamable HTTP)  │                                          │
│  └──────────┬──────────┘                                          │
└─────────────┼─────────────────────────────────────────────────────┘
              │ HTTP + Bearer Token
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                HR & Leave Management MCP Server                      │
│                http://localhost:8000                                 │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  JWT Token Verifier (JWKS) → Scope-Based Access Control     │  │
│  │                                                              │  │
│  │  hr_basic   → get_company_holidays, get_employee_status      │  │
│  │  hr_read    → get_team_leave_requests, get_leave_request_details│
│  │  hr_approve → approve_leave_request, reject_leave_request    │  │
│  └──────────────────────────┬───────────────────────────────────┘  │
│  ┌──────────────────────────▼───────────────────────────────────┐  │
│  │  In-Memory HR Data (employees, leave requests, holidays)     │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Design

### 2.1 HR MCP Server (`hr-mcp-server/`)

No change from the previous design — the MCP server is auth-method-agnostic.

#### Files

| File | Responsibility |
|------|---------------|
| `main.py` | FastMCP server setup, JWT verifier, tool definitions with scope checks |
| `hr_data.py` | In-memory mock data and data access functions |
| `jwt_validator.py` | JWT validation using JWKS (reused from `mcp-auth` sample) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |

#### Scope-Based Access Control

1. **Token Verifier** validates the JWT and extracts scopes into the `AccessToken` object.
2. **Context variable** (`contextvars.ContextVar`) stores the current request's scopes.
3. **Each tool** calls a `require_scope()` helper. If the scope is missing, the tool returns a structured error dict (not an exception) so the LLM can understand.

```python
import contextvars

current_scopes: contextvars.ContextVar[list] = contextvars.ContextVar("current_scopes", default=[])

def require_scope(scope: str) -> dict | None:
    """Returns an error dict if scope is missing, None if OK."""
    scopes = current_scopes.get()
    if scope not in scopes:
        return {
            "error": "insufficient_scope",
            "required_scope": scope,
            "message": f"Access denied. This action requires '{scope}' permission. "
                       f"The user (manager) needs to authorize this action."
        }
    return None

@mcp.tool()
async def approve_leave_request(request_id: str) -> dict:
    """Approve a pending leave request. Requires manager authorization."""
    scope_error = require_scope("hr_approve")
    if scope_error:
        return scope_error
    # ... actual approval logic
```

#### Tool Specifications

| Tool | Scope | Input | Output |
|------|-------|-------|--------|
| `get_company_holidays()` | `hr_basic` | none | `{ "holidays": [...] }` |
| `get_employee_status(employee_name)` | `hr_basic` | name str | `{ "employee": "...", "status": "in-office" }` |
| `get_team_leave_requests()` | `hr_read` | none | `{ "pending_requests": [...] }` |
| `get_leave_request_details(request_id)` | `hr_read` | ID str | `{ "request_id": "...", ... }` |
| `approve_leave_request(request_id)` | `hr_approve` | ID str | `{ "success": true, ... }` |
| `reject_leave_request(request_id, reason)` | `hr_approve` | ID str, reason str | `{ "success": true, ... }` |

---

### 2.2 Agent Web Server (`agent/`)

A **FastAPI** web server that serves the chat UI and exposes API endpoints for the agent.

#### Files

| File | Responsibility |
|------|---------------|
| `main.py` | FastAPI app, API endpoints, agent orchestration, OAuth handling |
| `static/index.html` | Chat web UI (HTML + CSS + JS, single file) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |

#### API Endpoints

**`GET /`** — Serve the chat UI
- Returns the static `index.html` file.

**`POST /api/chat`** — Process a user message
- Request: `{ "message": "Approve Sarah's leave" }`
- Response (normal): `{ "type": "response", "message": "Here are the holidays..." }`
- Response (auth needed): `{ "type": "auth_required", "message": "I need your authorization to approve leave requests.", "pending_message": "Approve Sarah's leave" }`
- The backend invokes the LangChain agent, checks for `insufficient_scope` in tool results, and returns the appropriate response type.

**`GET /api/auth/url`** — Generate PKCE authorization URL
- Called by the frontend when user clicks "Authorize".
- Backend generates PKCE challenge, stores `code_verifier` in session state.
- Response: `{ "auth_url": "https://api.asgardeo.io/t/.../authorize?..." }`

**`GET /oauth/callback?code=...&state=...`** — Handle OAuth redirect
- Called by Asgardeo after the user logs in (browser redirect).
- Exchanges the authorization code + PKCE verifier for an OBO token.
- Stores OBO token in session state.
- Returns an HTML page that sends `postMessage` to the opener window and closes the popup.

**`GET /api/auth/status`** — Check authorization status
- Response: `{ "authorized": true/false }`
- Frontend polls this after the popup closes to confirm auth success.

#### Session State (In-Memory)

Since this is a single-user sample application, state is stored in a module-level dict:

```python
session_state = {
    "agent_token": None,       # Obtained on startup
    "obo_token": None,         # Obtained after user authorizes
    "code_verifier": None,     # PKCE state during auth flow
    "pkce_state": None,        # CSRF state parameter
    "chat_history": [],        # LangChain message history
    "pending_message": None,   # Message to retry after auth
}
```

---

### 2.3 Chat Web UI (`agent/static/index.html`)

A single-file HTML page with embedded CSS and JavaScript. No build tools required.

#### UI Layout

```
┌──────────────────────────────────────────────┐
│  ┌────────────────────────────────────────┐  │
│  │  🏢 Smart Employee Assistant           │  │
│  │  HR & Leave Management                 │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  ┌────────────────────────────────────────┐  │
│  │                                        │  │
│  │  Agent: Hello! I'm your Smart Employee │  │
│  │  Assistant. How can I help you today?  │  │
│  │                                        │  │
│  │  You: What holidays do we have?        │  │
│  │                                        │  │
│  │  Agent: Here are the company holidays  │  │
│  │  for 2025: ...                         │  │
│  │                                        │  │
│  │  You: Approve Sarah's leave            │  │
│  │                                        │  │
│  │  Agent: I need your authorization to   │  │
│  │  approve leave requests.               │  │
│  │  ┌──────────────────────────────────┐  │  │
│  │  │ 🔐 Authorize (Login to approve) │  │  │
│  │  └──────────────────────────────────┘  │  │
│  │                                        │  │
│  │  Agent: ✅ Sarah Johnson's annual      │  │
│  │  leave (Mar 10-14) has been approved.  │  │
│  │                                        │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  ┌────────────────────────────┐ ┌──────────┐│
│  │  Type your message...      │ │  Send    ││
│  └────────────────────────────┘ └──────────┘│
└──────────────────────────────────────────────┘
```

#### Frontend Behavior

1. **Send message**: POST to `/api/chat` with the message text.
2. **Show response**: Append agent's response as a chat bubble.
3. **Handle auth_required**: When response type is `auth_required`:
   - Show the agent's message explaining auth is needed.
   - Show an **"Authorize"** button in the chat.
   - When clicked → call `GET /api/auth/url` to get the PKCE auth URL.
   - Open the auth URL in a **popup window** (same browser).
   - Listen for `postMessage` from the popup confirming auth success.
   - On success → automatically retry the pending message via `POST /api/chat`.
4. **Loading state**: Show a typing indicator while waiting for the agent.

#### Popup Auth Flow (from user's perspective)

```
1. User clicks "Authorize" button in chat
2. Popup opens → Asgardeo login page appears
3. User enters credentials (same browser)
4. Asgardeo redirects popup to /oauth/callback
5. Callback page shows "Authorization successful!" and auto-closes
6. Chat UI automatically retries the pending request
7. Agent response appears in chat: "✅ Leave approved"
```

---

## 3. Authentication Flow Sequences

### 3.1 Startup

```
Browser              Agent Server              Asgardeo            MCP Server
   │                      │                       │                    │
   │                      │── Agent Credentials ──►│                    │
   │                      │   (agent_id + secret)  │                    │
   │                      │◄── Agent Token ────────│                    │
   │                      │   (scopes: hr_basic)   │                    │
   │                      │                        │                    │
   │                      │── Connect MCP ─────────────────────────────►│
   │                      │◄── Tools Available ────────────────────────│
   │                      │                        │                    │
   │── GET / ────────────►│                        │                    │
   │◄── Chat UI HTML ─────│                        │                    │
```

### 3.2 Basic Query (Agent Token sufficient)

```
Browser              Agent Server                              MCP Server
   │                      │                                        │
   │── POST /api/chat ───►│                                        │
   │   "What holidays?"   │                                        │
   │                      │── get_company_holidays() ─────────────►│
   │                      │   (Bearer: agent_token)                │
   │                      │◄── { holidays: [...] } ────────────────│
   │                      │                                        │
   │◄── { type: "response", message: "Here are..." } ─────────────│
```

### 3.3 Privilege Escalation (OBO Flow via Browser Popup)

```
Browser              Agent Server          Asgardeo           MCP Server
   │                      │                    │                    │
   │── POST /api/chat ───►│                    │                    │
   │   "Approve Sarah's   │                    │                    │
   │    leave"             │                    │                    │
   │                      │── approve_leave() ─────────────────────►│
   │                      │   (Bearer: agent_token)                │
   │                      │◄── { error: "insufficient_scope" } ────│
   │                      │                    │                    │
   │◄── { type: "auth_required",              │                    │
   │      message: "I need authorization..." } │                    │
   │                      │                    │                    │
   │ [Shows "Authorize"   │                    │                    │
   │  button in chat]     │                    │                    │
   │                      │                    │                    │
   │── GET /api/auth/url ►│                    │                    │
   │                      │── Generate PKCE ───│                    │
   │◄── { auth_url: ... } │                    │                    │
   │                      │                    │                    │
   │ [Opens popup to      │                    │                    │
   │  auth_url]           │                    │                    │
   │         ┌────────────┼────────────────────┤                    │
   │  Popup: │ Asgardeo   │                    │                    │
   │         │ Login Page  │                    │                    │
   │         │    ↓        │                    │                    │
   │         │ User logs in│                    │                    │
   │         │    ↓        │                    │                    │
   │         │ Redirect to │                    │                    │
   │         │ /oauth/     │                    │                    │
   │         │ callback    │                    │                    │
   │         └────────────►│                    │                    │
   │                      │── Exchange code ───►│                    │
   │                      │   + PKCE verifier   │                    │
   │                      │   + agent_token     │                    │
   │                      │◄── OBO Token ───────│                    │
   │                      │   (hr_basic,        │                    │
   │                      │    hr_read,         │                    │
   │                      │    hr_approve)      │                    │
   │                      │                    │                    │
   │◄── postMessage ──────│                    │                    │
   │   ("auth_success")   │                    │                    │
   │ [Popup closes]       │                    │                    │
   │                      │                    │                    │
   │── POST /api/chat ───►│                    │                    │
   │   "Approve Sarah's   │                    │                    │
   │    leave" (retry)    │                    │                    │
   │                      │── Reconnect MCP ───────────────────────►│
   │                      │   (Bearer: obo_token)                  │
   │                      │── approve_leave() ─────────────────────►│
   │                      │◄── { success: true } ──────────────────│
   │                      │                    │                    │
   │◄── { type: "response",                   │                    │
   │      message: "✅ Sarah's leave approved" }                    │
```

---

## 4. Data Model

### In-Memory HR Data (`hr_data.py`)

```python
employees = {
    "EMP001": {
        "id": "EMP001", "name": "Sarah Johnson",
        "department": "Engineering", "role": "Software Engineer",
        "status": "in-office", "leave_balance": {"annual": 18, "sick": 10}
    },
    "EMP002": {
        "id": "EMP002", "name": "Ahmed Khan",
        "department": "Engineering", "role": "Senior Developer",
        "status": "in-office", "leave_balance": {"annual": 15, "sick": 8}
    },
    "EMP003": {
        "id": "EMP003", "name": "Maria Garcia",
        "department": "Engineering", "role": "QA Engineer",
        "status": "out-of-office", "leave_balance": {"annual": 20, "sick": 10}
    },
    "EMP004": {
        "id": "EMP004", "name": "James Wilson",
        "department": "Engineering", "role": "DevOps Engineer",
        "status": "in-office", "leave_balance": {"annual": 12, "sick": 9}
    }
}

leave_requests = {
    "LR001": { "request_id": "LR001", "employee_id": "EMP001",
               "type": "Annual Leave", "start_date": "2025-03-10",
               "end_date": "2025-03-14", "days_requested": 5,
               "status": "Pending", "reason": "Family vacation",
               "submitted_at": "2025-02-28T09:00:00Z" },
    "LR002": { ... },
    "LR003": { ... }
}

company_holidays = [
    {"date": "2025-01-01", "name": "New Year's Day"},
    {"date": "2025-03-31", "name": "Eid Al Fitr (expected)"},
    {"date": "2025-04-18", "name": "Good Friday"},
    {"date": "2025-12-02", "name": "UAE National Day"},
    {"date": "2025-12-25", "name": "Christmas Day"}
]
```

---

## 5. Configuration

### HR MCP Server `.env.example`
```env
AUTH_ISSUER=https://api.asgardeo.io/t/<your-tenant>/oauth2/token
CLIENT_ID=<your-mcp-app-client-id>
JWKS_URL=https://api.asgardeo.io/t/<your-tenant>/oauth2/jwks
```

### Agent Web Server `.env.example`
```env
# Asgardeo OAuth2
ASGARDEO_BASE_URL=https://api.asgardeo.io/t/<your-tenant>
CLIENT_ID=<your-mcp-client-app-client-id>
REDIRECT_URI=http://localhost:5000/oauth/callback

# Agent Credentials
AGENT_ID=<your-agent-id>
AGENT_SECRET=<your-agent-secret>

# LLM
GOOGLE_API_KEY=<your-google-api-key>
MODEL_NAME=gemini-2.5-flash

# MCP Server
MCP_SERVER_URL=http://127.0.0.1:8000/mcp
```

---

## 6. Dependencies

### HR MCP Server (`requirements.txt`)
```
mcp~=1.12.3
PyJWT~=2.10.1
httpx~=0.27.0
pydantic~=2.11.7
python-dotenv~=1.0.0
```

### Agent Web Server (`requirements.txt`)
```
fastapi~=0.115.0
uvicorn~=0.34.0
asgardeo==0.2.1
asgardeo_ai==0.2.2
langchain==1.1.0
langchain-google-genai==3.2.0
langchain-mcp-adapters==0.1.14
python-dotenv~=1.0.0
```

---

## 7. Project Structure

```
smart-employee-agent/
├── README.md
├── REQUIREMENTS.md
├── DESIGN.md
│
├── hr-mcp-server/                     # HR & Leave Management MCP Server
│   ├── main.py                        # FastMCP server, JWT verifier, scoped tools
│   ├── hr_data.py                     # In-memory mock HR data
│   ├── jwt_validator.py               # JWT validation via JWKS
│   ├── requirements.txt
│   └── .env.example
│
└── agent/                             # Smart Employee Agent (Web App)
    ├── main.py                        # FastAPI server, LangChain agent, OAuth handling
    ├── static/
    │   └── index.html                 # Chat UI (HTML + CSS + JS, single file)
    ├── requirements.txt
    └── .env.example
```

---

## 8. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Web framework | FastAPI | Async-native, works well with LangChain async. Serves both API and static files. |
| Frontend | Single HTML file (vanilla JS) | No build tools needed. Keeps it simple for a sample. Self-contained. |
| Auth popup flow | `window.open()` + `postMessage` | User stays on the chat page. Popup handles login, notifies parent on success. No context lost. |
| OAuth callback | FastAPI endpoint `/oauth/callback` | Replaces the standalone `OAuthCallbackServer`. Integrated into the web app. |
| Session state | Module-level dict | Single-user sample. No database/Redis needed. Easy to understand. |
| Escalation detection | Check tool message history for `insufficient_scope` | Natural flow — agent tries, server rejects, app detects and triggers auth. |
| OBO token reuse | Cached in session after first auth | Once authorized, subsequent elevated actions don't prompt again. |
| MCP reconnection | Create new MCP client with OBO token | LangChain MCP adapters bind tokens at connection time. New token = new client. |
| Chat history | Maintained across messages in session | Agent has conversational context (knows prior requests). Reset on page reload. |
