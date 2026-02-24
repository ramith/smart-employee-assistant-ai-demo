# Smart Employee Agent

A sample application demonstrating **dynamic privilege escalation** for AI agents using Asgardeo. An AI agent starts with low-privilege access and dynamically escalates to act on behalf of a manager when an elevated action (leave approval) is required.

This combines both the **Agent-Auth-Flow** and **On-Behalf-Of Flow** in a single, realistic HR scenario with a web-based chat UI.

## Scenario

A Department Manager uses an AI assistant to interact with the company's HR system:

1. **Basic queries** (holidays, employee status) work immediately with the agent's own token
2. **Elevated actions** (approve/reject leave) trigger an in-browser authorization flow where the manager logs in via Asgardeo
3. The agent receives a time-limited **OBO (On-Behalf-Of) token** carrying the manager's privileges
4. All actions are audit-logged with both the agent's and manager's identity

## Architecture

```
Browser (Chat UI)  ──►  Agent Web Server (FastAPI + LangChain + Gemini)  ──►  HR MCP Server
       │                          │                                              │
       │                          │── Agent Token (hr_basic) ──────────────────►│
       │                          │                                              │
       │ [Popup: Asgardeo Login]  │── OBO Token (hr_basic, hr_read, hr_approve)►│
       │                          │                                              │
       └──────────────────────────┴──────────────────────────────────────────────┘
```

## Project Structure

```
smart-employee-agent/
├── README.md
├── hr-mcp-server/                  # HR & Leave Management MCP Server
│   ├── main.py                     # FastMCP server with scoped tools
│   ├── hr_data.py                  # In-memory mock HR data
│   ├── jwt_validator.py            # JWT validation via JWKS
│   ├── requirements.txt
│   └── .env.example
└── agent/                          # Smart Employee Agent (Web App)
    ├── main.py                     # FastAPI + LangChain agent
    ├── static/index.html           # Chat web UI
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
3. Set the **Redirect URI** to: `http://localhost:5000/oauth/callback`
4. Note down the **Client ID**

### 3. Configure API Scopes

Ensure the following scopes are configured for the application:
- `openid`
- `hr_basic` — Basic HR access (holidays, employee status)
- `hr_read` — Read leave requests
- `hr_approve` — Approve/reject leave requests

**Scope permissions:**
- The **Agent's default** scopes: `openid`, `hr_basic`
- **User-authorized** scopes (via OBO): `openid`, `hr_basic`, `hr_read`, `hr_approve`

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

# Start the MCP server (runs on port 8000)
python main.py
```

### Step 2: Set Up the Agent Web Server

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
#   REDIRECT_URI=http://localhost:5000/oauth/callback
#   AGENT_ID=<your-agent-id>
#   AGENT_SECRET=<your-agent-secret>
#   GOOGLE_API_KEY=<your-google-api-key>
#   MODEL_NAME=gemini-2.5-flash
#   MCP_SERVER_URL=http://127.0.0.1:8000/mcp

# Start the agent web server (runs on port 5000)
python main.py
```

### Step 3: Open the Chat UI

Open your browser and go to: **http://localhost:5000**

## Usage

### Basic Queries (No Authorization Needed)

Try asking:
- "What are the company holidays this year?"
- "Is Sarah Johnson in the office today?"

These work immediately with the agent's low-privilege token.

### Elevated Actions (Authorization Required)

Try asking:
- "Approve Sarah's annual leave request"
- "What are the pending leave requests?"

When the agent needs elevated privileges:

1. The chat shows the agent's message explaining it needs authorization
2. An **"Authorize"** button appears
3. Click it → a popup opens with the Asgardeo login page
4. Log in with your manager credentials
5. The popup closes automatically
6. The agent retries and completes the action

The header badge changes from **"Agent Access"** (yellow) to **"Manager Access"** (green) after authorization.

## Understanding the Flows

### Agent Token Flow (Startup)
```
Agent Server → Asgardeo: Agent ID + Secret
Asgardeo → Agent Server: Token (scopes: hr_basic)
Agent Server → MCP Server: Connect with agent token
```

### OBO Token Flow (Privilege Escalation)
```
1. Agent tries elevated tool → MCP returns "insufficient_scope"
2. Frontend shows "Authorize" button
3. User clicks → Popup opens Asgardeo login
4. User authenticates → Redirect to /oauth/callback
5. Server exchanges auth code + PKCE verifier → OBO token
6. Agent reconnects to MCP with OBO token → Retries action → Success
```

### Scope-Based Access Control

| Scope | Tools | Token Type |
|-------|-------|------------|
| `hr_basic` | `get_company_holidays`, `get_employee_status` | Agent Token |
| `hr_read` | `get_team_leave_requests`, `get_leave_request_details` | OBO Token |
| `hr_approve` | `approve_leave_request`, `reject_leave_request` | OBO Token |

### Audit Trail

The MCP server logs all elevated actions with the token's identity claims:
```
[AUDIT] Leave LR001 approved by AI Agent (on behalf of manager@company.com)
```

## Mock Data

The HR MCP server comes pre-loaded with sample data:

**Employees:** Sarah Johnson, Ahmed Khan, Maria Garcia, James Wilson

**Pending Leave Requests:**
| ID | Employee | Type | Dates | Reason |
|----|----------|------|-------|--------|
| LR001 | Sarah Johnson | Annual Leave | Mar 10-14, 2026 | Family vacation |
| LR002 | Ahmed Khan | Sick Leave | Mar 5-6, 2026 | Medical appointment |
| LR003 | Maria Garcia | Annual Leave | Mar 17-21, 2026 | Personal travel |

## Troubleshooting

### "Agent error" on startup
- Ensure the MCP server is running on port 8000 before starting the agent
- Verify your Asgardeo credentials in the `.env` files

### Popup blocked
- Allow popups for `localhost:5000` in your browser settings

### OAuth callback error
- Ensure the redirect URI in Asgardeo matches exactly: `http://localhost:5000/oauth/callback`
- Check that the scopes (`hr_basic`, `hr_read`, `hr_approve`) are configured in your Asgardeo application

### Token validation fails on MCP server
- Verify the `AUTH_ISSUER`, `CLIENT_ID`, and `JWKS_URL` in the MCP server's `.env`
- Ensure the MCP server's `CLIENT_ID` matches the application the agent is using
