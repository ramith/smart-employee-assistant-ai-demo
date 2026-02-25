# Smart Employee Agent

A sample application demonstrating **dynamic privilege escalation** for AI agents using Asgardeo. An AI agent starts with low-privilege access and dynamically escalates to act on behalf of a manager when elevated actions are required.

The application features a **split-panel UI** with a conversational AI assistant on the left and a live employee dashboard on the right. Changes made through the chat (approving leave, booking IT appointments) are reflected instantly on the dashboard.

This combines both the **Agent-Auth-Flow** and **On-Behalf-Of Flow** in a realistic corporate scenario with HR leave management and IT support scheduling.

## Scenario

An employee uses an AI assistant ("Corporate Concierge") to interact with corporate systems:

1. **Basic queries** (holidays, employee status, available IT slots) work immediately with the agent's own token
2. **Elevated actions** (approve/reject leave, book/cancel IT appointments) trigger an in-browser authorization flow where the user logs in via Asgardeo
3. The agent receives a time-limited **OBO (On-Behalf-Of) token** carrying the user's privileges
4. All actions are audit-logged with both the agent's and user's identity
5. The **dashboard updates live** after each agent interaction

## Architecture

```
Browser (Split-Panel UI)
├── Chat Panel ──►  Agent Web Server (FastAPI + LangChain + Gemini)
│                         ├── MCP Client ──► HR MCP Server (port 8000)
│                         └── MCP Client ──► IT Support MCP Server (port 8001)
│
└── Dashboard  ──►  Agent Web Server (requires user login)
                          ├── httpx + JWT ──► HR MCP /api/leaves (requires hr_read)
                          └── httpx + JWT ──► IT MCP /api/bookings (requires it_read)
```

## Project Structure

```
smart-employee-agent/
├── README.md
├── DESIGN.md
├── REQUIREMENTS.md
├── hr-mcp-server/                  # HR & Leave Management MCP Server
│   ├── main.py                     # FastMCP server + REST dashboard endpoint
│   ├── hr_data.py                  # In-memory mock HR data
│   ├── jwt_validator.py            # JWT validation via JWKS
│   ├── requirements.txt
│   └── .env.example
├── it-support-mcp-server/          # IT Support Appointment MCP Server
│   ├── main.py                     # FastMCP server + REST dashboard endpoint
│   ├── it_support_data.py          # In-memory mock IT data
│   ├── jwt_validator.py            # JWT validation via JWKS
│   ├── requirements.txt
│   └── .env.example
└── agent/                          # Smart Employee Agent (Web App)
    ├── main.py                     # FastAPI + LangChain agent + dashboard proxy
    ├── static/index.html           # Split-panel UI (Chat + Dashboard)
    ├── requirements.txt
    └── .env.example
```

## Prerequisites

- Python 3.10+
- An [Asgardeo](https://asgardeo.io) account
- A [Google AI Studio](https://aistudio.google.com/) API key (for Gemini)

## Asgardeo Configuration

### 1. Register an AI Agent

1. Log in to the [Asgardeo Console](https://console.asgardeo.io)
2. Navigate to **Agents** section
3. Click **+ New Agent**
4. Note down the **Agent ID** and **Agent Secret**

### 2. Create an MCP Client Application

1. Go to **Applications** → **New Application**
2. Select **MCP Client Application**
3. Set the **Redirect URI** to: `http://localhost:5001/oauth/callback`
4. Add an additional **Redirect URI**: `http://localhost:5001/api/user/callback`
5. Note down the **Client ID**

### 3. Configure API Scopes

Ensure the following scopes are configured for the application:

**HR Scopes:**
- `openid`
- `hr_basic` — Basic HR access (holidays, employee status)
- `hr_read` — Read leave requests
- `hr_approve` — Approve/reject leave requests

**IT Support Scopes:**
- `it_basic` — View support categories, technicians, available slots
- `it_read` — View own IT appointments
- `it_manage` — Book, cancel, or reschedule IT appointments

**Scope permissions:**
- The **Agent's default** scopes: `openid`, `hr_basic`
- **User-authorized** scopes (via OBO): `openid`, `hr_basic`, `hr_read`, `hr_approve`, `it_basic`, `it_read`, `it_manage`

## Setup & Run

### Step 1: Set Up the HR MCP Server

```bash
cd hr-mcp-server

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your Asgardeo tenant details:
#   AUTH_ISSUER=https://api.asgardeo.io/t/<your-tenant>/oauth2/token
#   CLIENT_ID=<your-mcp-app-client-id>
#   JWKS_URL=https://api.asgardeo.io/t/<your-tenant>/oauth2/jwks

# Start the HR MCP server (runs on port 8000)
python main.py
```

### Step 2: Set Up the IT Support MCP Server

Open a **new terminal**:

```bash
cd it-support-mcp-server

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with the SAME Asgardeo tenant details as the HR server:
#   AUTH_ISSUER=https://api.asgardeo.io/t/<your-tenant>/oauth2/token
#   CLIENT_ID=<your-mcp-app-client-id>
#   JWKS_URL=https://api.asgardeo.io/t/<your-tenant>/oauth2/jwks

# Start the IT Support MCP server (runs on port 8001)
python main.py
```

### Step 3: Set Up the Agent Web Server

Open a **new terminal**:

```bash
cd agent

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your configuration:
#   ASGARDEO_BASE_URL=https://api.asgardeo.io/t/<your-tenant>
#   CLIENT_ID=<your-mcp-client-app-client-id>
#   REDIRECT_URI=http://localhost:5001/oauth/callback
#   USER_REDIRECT_URI=http://localhost:5001/api/user/callback
#   AGENT_ID=<your-agent-id>
#   AGENT_SECRET=<your-agent-secret>
#   GOOGLE_API_KEY=<your-google-api-key>
#   MODEL_NAME=gemini-2.5-flash
#   MCP_SERVER_URL=http://127.0.0.1:8000/mcp
#   IT_SUPPORT_SERVER_URL=http://127.0.0.1:8001/mcp

# Start the agent web server (runs on port 5001)
python main.py
```

### Step 4: Open the UI

Open your browser and go to: **http://localhost:5001**

You will see a split-panel interface:
- **Left**: Chat panel ("Concierge AI") for conversational interaction
- **Right**: Employee Dashboard (requires login) showing leave requests and IT bookings

Click **"Sign In"** on the dashboard panel to log in with your Asgardeo credentials. This is required to view dashboard data.

## Usage

### Basic Queries (No Authorization Needed)

Try asking:
- "What are the company holidays this year?"
- "Is Sarah Johnson in the office today?"
- "What IT support categories are available?"
- "Show me available IT slots for tomorrow"

These work immediately with the agent's low-privilege token.

### Elevated Actions (Authorization Required)

Try asking:
- "Approve Sarah's annual leave request"
- "What are the pending leave requests?"
- "Book an IT appointment for my laptop screen issue"
- "Cancel appointment APT001"

When the agent needs elevated privileges:

1. The chat shows the agent's message explaining it needs authorization
2. An **"Authorize"** button appears
3. Click it → a popup opens with the Asgardeo login page
4. Log in with your credentials
5. The popup closes automatically
6. The agent retries and completes the action
7. The **dashboard updates immediately** to reflect the change

The header badge changes from **"Agent Access"** (yellow) to **"Manager Access"** (green) after authorization.

## Understanding the Flows

### Agent Token Flow (Startup)
```
Agent Server → Asgardeo: Agent ID + Secret
Asgardeo → Agent Server: Token (scopes: hr_basic)
Agent Server → HR MCP + IT MCP: Connect with agent token
```

### OBO Token Flow (Privilege Escalation)
```
1. Agent tries elevated tool → MCP returns "insufficient_scope"
2. Frontend shows "Authorize" button
3. User clicks → Popup opens Asgardeo login
4. User authenticates → Redirect to /oauth/callback
5. Server exchanges auth code + PKCE verifier → OBO token
6. Agent reconnects to both MCP servers with OBO token → Retries action → Success
7. Dashboard auto-refreshes to show updated data
```

### User Dashboard Login Flow
```
1. Dashboard shows login overlay on page load
2. User clicks "Sign In" → popup opens Asgardeo login (PKCE flow)
3. User authenticates → server gets user token (scopes: hr_read, it_read)
4. Popup closes → frontend fetches GET /api/dashboard with user token
5. Agent server proxies to MCP servers with user's JWT:
   - HR MCP REST: GET http://localhost:8000/api/leaves (requires hr_read)
   - IT MCP REST: GET http://localhost:8001/api/bookings (requires it_read)
6. Returns aggregated data to frontend
7. After each chat, frontend re-fetches to show live updates
```

### Scope-Based Access Control

| Scope | Tools | Token Type |
|-------|-------|------------|
| `hr_basic` | `get_company_holidays`, `get_employee_status` | Agent Token |
| `hr_read` | `get_team_leave_requests`, `get_leave_request_details`, Dashboard `/api/leaves` | OBO Token / User Token |
| `hr_approve` | `approve_leave_request`, `reject_leave_request` | OBO Token |
| *(none)* | `get_support_categories`, `get_available_slots`, `get_technicians` | Any (no scope required) |
| `it_read` | `get_my_appointments`, `get_appointment_details`, Dashboard `/api/bookings` | OBO Token / User Token |
| `it_manage` | `book_appointment`, `cancel_appointment`, `reschedule_appointment` | OBO Token |

### Audit Trail

Both MCP servers log all elevated actions with the token's identity claims:
```
[AUDIT] Leave LR001 approved by AI Agent (on behalf of manager@company.com)
[AUDIT] Appointment APT002 booked by AI Agent (on behalf of user@company.com) for Sarah Johnson
```

## Mock Data

### HR Data

**Employees:** Sarah Johnson, Ahmed Khan, Maria Garcia, James Wilson

**Pending Leave Requests:**
| ID | Employee | Type | Dates | Reason |
|----|----------|------|-------|--------|
| LR001 | Sarah Johnson | Annual Leave | Mar 10-14, 2026 | Family vacation |
| LR002 | Ahmed Khan | Sick Leave | Mar 5-6, 2026 | Medical appointment |
| LR003 | Maria Garcia | Annual Leave | Mar 17-21, 2026 | Personal travel |

### IT Support Data

**Technicians:** Alex Rivera (Hardware), Priya Patel (Software/Network), Omar Hassan (Account/Email/Security), Lisa Chen (Network/Security/General)

**Support Categories:** Hardware Issue, Software Installation, Network & Connectivity, Account & Access, Email & Communication, Security Incident, New Equipment Request, General IT Consultation

**Available Slots:** 16 slots across Feb 25-27, 2026 (30 min each, various technicians)

**Existing Appointments:**
| ID | Employee | Category | Description | Status |
|----|----------|----------|-------------|--------|
| APT001 | Sarah Johnson | Hardware Issue | Laptop screen flickering | Confirmed |

## Troubleshooting

### "Agent error" on startup
- Ensure **both** MCP servers are running (HR on port 8000, IT on port 8001) before starting the agent
- Verify your Asgardeo credentials in all `.env` files

### Dashboard shows login overlay
- This is expected. Click "Sign In" to log in with your Asgardeo credentials.
- The dashboard requires a user token with `hr_read` and `it_read` scopes.

### Dashboard shows "Loading..." after login
- Check that the MCP servers are running and accessible
- Verify the agent can reach `http://127.0.0.1:8000/api/leaves` and `http://127.0.0.1:8001/api/bookings`
- Ensure the user's token has `hr_read` and `it_read` scopes

### Popup blocked
- Allow popups for `localhost:5001` in your browser settings

### OAuth callback error
- Ensure both redirect URIs are configured in Asgardeo:
  - Agent escalation: `http://localhost:5001/oauth/callback`
  - User dashboard login: `http://localhost:5001/api/user/callback`
- Check that all scopes (`hr_basic`, `hr_read`, `hr_approve`, `it_basic`, `it_read`, `it_manage`) are configured in your Asgardeo application

### Token validation fails on MCP server
- Verify the `AUTH_ISSUER`, `CLIENT_ID`, and `JWKS_URL` in both MCP servers' `.env` files
- Ensure the `CLIENT_ID` matches the MCP Client Application the agent is using
- Both MCP servers should use the same Asgardeo tenant and client ID

### IT Support server not responding
- Ensure it-support-mcp-server is running on port 8001 (`python main.py`)
- Check that `.env` exists in the `it-support-mcp-server/` directory (copy from `.env.example`)
