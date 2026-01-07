# Agent Identity Quickstart — Agent Authentication Flow

This guide walks you through running the **Agent Authentication Flow** sample for authenticating AI agents using **Asgardeo** and securely connecting to an MCP server using **LangChain**.

In this flow, the AI agent authenticates by itself (not on behalf of a user) using its Agent Credentials and obtains a valid access token to call MCP tools securely.

This example corresponds to the _“AI agent acting on its own”_ scenario described in the [Agent Authentication Guide](https://wso2.com/asgardeo/docs/guides/agentic-ai/ai-agents/agent-authentication/).

## Prerequisites

- Python 3.10 or higher
- An Asgardeo account and application setup
- pip (Python package installer)
- An MCP server secured with Asgardeo (you may use your own or follow the [MCP Auth Server quickstart](https://wso2.com/asgardeo/docs/quick-starts/mcp-auth-server/#add-auth-to-the-mcp-server) to set one up quickly).
- **Google Gemini API Key**

## Directory Overview

```
agent-identity/python/agent-auth-flow/
├── main.py             # Main application entry point
├── ui_utils.py         # UI and terminal formatting utilities
└── requirements.txt    # Python dependencies
```

## Register an AI Agent in Asgardeo

1. Sign in to `Asgardeo Console → Agents`
2. Click `+ New Agent`
3. Provide:
   - Name (required)
   - Description (optional)
4. Click `Create`

You will receive:
- Agent ID
- Agent Secret (shown once) → store securely

You will need these values for the `.env` file.

## Configure an MCP Client Application

For the agent to authenticate and talk to a secured MCP server:

1. Go to `Applications → New Application`
2. Select `MCP Client Application`
3. Provide:
    - Application name
    - Authorized Redirect URL (eg: http://localhost:6274/oauth/callback)
      
        _The **authorized redirect URL** is the location Asgardeo uses to send users after a successful login. In the direct agent authentication flow, this value isn’t used because no user sign-in occurs. However, the same application can also be used for the **On-Behalf-Of (OBO)** flow, where user redirection is required. Therefore, for consistency and future use, we can set the redirect URL to:
http://localhost:6274/oauth/callback._
4. Finish the wizard

Make note of:
- **Client ID** (in the _Protocol_ tab)
- **Tenant name** (visible in the Asgardeo URL)

## Set Up the Project Locally

Navigate into the folder:

```bash
cd agent-identity/python/agent-auth-flow
```

### Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

## Configure Environment Variables

Update the `.env` file located at `agent-identity/python` with your values.

**1. Asgardeo Configuration:**

```bash
ASGARDEO_BASE_URL=https://api.asgardeo.io/t/<your-tenant>
CLIENT_ID=<your-client-id>
REDIRECT_URI=http://localhost:6274/oauth/callback
AGENT_ID=<your-agent-id>
AGENT_SECRET=<your-agent-secret>
MCP_SERVER_URL=<your-mcp-server-url>
```

**2. Google Gemini Configuration:**

```bash
GOOGLE_API_KEY=your-gemini-api-key
GOOGLE_MODEL_NAME=gemini-2.0-flash  # Optional
GOOGLE_TEMPERATURE=0.9              # Optional
```

If you don’t already have an MCP server running, you can quickly set one up by following the [MCP Authentication Quickstart](https://github.com/wso2/iam-ai-samples/tree/main/mcp-auth/python) guide.

## Running the Agent Authentication Flow

Ensure the secured MCP Server is up and running.

Run the application:

```bash
python main.py
```

### What the agent will do:

1. Authenticate with Asgardeo using `Agent Credentials`
2. Obtain a valid `agent access token`
3. Connect to your MCP server using: `Authorization: Bearer <token>`
4. Start an **interactive chat session** where you can ask questions.
5. The agent will **think**, **call MCP tools** (e.g., calculations), and return the final answer.

### Example Interaction

```text
Enter your question: What is 100 + 500?

🤔 Thinking...

🔧 Calling Tool: add
┌────────────────────────────────────────────────┐
│ Input:                                         │
│    {                                           │
│      "a": 100,                                 │
│      "b": 500                                  │
│    }                                           │
└────────────────────────────────────────────────┘

✅ Tool Result: add
┌────────────────────────────────────────────────┐
│ Output:                                        │
│    {                                           │
│      "result": 600                             │
│    }                                           │
└────────────────────────────────────────────────┘

🤖 Agent:
The sum of 100 and 500 is 600.
```

## Understanding the Agent Authentication Flow

This flow is used when:

    ✔ The agent is acting independently
    ✔ No end-user is involved
    ✔ Tokens represent the agent itself

If you want to authenticate on behalf of a user using PKCE and authorization code flow, refer to:
➡ `agent-identity/python/on-behalf-of-flow/README.md`
