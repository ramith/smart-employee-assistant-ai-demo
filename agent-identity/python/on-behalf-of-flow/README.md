# Agent Identity Quickstart - On-Behalf-Of (OBO) Flow

This guide explains how to run the `On-Behalf-Of (OBO) authentication flow` using Asgardeo with **LangChain**.

In this scenario, the AI agent authenticates on behalf of a user, using:

- Authorization Code Flow
- PKCE (Proof Key for Code Exchange) to ensure only your agent can securely exchange the authorization code for the OBO token
- Token exchange to obtain an OBO token that represents the user

Your agent then uses that OBO token to securely call MCP tools.

This example corresponds to the _“AI agent acting on behalf of a user”_ scenario described in the [Agent Authentication Guide](https://wso2.com/asgardeo/docs/guides/agentic-ai/ai-agents/agent-authentication/).

## Prerequisites

- Python 3.10 or higher
- An Asgardeo account and application setup
- pip (Python package installer)
- An MCP server secured with Asgardeo (you may use your own or follow the [MCP Auth Server quickstart](https://wso2.com/asgardeo/docs/quick-starts/mcp-auth-server/#add-auth-to-the-mcp-server) to set one up quickly).
- **Google Gemini API Key**

## Directory Overview

This sample is located under:

```
agent-identity/python/on-behalf-of-flow/
├── README.md               # You are here
├── main.py                 # Main application entry point
├── oauth_callback.py       # Lightweight callback server for Authorization code + PKCE flow
└── requirements.txt        # Python dependencies
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
    - Authorized Redirect URL 
      
      _The **authorized redirect URL** is the location Asgardeo sends users to after a successful login, typically the callback endpoint of the client application that connects to the MCP server. In this guide, we use the redirect URL: http://localhost:6274/oauth/callback, which corresponds to the local callback server that listens for Asgardeo’s redirection and captures the authorization code._
4. Finish the wizard

Make note of:
- **Client ID** (in the _Protocol_ tab)
- **Tenant name** (visible in the Asgardeo URL)

## Set Up the Project Locally

Navigate into the folder:

```bash
cd agent-identity/python/on-behalf-of-flow
```

### Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

## Configure Environment Variables

Update the `.env` file located at `agent-identity/python` by replacing the following placeholders with your actual values:

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

## Understanding the OBO Flow

Here’s what happens when you run this sample:

1. The agent authenticates with its own Agent Credentials
2. The agent prepares an authorization URL for the user
3. A local callback server (`oauth_callback.py`) listens on port 6274
4. You open the authorization URL
5. You log in as a user
6. Upon successful login, Asgardeo redirects back to `http://localhost:6274/oauth/callback`
7. The callback server captures `code` and `state`
8. The agent exchanges the code for an **OBO token**
9. The agent calls your MCP server using `Authorization: Bearer <obo_token>`

## Running the OBO Flow

Ensure the secured MCP Server is up and running.

Run the application:

```bash
python main.py
```

You will see instructions to open a URL to authenticate. After login, return to the terminal to chat with the agent.

### Example Interaction

```text
Enter your question: what is 76 + 8?

🤔 Thinking...

🔧 Calling Tool: add
...

✅ Tool Result: add
...

🤖 Agent:
The sum of 76 and 8 is 84.
```
