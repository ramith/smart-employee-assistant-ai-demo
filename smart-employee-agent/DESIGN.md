# Smart Employee Agent - System Design

## 1. System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Browser (User)                                  │
│                                                                              │
│  ┌─────────────────────────┐  ┌──────────────────────────────────────────┐  │
│  │     Chat Panel          │  │         Employee Dashboard               │  │
│  │  (Concierge AI)         │  │                                          │  │
│  │  - Message input        │  │  - Login overlay (user must log in)      │  │
│  │  - Chat bubbles         │  │  - My Leave Requests (live table)        │  │
│  │  - Authorize button     │  │  - My IT Support Bookings (live table)   │  │
│  │  - Typing indicator     │  │  - Auto-refreshes after each chat        │  │
│  └────────┬────────────────┘  └──────────┬───────────────────────────────┘  │
│           │ POST /api/chat               │ GET /api/dashboard               │
│           │                              │ (requires user login token)      │
└───────────┼──────────────────────────────┼──────────────────────────────────┘
            │                              │
            ▼                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                      Agent Web Server (FastAPI)                               │
│                      http://localhost:5001                                    │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  Endpoints                                                             │  │
│  │                                                                        │  │
│  │  GET  /                 → Serve split-panel UI (static HTML/JS/CSS)   │  │
│  │  POST /api/chat         → Process user message via LLM agent          │  │
│  │  GET  /api/dashboard    → Aggregate data from both MCP servers        │  │
│  │                           (requires user login token)                  │  │
│  │  Agent Escalation:                                                     │  │
│  │  GET  /api/auth/url     → Generate PKCE auth URL for OBO              │  │
│  │  GET  /oauth/callback   → Handle Asgardeo redirect, get OBO token     │  │
│  │  GET  /api/auth/status  → Check if OBO token is available             │  │
│  │  GET  /api/auth/pending → Get the pending message that triggered auth  │  │
│  │                                                                        │  │
│  │  User Dashboard Login:                                                 │  │
│  │  GET  /api/user/login   → Generate PKCE auth URL for user login       │  │
│  │  GET  /api/user/callback→ Handle Asgardeo redirect for user login     │  │
│  │  GET  /api/user/status  → Check user login status & scopes            │  │
│  │  POST /api/user/logout  → Clear user dashboard session                │  │
│  └────────┬──────────────────────────────┬────────────────────────────────┘  │
│           │                              │                                   │
│  ┌────────▼────────────┐  ┌──────────────▼──────────────────────────────┐   │
│  │  LangChain Agent    │  │  Session State (in-memory)                  │   │
│  │  + Gemini LLM       │  │  - agent_token (startup)                    │   │
│  │  + MCP Tools (14)   │  │  - obo_token (after agent escalation)       │   │
│  │  + Chat History     │  │  - PKCE state & code_verifier               │   │
│  └────────┬────────────┘  │  - chat_history, pending_message            │   │
│           │               │  - user_token (dashboard login)              │   │
│           │               │  - user PKCE state & code_verifier           │   │
│           │               └─────────────────────────────────────────────┘   │
│  ┌────────▼────────────┐                                                    │
│  │  MultiServerMCP     │ ◄── Connects to both MCP servers                   │
│  │  Client (HTTP)      │     with Bearer token                              │
│  └───┬─────────────┬───┘                                                    │
│      │             │         ┌──────────────────────────────────────────┐    │
│      │             │         │  Dashboard Proxy (httpx)                 │    │
│      │             │         │  GET /api/leaves  → HR MCP REST          │    │
│      │             │         │  GET /api/bookings → IT MCP REST         │    │
│      │             │         │  (sends user_token for authentication)   │    │
│      │             │         └──────────────────────────────────────────┘    │
└──────┼─────────────┼────────────────────────────────────────────────────────┘
       │ MCP + JWT   │ MCP + JWT
       ▼             ▼
┌───────────────────────┐  ┌─────────────────────────────────────────────┐
│  HR MCP Server        │  │  IT Support MCP Server                      │
│  http://localhost:8000│  │  http://localhost:8001                      │
│                       │  │                                             │
│  ASGI Middleware:     │  │  ASGI Middleware:                           │
│  /api/leaves (REST)   │  │  /api/bookings (REST)                      │
│    JWT + hr_read      │  │    JWT + it_read                            │
│  /mcp (MCP protocol)  │  │  /mcp (MCP protocol)                       │
│                       │  │                                             │
│  JWT + Scope Control  │  │  JWT + Scope Control                       │
│  hr_basic, hr_read,   │  │  it_read, it_manage                        │
│  hr_approve           │  │                                             │
│                       │  │                                             │
│  In-Memory HR Data    │  │  In-Memory IT Data                         │
│  (employees, leaves,  │  │  (technicians, slots,                      │
│   holidays)           │  │   appointments, categories)                │
└───────────────────────┘  └─────────────────────────────────────────────┘
```

---

## 2. Component Design

### 2.1 HR MCP Server (`hr-mcp-server/`)

The HR MCP server exposes leave management tools with scope-based access control. It also provides a REST endpoint for the dashboard.

#### Files

| File | Responsibility |
|------|---------------|
| `main.py` | FastMCP server setup, JWT verifier, scoped tools, Starlette wrapper with REST endpoint |
| `hr_data.py` | In-memory mock data and data access functions |
| `jwt_validator.py` | JWT validation using JWKS |
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

#### REST Dashboard Endpoint

The MCP server is wrapped with ASGI middleware (`DashboardMiddleware`) that intercepts the `/api/leaves` path before it reaches the MCP app:

```
GET /api/leaves → Returns all leave requests (requires JWT with hr_read scope)
GET /mcp        → MCP protocol (with JWT auth middleware)
```

The middleware validates the JWT token and checks for the `hr_read` scope. This approach avoids wrapping the MCP Starlette app in a parent Starlette app, which would break its lifespan (session manager task group initialization).

---

### 2.2 IT Support MCP Server (`it-support-mcp-server/`)

The IT Support MCP server exposes appointment management tools with scope-based access control.

#### Files

| File | Responsibility |
|------|---------------|
| `main.py` | FastMCP server setup, JWT verifier, scoped tools, Starlette wrapper with REST endpoint |
| `it_support_data.py` | In-memory mock data (technicians, slots, appointments, categories) |
| `jwt_validator.py` | JWT validation using JWKS |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |

#### Tool Specifications

| Tool | Scope | Input | Output |
|------|-------|-------|--------|
| `get_support_categories()` | *(none)* | none | `{ "categories": [...] }` |
| `get_available_slots(date, technician_id)` | *(none)* | optional filters | `{ "slots": [...] }` or `{ "message": "No available slots...", "slots": [] }` |
| `get_technicians(category_name)` | *(none)* | optional filter | `{ "technicians": [...] }` |
| `get_my_appointments(employee_name)` | `it_read` | name str | `{ "appointments": [...] }` or `{ "message": "No appointments found...", "appointments": [] }` |
| `get_appointment_details(appointment_id)` | `it_read` | ID str | `{ "appointment_id": "...", ... }` |
| `book_appointment(employee_name, category_id, slot_id, description)` | `it_manage` | fields | `{ "success": true, ... }` |
| `cancel_appointment(appointment_id)` | `it_manage` | ID str | `{ "success": true, ... }` |
| `reschedule_appointment(appointment_id, new_slot_id)` | `it_manage` | ID str, slot str | `{ "success": true, ... }` |

#### REST Dashboard Endpoint

The MCP server is wrapped with ASGI middleware (`DashboardMiddleware`) that intercepts the `/api/bookings` path:

```
GET /api/bookings → Returns all IT appointments (requires JWT with it_read scope)
GET /mcp          → MCP protocol (with JWT auth middleware)
```

The middleware validates the JWT token and checks for the `it_read` scope.

---

### 2.3 Agent Web Server (`agent/`)

A **FastAPI** web server that serves the split-panel UI, hosts the LangChain AI agent, and proxies dashboard data from both MCP servers.

#### Files

| File | Responsibility |
|------|---------------|
| `main.py` | FastAPI app, API endpoints, agent orchestration, dashboard proxy, OAuth handling |
| `static/index.html` | Split-panel web UI (Chat + Dashboard, HTML + CSS + JS, single file) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |

#### API Endpoints

**`GET /`** — Serve the split-panel UI
- Returns the static `index.html` file.

**`POST /api/chat`** — Process a user message
- Request: `{ "message": "Approve Sarah's leave" }`
- Response (normal): `{ "type": "response", "message": "...", "refresh_dashboard": true }`
- Response (auth needed): `{ "type": "auth_required", "message": "I need your authorization..." }`
- The backend invokes the LangChain agent, checks for `insufficient_scope` in tool results, and returns the appropriate response type.
- Every successful response includes `refresh_dashboard: true` to trigger a dashboard refresh.

**`GET /api/dashboard`** — Aggregate dashboard data
- **Requires user login** (returns 401 if `user_token` is not set).
- Fetches from both MCP servers' REST endpoints via `httpx`, sending the user's token:
  - `GET http://localhost:8000/api/leaves` (requires `hr_read` scope) → leave requests
  - `GET http://localhost:8001/api/bookings` (requires `it_read` scope) → IT appointments
- Response: `{ "leaves": [...], "bookings": [...], "hr_accessible": true, "it_accessible": true }`
- Handles token expiration (returns `{ "error": "token_expired" }` with 401 status).
- Called by the frontend after user login and after each chat interaction.

**Agent Escalation Endpoints:**

**`GET /api/auth/url`** — Generate PKCE authorization URL for OBO flow
- Requests scopes for both HR and IT: `openid`, `hr_basic`, `hr_read`, `hr_approve`, `it_basic`, `it_read`, `it_manage`
- Response: `{ "auth_url": "https://api.asgardeo.io/t/.../authorize?..." }`

**`GET /oauth/callback?code=...&state=...`** — Handle OAuth redirect for agent escalation
- Exchanges the authorization code + PKCE verifier for an OBO token.
- Returns an HTML page that sends `postMessage({ type: 'auth_success' })` to the opener window and closes the popup.

**`GET /api/auth/status`** — Check agent escalation status
- Response: `{ "authorized": true/false }`

**`GET /api/auth/pending`** — Get the pending message that triggered auth
- Response: `{ "pending_message": "Approve Sarah's leave" }` or `{ "pending_message": null }`

**User Dashboard Login Endpoints:**

**`GET /api/user/login`** — Generate PKCE authorization URL for user dashboard login
- Uses a separate redirect URI (`USER_REDIRECT_URI`) and requests scopes: `openid`, `hr_read`, `it_read`.
- Response: `{ "auth_url": "https://api.asgardeo.io/t/.../authorize?..." }`

**`GET /api/user/callback?code=...&state=...`** — Handle OAuth redirect for user login
- Exchanges the authorization code + PKCE verifier for a user token.
- Returns an HTML page that sends `postMessage({ type: 'user_login_success' })` to the opener window.

**`GET /api/user/status`** — Check user login status and scopes
- Response: `{ "logged_in": true, "scopes": [...], "has_hr_read": true, "has_it_read": true }`

**`POST /api/user/logout`** — Clear the user dashboard session
- Response: `{ "success": true }`

#### Session State (In-Memory)

```python
session = {
    "agent_token": None,       # Obtained on startup (agent-auth-flow)
    "obo_token": None,         # Obtained after agent escalation (OBO flow)
    "code_verifier": None,     # PKCE verifier during agent escalation
    "pkce_state": None,        # CSRF state parameter for agent escalation
    "chat_history": [],        # LangChain message history
    "pending_message": None,   # Message to retry after auth
    "mcp_client": None,        # MultiServerMCPClient instance
    # User dashboard login (separate from agent escalation)
    "user_token": None,        # User's own token for dashboard access
    "user_code_verifier": None,# PKCE verifier during user login
    "user_pkce_state": None,   # CSRF state parameter for user login
}
```

#### MCP Client Configuration

The agent connects to both MCP servers using `MultiServerMCPClient`:

```python
session["mcp_client"] = MultiServerMCPClient({
    "hr_server": {
        "transport": "streamable_http",
        "url": "http://127.0.0.1:8000/mcp",
        "headers": {"Authorization": f"Bearer {access_token}"},
    },
    "it_server": {
        "transport": "streamable_http",
        "url": "http://127.0.0.1:8001/mcp",
        "headers": {"Authorization": f"Bearer {access_token}"},
    }
})
```

---

### 2.4 Split-Panel Web UI (`agent/static/index.html`)

A single-file HTML page with embedded CSS and JavaScript. No build tools required.

#### UI Layout

```
┌───────────────────────────────┬──────────────────────────────────────────────┐
│                               │                                              │
│  ┌─ Chat Header ────────────┐│  ┌─ Dashboard Header ──────────────────────┐ │
│  │  🧳 Concierge AI  [badge]││  │  Employee Dashboard    Welcome │ Sign Out│ │
│  └──────────────────────────┘│  └──────────────────────────────────────────┘ │
│                               │                                              │
│  ┌─ Chat Messages ─────────┐│  ┌─ Leave Requests ──────────────────────────┐│
│  │                          ││  │  📅 My Leave Requests                     ││
│  │  🧳 Hello! I'm your     ││  │  ┌──────┬──────────┬──────┬────┬───────┐ ││
│  │  corporate concierge.    ││  │  │ ID   │ Employee │ Type │Date│Status │ ││
│  │  How can I assist you?   ││  │  ├──────┼──────────┼──────┼────┼───────┤ ││
│  │                          ││  │  │LR001 │ Sarah J. │Annual│3/10│Pending│ ││
│  │       Book an IT slot    ││  │  │LR002 │ Ahmed K. │ Sick │3/05│Pending│ ││
│  │       for my laptop ─┐   ││  │  └──────┴──────────┴──────┴────┴───────┘ ││
│  │                      │   ││  └───────────────────────────────────────────┘│
│  │  🧳 I've booked an  │   ││                                               │
│  │  appointment for you.│   ││  ┌─ IT Bookings ─────────────────────────────┐│
│  │                      │───┼┼─►│  🖥 My IT Support Bookings                ││
│  │  [auto-refresh] ─────┼───┘│  │  ┌────┬──────────┬────┬────┬───────────┐ ││
│  │                      │   ││  │  │ ID │ Category │Date│Time│Technician │ ││
│  └──────────────────────┘   ││  │  │APT2│ Hardware │2/25│9:00│Alex Rivera│ ││
│                               │  │  └────┴──────────┴────┴────┴───────────┘ ││
│  ┌─ Input ──────────────────┐│  └───────────────────────────────────────────┘│
│  │  Ask about leave or...  ➤││                                               │
│  └──────────────────────────┘│                                               │
└───────────────────────────────┴──────────────────────────────────────────────┘
```

#### Frontend Behavior

1. **Page load**: Dashboard shows a login overlay. User must log in to view dashboard data.
2. **User login**: Click "Sign In" → opens Asgardeo login popup via `/api/user/login`. On success, dashboard data is fetched.
3. **Dashboard data**: After user login, fetch `GET /api/dashboard` (with user token) to populate leave requests and IT bookings tables.
4. **Send message**: POST to `/api/chat` with the message text.
5. **Show response**: Append agent's response as a chat bubble with avatar and markdown rendering.
6. **Refresh dashboard**: After every successful chat response (`refresh_dashboard: true`), re-fetch `/api/dashboard` to update both tables live.
7. **Handle auth_required**: When response type is `auth_required`:
   - Show the agent's message explaining auth is needed.
   - Show an **"Authorize"** button in the chat.
   - When clicked → call `GET /api/auth/url` to get the PKCE auth URL.
   - Open the auth URL in a **popup window**.
   - Listen for `postMessage` from the popup confirming auth success.
   - On success → update auth badge, automatically retry the pending message.
8. **Loading state**: Show a typing indicator (3 bouncing dots) while waiting for the agent.
9. **Sign Out**: Calls `POST /api/user/logout` and reloads the page, resetting client-side state.

#### Live Dashboard Updates

The dashboard panels update after each chat interaction:

```
User: "Approve Sarah's leave"
  → Agent approves via MCP → response sent to chat
  → Frontend calls GET /api/dashboard
  → Leave table shows LR001 status changed from "Pending" to "Approved"

User: "Book an IT slot for my laptop issue"
  → Agent books via MCP → response sent to chat
  → Frontend calls GET /api/dashboard
  → Bookings table shows new appointment row
```

---

## 3. Authentication Flow Sequences

### 3.1 Startup & User Dashboard Login

```
Browser              Agent Server              Asgardeo         HR MCP / IT MCP
   │                      │                       │                    │
   │                      │── Agent Credentials ──►│                    │
   │                      │   (agent_id + secret)  │                    │
   │                      │◄── Agent Token ────────│                    │
   │                      │   (scopes: hr_basic)   │                    │
   │                      │                        │                    │
   │                      │── Connect MCP (x2) ─────────────────────►│
   │                      │   (HR server + IT server)                 │
   │                      │◄── Tools Available (14 tools) ───────────│
   │                      │                        │                    │
   │── GET / ────────────►│                        │                    │
   │◄── Split-Panel UI ───│                        │                    │
   │                      │                        │                    │
   │ [Dashboard shows login overlay]               │                    │
   │                      │                        │                    │
   │── GET /api/user/login►│                        │                    │
   │◄── { auth_url: ... } │                        │                    │
   │                      │                        │                    │
   │ [Opens popup to      │                        │                    │
   │  Asgardeo login]     │                        │                    │
   │         ┌────────────┼────────────────────────┤                    │
   │  Popup: │ User logs in│                       │                    │
   │         │    ↓        │                        │                    │
   │         │ Redirect to │                        │                    │
   │         │ /api/user/  │                        │                    │
   │         │ callback    │                        │                    │
   │         └────────────►│                        │                    │
   │                      │── Exchange code ───────►│                    │
   │                      │   + PKCE verifier       │                    │
   │                      │◄── User Token ──────────│                    │
   │                      │   (hr_read, it_read)    │                    │
   │                      │                        │                    │
   │◄── postMessage ──────│                        │                    │
   │   ("user_login_success")                      │                    │
   │ [Popup closes]       │                        │                    │
   │                      │                        │                    │
   │── GET /api/dashboard ►│                        │                    │
   │                      │── GET /api/leaves ──────────────────────►│
   │                      │   (Bearer: user_token)                   │
   │                      │◄── { leaves: [...] } ──────────────────│
   │                      │── GET /api/bookings ───────────────────►│
   │                      │   (Bearer: user_token)                   │
   │                      │◄── { bookings: [...] } ────────────────│
   │◄── { leaves, bookings }                       │                    │
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
   │◄── { type: "response", message: "Here are...",               │
   │      refresh_dashboard: true }                                │
   │                      │                                        │
   │── GET /api/dashboard ►│  (auto-refresh)                       │
   │◄── { leaves, bookings }                                       │
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
   │                      │   (scopes: hr_*, it_*)                 │
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
   │                      │   (hr_basic,hr_read,│                    │
   │                      │    hr_approve,      │                    │
   │                      │    it_basic,it_read,│                    │
   │                      │    it_manage)       │                    │
   │                      │                    │                    │
   │◄── postMessage ──────│                    │                    │
   │   ("auth_success")   │                    │                    │
   │ [Popup closes]       │                    │                    │
   │ [Badge → "Manager"]  │                    │                    │
   │                      │                    │                    │
   │── POST /api/chat ───►│                    │                    │
   │   "Approve Sarah's   │                    │                    │
   │    leave" (retry)    │                    │                    │
   │                      │── Reconnect MCP ───────────────────────►│
   │                      │   (Bearer: obo_token)                  │
   │                      │── approve_leave() ─────────────────────►│
   │                      │◄── { success: true } ──────────────────│
   │                      │                    │                    │
   │◄── { type: "response", message: "Approved",                   │
   │      refresh_dashboard: true }                                 │
   │                      │                    │                    │
   │── GET /api/dashboard ►│  (auto-refresh)   │                    │
   │◄── { leaves: [{..., status: "Approved"}], ... }               │
   │ [Dashboard shows updated status]                               │
```

---

## 4. Data Model

### 4.1 In-Memory HR Data (`hr_data.py`)

```python
employees = {
    "EMP001": {
        "id": "EMP001", "name": "Sarah Johnson",
        "department": "Engineering", "role": "Software Engineer",
        "status": "in-office", "leave_balance": {"annual": 18, "sick": 10}
    },
    "EMP002": { ... },  # Ahmed Khan
    "EMP003": { ... },  # Maria Garcia
    "EMP004": { ... },  # James Wilson
}

leave_requests = {
    "LR001": { "request_id": "LR001", "employee_id": "EMP001",
               "type": "Annual Leave", "start_date": "2026-03-10",
               "end_date": "2026-03-14", "days_requested": 5,
               "status": "Pending", "reason": "Family vacation" },
    "LR002": { ... },  # Ahmed Khan - Sick Leave
    "LR003": { ... },  # Maria Garcia - Annual Leave
}

company_holidays = [
    {"date": "2026-01-01", "name": "New Year's Day"},
    {"date": "2026-03-20", "name": "Eid Al Fitr (expected)"},
    {"date": "2026-05-27", "name": "Arafat Day (expected)"},
    {"date": "2026-05-28", "name": "Eid Al Adha (expected)"},
    {"date": "2026-07-18", "name": "Islamic New Year (expected)"},
    {"date": "2026-12-01", "name": "Commemoration Day"},
    {"date": "2026-12-02", "name": "UAE National Day"},
]
```

### 4.2 In-Memory IT Support Data (`it_support_data.py`)

```python
support_categories = [
    {"id": "CAT001", "name": "Hardware Issue", ...},
    {"id": "CAT002", "name": "Software Installation", ...},
    {"id": "CAT003", "name": "Network & Connectivity", ...},
    {"id": "CAT004", "name": "Account & Access", ...},
    {"id": "CAT005", "name": "Email & Communication", ...},
    {"id": "CAT006", "name": "Security Incident", ...},
    {"id": "CAT007", "name": "New Equipment Request", ...},
    {"id": "CAT008", "name": "General IT Consultation", ...},
]

technicians = {
    "TECH001": { "name": "Alex Rivera", "specializations": ["Hardware Issue", ...] },
    "TECH002": { "name": "Priya Patel", ... },
    "TECH003": { "name": "Omar Hassan", ... },
    "TECH004": { "name": "Lisa Chen", ... },
}

available_slots = [
    {"slot_id": "SLOT001", "date": "2026-02-25", "time": "09:00", ...},
    # ... 16 slots across 3 days
]

appointments = {
    "APT001": {
        "employee_name": "Sarah Johnson",
        "category_id": "CAT001",
        "description": "Laptop screen flickering intermittently",
        "status": "Confirmed", ...
    },
}
```

---

## 5. Configuration

### HR MCP Server `.env.example`
```env
AUTH_ISSUER=https://api.asgardeo.io/t/<your-tenant>/oauth2/token
CLIENT_ID=<your-mcp-app-client-id>
JWKS_URL=https://api.asgardeo.io/t/<your-tenant>/oauth2/jwks
```

### IT Support MCP Server `.env.example`
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
REDIRECT_URI=http://localhost:5001/oauth/callback
USER_REDIRECT_URI=http://localhost:5001/api/user/callback

# Agent Credentials
AGENT_ID=<your-agent-id>
AGENT_SECRET=<your-agent-secret>

# LLM
GOOGLE_API_KEY=<your-google-api-key>
MODEL_NAME=gemini-2.5-flash

# MCP Servers
MCP_SERVER_URL=http://127.0.0.1:8000/mcp
IT_SUPPORT_SERVER_URL=http://127.0.0.1:8001/mcp
```

---

## 6. Dependencies

### HR MCP Server (`requirements.txt`)
```
mcp~=1.12.3
PyJWT[crypto]~=2.10.1
httpx~=0.27.0
pydantic~=2.11.7
python-dotenv~=1.0.0
```

### IT Support MCP Server (`requirements.txt`)
```
mcp~=1.12.3
PyJWT[crypto]~=2.10.1
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
httpx~=0.27.0
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
│   ├── main.py                        # FastMCP server + Starlette wrapper with REST endpoint
│   ├── hr_data.py                     # In-memory mock HR data
│   ├── jwt_validator.py               # JWT validation via JWKS
│   ├── requirements.txt
│   └── .env.example
│
├── it-support-mcp-server/             # IT Support Appointment MCP Server
│   ├── main.py                        # FastMCP server + Starlette wrapper with REST endpoint
│   ├── it_support_data.py             # In-memory mock IT data
│   ├── jwt_validator.py               # JWT validation via JWKS
│   ├── requirements.txt
│   └── .env.example
│
└── agent/                             # Smart Employee Agent (Web App)
    ├── main.py                        # FastAPI server, LangChain agent, dashboard proxy, OAuth
    ├── static/
    │   └── index.html                 # Split-panel UI (Chat + Dashboard, HTML + CSS + JS)
    ├── requirements.txt
    └── .env.example
```

---

## 8. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Web framework | FastAPI | Async-native, works well with LangChain async. Serves both API and static files. |
| Frontend | Single HTML file (vanilla JS + marked.js) | No build tools needed. Keeps it simple for a sample. Self-contained. Markdown rendering for agent responses. |
| UI layout | Split-panel (chat + dashboard) | Shows both conversational AI and live data in one view. Changes from chat are visible immediately on the dashboard. |
| Dashboard auth | Separate user login (PKCE flow) | Dashboard data requires authentication. User logs in via a popup to get a token with `hr_read` and `it_read` scopes. Independent of agent escalation. |
| Dashboard data source | REST endpoints on MCP servers (ASGI middleware) | ASGI middleware (`DashboardMiddleware`) intercepts REST paths before the MCP app, validates JWT, and checks scopes. Avoids wrapping MCP app in a parent Starlette app which would break lifespan. |
| Dashboard refresh | After every chat response | Simple and reliable. The frontend re-fetches `/api/dashboard` after each agent interaction. |
| Auth popup flow | `window.open()` + `postMessage` | User stays on the chat page. Popup handles login, notifies parent on success. No context lost. Used for both user login and agent escalation. |
| OAuth callbacks | Two separate redirect URIs | Agent escalation uses `/oauth/callback`, user dashboard login uses `/api/user/callback`. Both integrated into the web app. |
| Session state | Module-level dict | Single-user sample. No database/Redis needed. Easy to understand. |
| Escalation detection | Check tool message history for `insufficient_scope` | Natural flow -- agent tries, server rejects, app detects and triggers auth. |
| OBO token reuse | Cached in session after first auth | Once authorized, subsequent elevated actions don't prompt again. |
| OBO scopes | All HR + IT scopes combined | Single authorization grants access to both systems. |
| MCP reconnection | Create new MCP client with OBO token | LangChain MCP adapters bind tokens at connection time. New token = new client. |
| Multi-server MCP | `MultiServerMCPClient` | LangChain adapter supports connecting to multiple MCP servers and presenting all tools to the agent as a unified set. |
| IT basic tools | No scope required | `get_support_categories`, `get_available_slots`, `get_technicians` are publicly accessible without any scope, allowing the agent to browse available options before escalation. |
