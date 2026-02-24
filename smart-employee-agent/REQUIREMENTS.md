# Smart Employee Agent - Requirements Document

## Overview

A sample application demonstrating **dynamic privilege escalation** for AI agents using Asgardeo/Identity Server. The AI agent starts with low-privilege access (its own identity) and dynamically escalates to act on behalf of a manager when an elevated action (e.g., leave approval) is required.

This combines both the **Agent-Auth-Flow** and **On-Behalf-Of Flow** in a single, realistic HR scenario.

---

## Scenario

**The Smart Employee Assistant** helps a Department Manager interact with the company's HR & Leave Management system.

- **User**: Department Manager (high privileges - can view team data, approve/reject leave)
- **AI Agent**: Smart Employee Assistant (low baseline privileges - can only read public data)
- **Shared Backend**: HR & Leave Management MCP Server

### User Story

> As a Department Manager, I want to ask my AI assistant to approve a team member's leave request, and have the assistant securely escalate its privileges by requesting my authorization before performing the action.

---

## Functional Requirements

### FR-1: HR & Leave Management MCP Server

A secured MCP server that acts as the HR backend, exposing the following tools:

| Tool | Description | Required Scope |
|------|-------------|----------------|
| `get_company_holidays` | Returns the company holiday calendar | `hr_basic` |
| `get_employee_status` | Check if an employee is in-office or out-of-office | `hr_basic` |
| `get_team_leave_requests` | List pending leave requests for the manager's team | `hr_read` |
| `get_leave_request_details` | Get detailed info about a specific leave request | `hr_read` |
| `approve_leave_request` | Approve a pending leave request | `hr_approve` |
| `reject_leave_request` | Reject a pending leave request with a reason | `hr_approve` |

**Access control rules:**
- Agent token (agent-auth-flow) is granted: `hr_basic` scope only
- OBO token (on-behalf-of-flow with manager) is granted: `hr_basic`, `hr_read`, `hr_approve` scopes

**Data:**
- The MCP server uses in-memory mock data (no database required)
- Pre-populated with sample employees, leave balances, and pending leave requests

**Token validation:**
- JWT validation using Asgardeo JWKS endpoint (follows existing `mcp-auth` pattern)
- Scope-based access control on each tool
- Returns `403 Forbidden` (or error response) when token lacks required scope

### FR-2: AI Agent (Smart Employee Assistant)

A LangChain-based agent powered by Gemini LLM that:

1. **Starts with agent-level authentication**
   - On startup, authenticates using Agent ID + Agent Secret (agent-auth-flow)
   - Connects to the HR MCP server with its low-privilege agent token
   - Can answer basic questions (holiday calendar, employee status)

2. **Detects when elevated privileges are needed**
   - When the user requests an action that requires higher privileges (e.g., "approve Sarah's leave"), the agent recognizes it needs manager-level authorization
   - The agent informs the user that authorization is required

3. **Triggers On-Behalf-Of flow for privilege escalation**
   - Initiates the PKCE authorization flow
   - Opens the browser for the manager to authenticate via Asgardeo
   - Captures the authorization code via a local callback server
   - Exchanges for an OBO token carrying the manager's privileges

4. **Executes the elevated action**
   - Creates a new MCP connection using the OBO token
   - Calls the elevated tool (e.g., `approve_leave_request`)
   - Reports the result back to the user

5. **Maintains audit context**
   - The OBO token carries both the agent's identity and the manager's identity
   - The MCP server logs which agent acted on behalf of which user

### FR-3: Interactive Chat Loop

- The agent runs in an interactive CLI chat loop
- The user can ask multiple questions in a session
- Low-privilege queries are answered immediately (using agent token)
- High-privilege actions trigger the OBO flow only when needed
- After the OBO token is obtained, it is reused for subsequent elevated actions within the session (until it expires)

---

## Non-Functional Requirements

### NFR-1: Technology Stack
- **Language**: Python 3.10+
- **Agent Framework**: LangChain with `langchain-mcp-adapters`
- **LLM**: Google Gemini (via `langchain-google-genai`)
- **MCP Server**: FastMCP (from `mcp` package)
- **Auth SDK**: `asgardeo` and `asgardeo_ai` packages
- **Token Validation**: PyJWT with JWKS

### NFR-2: Consistency with Existing Samples
- Follow the same project structure, naming conventions, and patterns as `agent-identity/python/`
- Use `.env` for configuration (with `.env.example` template)
- Reuse `OAuthCallbackServer` pattern from existing OBO samples

### NFR-3: Security
- Agent token has minimal scopes (principle of least privilege)
- OBO token is time-limited
- All tokens validated via JWKS (asymmetric signatures)
- Scope-based access control enforced at the MCP server level
- Audit trail for all elevated actions

---

## Mock Data

### Employees
| ID | Name | Department | Role |
|----|------|------------|------|
| EMP001 | Sarah Johnson | Engineering | Software Engineer |
| EMP002 | Ahmed Khan | Engineering | Senior Developer |
| EMP003 | Maria Garcia | Engineering | QA Engineer |
| EMP004 | James Wilson | Engineering | DevOps Engineer |

### Pending Leave Requests
| Request ID | Employee | Type | Start Date | End Date | Status | Reason |
|------------|----------|------|------------|----------|--------|--------|
| LR001 | Sarah Johnson (EMP001) | Annual Leave | 2025-03-10 | 2025-03-14 | Pending | Family vacation |
| LR002 | Ahmed Khan (EMP002) | Sick Leave | 2025-03-05 | 2025-03-06 | Pending | Medical appointment |
| LR003 | Maria Garcia (EMP003) | Annual Leave | 2025-03-17 | 2025-03-21 | Pending | Personal travel |

### Company Holidays (2025)
| Date | Holiday |
|------|---------|
| 2025-01-01 | New Year's Day |
| 2025-04-18 | Good Friday |
| 2025-12-25 | Christmas Day |
| 2025-12-02 | UAE National Day |
| 2025-03-31 | Eid Al Fitr (expected) |

---

## Asgardeo Configuration Required

1. **Register an AI Agent** in Asgardeo Console
   - Navigate to Agents section
   - Create a new agent → get Agent ID and Agent Secret

2. **Create an MCP Client Application**
   - Application type: MCP Client Application
   - Redirect URI: `http://localhost:6274/oauth/callback`
   - Configure API scopes: `hr_basic`, `hr_read`, `hr_approve`

3. **Configure Scope Permissions**
   - Agent's default scopes: `openid`, `hr_basic`
   - User-authorized scopes (via OBO): `openid`, `hr_basic`, `hr_read`, `hr_approve`

---

## Expected User Interaction Flow

```
$ python main.py
🟢 Smart Employee Assistant started (Agent authenticated)
Available commands: Type your question or 'quit' to exit.

You: What are the company holidays this year?
Agent: Here are the company holidays for 2025:
  - Jan 1: New Year's Day
  - Mar 31: Eid Al Fitr (expected)
  - Apr 18: Good Friday
  - Dec 2: UAE National Day
  - Dec 25: Christmas Day

You: Is Sarah Johnson in the office today?
Agent: Sarah Johnson is currently in-office.

You: Approve Sarah's annual leave request for next week.
Agent: I need elevated privileges to approve leave requests.
       I'll need your authorization as a manager.

       Please authenticate in your browser:
       → Opening: https://api.asgardeo.io/t/<tenant>/oauth2/authorize?...

       Waiting for authorization...

[Manager authenticates in browser via Asgardeo]

Agent: Authorization received. Processing your request...
       ✅ Sarah Johnson's annual leave request (Mar 10-14) has been approved.

       Audit: Approved by Smart Employee Agent on behalf of Manager at 10:00 AM.

You: What are the other pending leave requests?
Agent: Here are the pending leave requests for your team:
  1. Ahmed Khan - Sick Leave (Mar 5-6) - Medical appointment
  2. Maria Garcia - Annual Leave (Mar 17-21) - Personal travel

You: quit
```

---

## Project Structure

```
smart-employee-agent/
├── README.md
├── hr-mcp-server/                    # HR & Leave Management MCP Server
│   ├── main.py                       # MCP server with HR tools
│   ├── hr_data.py                    # Mock HR data
│   ├── jwt_validator.py              # JWT validation (from mcp-auth pattern)
│   ├── requirements.txt
│   └── .env.example
└── agent/                            # Smart Employee AI Agent
    ├── main.py                       # Agent entry point with chat loop
    ├── oauth_callback.py             # OAuth callback server (from OBO pattern)
    ├── requirements.txt
    └── .env.example
```
