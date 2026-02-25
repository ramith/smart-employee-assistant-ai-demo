# Smart Employee Agent - Requirements Document

## Overview

A sample application demonstrating **dynamic privilege escalation** for AI agents using Asgardeo/Identity Server. The AI agent starts with low-privilege access (its own identity) and dynamically escalates to act on behalf of a user when elevated actions (e.g., leave approval, IT appointment booking) are required.

This combines both the **Agent-Auth-Flow** and **On-Behalf-Of Flow** in a realistic corporate scenario with two backend services: HR leave management and IT support scheduling.

---

## Scenario

**The Corporate Concierge** helps employees interact with the company's HR and IT support systems through a split-panel web interface with a conversational AI chat and a live employee dashboard.

- **User**: Employee / Department Manager
- **AI Agent**: Corporate Concierge (low baseline privileges — can only read public data)
- **Backend Services**: HR MCP Server + IT Support MCP Server

### User Story

> As an employee, I want to ask the AI assistant to approve a leave request or book an IT support appointment, and have the assistant securely escalate its privileges by requesting my authorization before performing the action. I want to see the results live on my dashboard.

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

**Dashboard REST endpoint:**
- `GET /api/leaves` — Returns all leave requests (all statuses) for the dashboard. No authentication required.

**Access control rules:**
- Agent token (agent-auth-flow) is granted: `hr_basic` scope only
- OBO token (on-behalf-of-flow with user) is granted: `hr_basic`, `hr_read`, `hr_approve` scopes

**Data:**
- In-memory mock data (no database required)
- Pre-populated with sample employees, leave balances, and pending leave requests

**Token validation:**
- JWT validation using Asgardeo JWKS endpoint
- Scope-based access control on each MCP tool
- Returns structured error dict when token lacks required scope

### FR-2: IT Support Appointment MCP Server

A secured MCP server that acts as the IT support backend, exposing the following tools:

| Tool | Description | Required Scope |
|------|-------------|----------------|
| `get_support_categories` | List available IT support categories | *(none)* |
| `get_available_slots` | View available appointment time slots | *(none)* |
| `get_technicians` | List IT technicians (optionally by category) | *(none)* |
| `get_my_appointments` | View an employee's appointments | `it_read` |
| `get_appointment_details` | Get detailed info about a specific appointment | `it_read` |
| `book_appointment` | Book a new IT support appointment | `it_manage` |
| `cancel_appointment` | Cancel an existing appointment | `it_manage` |
| `reschedule_appointment` | Reschedule an appointment to a new time slot | `it_manage` |

**Dashboard REST endpoint:**
- `GET /api/bookings` — Returns all IT appointments for the dashboard. No authentication required.

**Access control rules:**
- Agent token: basic read tools (categories, slots, technicians) require no scope
- OBO token: `it_read` for viewing appointments, `it_manage` for booking/cancelling

**Data:**
- In-memory mock data (no database required)
- Pre-populated with support categories, technicians, time slots, and sample appointments

### FR-3: AI Agent (Corporate Concierge)

A LangChain-based agent powered by Gemini LLM that:

1. **Starts with agent-level authentication**
   - On startup, authenticates using Agent ID + Agent Secret (agent-auth-flow)
   - Connects to both MCP servers with its low-privilege agent token
   - Can answer basic questions (holiday calendar, employee status, IT categories, available slots)

2. **Detects when elevated privileges are needed**
   - When the user requests an action requiring higher privileges, the agent recognizes it via `insufficient_scope` error from the MCP server
   - Informs the user that authorization is required

3. **Triggers On-Behalf-Of flow for privilege escalation**
   - Initiates the PKCE authorization flow
   - Opens the browser for the user to authenticate via Asgardeo
   - Exchanges for an OBO token carrying both HR and IT elevated scopes

4. **Executes the elevated action**
   - Reconnects to both MCP servers using the OBO token
   - Calls the elevated tool and reports the result

5. **Maintains audit context**
   - The OBO token carries both the agent's identity and the user's identity
   - Both MCP servers log which agent acted on behalf of which user

### FR-4: Split-Panel Web UI with Live Dashboard

- **Left panel**: Conversational AI chat ("Concierge AI")
  - Message input and chat bubbles
  - Shows "Authorize" button when escalation is needed
  - Typing indicator while agent processes
  - Auth badge showing current privilege level

- **Right panel**: Employee Dashboard
  - **My Leave Requests** table (ID, Employee, Type, Date, Status)
  - **My IT Support Bookings** table (ID, Category, Date, Time, Technician, Status)
  - Auto-refreshes after each chat interaction
  - Status badges with color coding (Pending, Approved, Rejected, Confirmed, Cancelled)

- **Live updates**: When the agent performs a state-changing action (approve leave, book appointment), the dashboard reflects the change immediately.

### FR-5: Interactive Chat

- The agent runs in a web-based chat interface
- The user can ask multiple questions in a session
- Low-privilege queries are answered immediately (using agent token)
- High-privilege actions trigger the OBO flow only when needed
- After the OBO token is obtained, it is reused for subsequent elevated actions within the session

---

## Non-Functional Requirements

### NFR-1: Technology Stack
- **Language**: Python 3.10+
- **Agent Framework**: LangChain with `langchain-mcp-adapters` (`MultiServerMCPClient`)
- **LLM**: Google Gemini (via `langchain-google-genai`)
- **MCP Server**: FastMCP (from `mcp` package)
- **Auth SDK**: `asgardeo` and `asgardeo_ai` packages
- **Token Validation**: PyJWT with JWKS
- **Web Framework**: FastAPI (agent server)
- **HTTP Client**: httpx (dashboard proxy)
- **Frontend**: Vanilla HTML/CSS/JS (single file, no build tools)

### NFR-2: Security
- Agent token has minimal scopes (principle of least privilege)
- OBO token is time-limited
- All tokens validated via JWKS (asymmetric signatures)
- Scope-based access control enforced at each MCP server
- Dashboard REST endpoints are read-only and unauthenticated (acceptable for a sample)
- Audit trail for all elevated actions

### NFR-3: Architecture
- Each MCP server runs in a Starlette wrapper that provides:
  - REST endpoints at the parent level (unauthenticated, for dashboard)
  - MCP protocol endpoints in a mounted sub-app (with JWT auth middleware)
- The agent server proxies dashboard data from both MCP servers via httpx
- Single authorization flow grants access to all elevated scopes across both systems

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
| LR001 | Sarah Johnson | Annual Leave | 2026-03-10 | 2026-03-14 | Pending | Family vacation |
| LR002 | Ahmed Khan | Sick Leave | 2026-03-05 | 2026-03-06 | Pending | Medical appointment |
| LR003 | Maria Garcia | Annual Leave | 2026-03-17 | 2026-03-21 | Pending | Personal travel |

### Company Holidays (2026)
| Date | Holiday |
|------|---------|
| 2026-01-01 | New Year's Day |
| 2026-03-20 | Eid Al Fitr (expected) |
| 2026-05-27 | Arafat Day (expected) |
| 2026-05-28 | Eid Al Adha (expected) |
| 2026-07-18 | Islamic New Year (expected) |
| 2026-12-01 | Commemoration Day |
| 2026-12-02 | UAE National Day |

### IT Support Categories
| ID | Name |
|----|------|
| CAT001 | Hardware Issue |
| CAT002 | Software Installation |
| CAT003 | Network & Connectivity |
| CAT004 | Account & Access |
| CAT005 | Email & Communication |
| CAT006 | Security Incident |
| CAT007 | New Equipment Request |
| CAT008 | General IT Consultation |

### IT Technicians
| ID | Name | Specializations |
|----|------|----------------|
| TECH001 | Alex Rivera | Hardware Issue, New Equipment Request |
| TECH002 | Priya Patel | Software Installation, Network & Connectivity |
| TECH003 | Omar Hassan | Account & Access, Email & Communication, Security Incident |
| TECH004 | Lisa Chen | Network & Connectivity, Security Incident, General IT Consultation |

### IT Appointments
| ID | Employee | Category | Description | Status |
|----|----------|----------|-------------|--------|
| APT001 | Sarah Johnson | Hardware Issue | Laptop screen flickering intermittently | Confirmed |

---

## Asgardeo Configuration Required

1. **Register an AI Agent** in Asgardeo Console
   - Navigate to Agents section
   - Create a new agent → get Agent ID and Agent Secret

2. **Create an MCP Client Application**
   - Application type: MCP Client Application
   - Redirect URI: `http://localhost:5001/oauth/callback`
   - Configure API scopes: `hr_basic`, `hr_read`, `hr_approve`, `it_basic`, `it_read`, `it_manage`

3. **Configure Scope Permissions**
   - Agent's default scopes: `openid`, `hr_basic`
   - User-authorized scopes (via OBO): `openid`, `hr_basic`, `hr_read`, `hr_approve`, `it_basic`, `it_read`, `it_manage`

---

## Project Structure

```
smart-employee-agent/
├── README.md
├── REQUIREMENTS.md
├── DESIGN.md
├── hr-mcp-server/                    # HR & Leave Management MCP Server
│   ├── main.py                       # MCP server + REST dashboard endpoint
│   ├── hr_data.py                    # Mock HR data
│   ├── jwt_validator.py              # JWT validation via JWKS
│   ├── requirements.txt
│   └── .env.example
├── it-support-mcp-server/            # IT Support Appointment MCP Server
│   ├── main.py                       # MCP server + REST dashboard endpoint
│   ├── it_support_data.py            # Mock IT support data
│   ├── jwt_validator.py              # JWT validation via JWKS
│   ├── requirements.txt
│   └── .env.example
└── agent/                            # Smart Employee AI Agent (Web App)
    ├── main.py                       # FastAPI + LangChain agent + dashboard proxy
    ├── static/index.html             # Split-panel UI (Chat + Dashboard)
    ├── requirements.txt
    └── .env.example
```
