"""
 Copyright (c) 2025, WSO2 LLC. (http://www.wso2.com). All Rights Reserved.

  This software is the property of WSO2 LLC. and its suppliers, if any.
  Dissemination of any information or reproduction of any material contained
  herein is strictly forbidden, unless permitted by WSO2 in accordance with
  the WSO2 Commercial License available at http://wso2.com/licenses.
  For specific language governing the permissions and limitations under
  this license, please see the license as well as any agreement you've
  entered into with WSO2 governing the purchase of this software and any
"""

import os
import sys
import asyncio
import json

from dotenv import load_dotenv
from pathlib import Path

from asgardeo import AsgardeoConfig
from asgardeo_ai import AgentConfig, AgentAuthManager

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage

from oauth_callback import OAuthCallbackServer


# ==================== TERMINAL COLORS ====================
class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    
    # Foreground colors
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    MAGENTA = '\033[35m'
    CYAN = '\033[36m'
    WHITE = '\033[37m'
    
    # Background colors
    BG_BLUE = '\033[44m'
    BG_MAGENTA = '\033[45m'


def print_header():
    """Print the chat header."""
    print(f"\n{Colors.BG_BLUE}{Colors.WHITE}{Colors.BOLD}")
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║         🤖 MCP Agent Chat - On-Behalf-Of Flow                ║")
    print("║              (Azure OpenAI + User Context)                   ║")
    print("║         Type 'quit' or 'exit' to end the conversation        ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"{Colors.RESET}\n")


def print_tools_available(tools):
    """Print available tools from MCP server."""
    print(f"{Colors.CYAN}{Colors.BOLD}📦 Available MCP Tools:{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * 50}{Colors.RESET}")
    for tool in tools:
        print(f"  {Colors.GREEN}•{Colors.RESET} {Colors.BOLD}{tool.name}{Colors.RESET}: {Colors.DIM}{tool.description or 'No description'}{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * 50}{Colors.RESET}\n")


def print_thinking():
    """Print thinking indicator."""
    print(f"\n{Colors.YELLOW}🤔 Thinking...{Colors.RESET}")


def print_tool_call(tool_name, tool_input):
    """Print tool call information."""
    print(f"\n{Colors.MAGENTA}{Colors.BOLD}🔧 Calling Tool: {tool_name}{Colors.RESET}")
    print(f"{Colors.DIM}┌{'─' * 48}┐{Colors.RESET}")
    print(f"{Colors.CYAN}│ Input:{Colors.RESET}")
    formatted_input = json.dumps(tool_input, indent=2)
    for line in formatted_input.split('\n'):
        print(f"{Colors.DIM}│{Colors.RESET}   {line}")
    print(f"{Colors.DIM}└{'─' * 48}┘{Colors.RESET}")


def print_tool_result(tool_name, result):
    """Print tool result."""
    print(f"\n{Colors.GREEN}{Colors.BOLD}✅ Tool Result: {tool_name}{Colors.RESET}")
    print(f"{Colors.DIM}┌{'─' * 48}┐{Colors.RESET}")
    print(f"{Colors.CYAN}│ Output:{Colors.RESET}")
    # Try to parse and format JSON if possible
    try:
        if isinstance(result, str):
            parsed = json.loads(result)
            formatted = json.dumps(parsed, indent=2)
        else:
            formatted = json.dumps(result, indent=2)
    except (json.JSONDecodeError, TypeError):
        formatted = str(result)
    
    for line in formatted.split('\n'):
        print(f"{Colors.DIM}│{Colors.RESET}   {Colors.GREEN}{line}{Colors.RESET}")
    print(f"{Colors.DIM}└{'─' * 48}┘{Colors.RESET}")


def print_agent_response(response):
    """Print final agent response."""
    print(f"\n{Colors.BLUE}{Colors.BOLD}🤖 Agent:{Colors.RESET}")
    
    # Handle Google Gemini's content block format: [{'type': 'text', 'text': '...'}]
    if isinstance(response, list):
        text_parts = []
        for item in response:
            if isinstance(item, dict) and 'text' in item:
                text_parts.append(item['text'])
            else:
                text_parts.append(str(item))
        response = '\n'.join(text_parts)
    
    print(f"{Colors.WHITE}{response}{Colors.RESET}\n")


def print_user_prompt():
    """Print user input prompt."""
    return input(f"{Colors.GREEN}{Colors.BOLD}You:{Colors.RESET} ")


# ==================== MAIN APPLICATION ====================

# Load environment variables from .env file
ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

ASGARDEO_CONFIG = AsgardeoConfig(
    base_url=os.getenv("ASGARDEO_BASE_URL"),
    client_id=os.getenv("CLIENT_ID"),
    redirect_uri=os.getenv("REDIRECT_URI")
)

AGENT_CONFIG = AgentConfig(
    agent_id=os.getenv("AGENT_ID"),
    agent_secret=os.getenv("AGENT_SECRET")
)


def convert_mcp_tools_to_langchain(mcp_tools):
    """Convert MCP tools with dict schemas to LangChain tools with Pydantic schemas."""
    from pydantic import BaseModel, Field, create_model
    from langchain_core.tools import StructuredTool
    
    converted_tools = []
    
    for tool in mcp_tools:
        # Skip if already has proper schema
        if hasattr(tool.args_schema, '__mro__'):
            converted_tools.append(tool)
            continue
            
        # Convert dict schema to Pydantic model
        if isinstance(tool.args_schema, dict):
            properties = tool.args_schema.get('properties', {})
            required = tool.args_schema.get('required', [])
            
            # Build field definitions
            fields = {}
            for prop_name, prop_spec in properties.items():
                field_type = float if prop_spec.get('type') == 'number' else str
                is_required = prop_name in required
                default = ... if is_required else None
                fields[prop_name] = (field_type, Field(default=default))
            
            # Create Pydantic model
            ModelClass = create_model(f'{tool.name.capitalize()}Schema', **fields)
            
            # Create new tool with Pydantic schema
            # IMPORTANT: Capture 'tool' by value using default argument to avoid closure bug
            # Without this, all closures would reference the last tool in the loop
            async def tool_func(t=tool, **kwargs):
                return await t.ainvoke(kwargs)
            
            converted_tool = StructuredTool(
                name=tool.name,
                description=tool.description or "",
                func=lambda t=tool, **kw: t.invoke(kw),
                coroutine=tool_func,
                args_schema=ModelClass
            )
            converted_tools.append(converted_tool)
        else:
            converted_tools.append(tool)
    
    return converted_tools


async def run_agent_with_streaming(llm_with_tools, messages, tools):
    """
    Run the LLM with bound tools and show tool calls/results.
    """
    print_thinking()
    
    # Create a tool map for easy lookup
    tool_map = {tool.name: tool for tool in tools}
    
    # Invoke LLM with tools
    result = await llm_with_tools.ainvoke(messages)
    
    # Process tool calls if any
    if result.tool_calls:
        # IMPORTANT: Add AIMessage with tool_calls FIRST (correct order for LLM history)
        messages.append(result)
        
        tool_messages = []
        has_error = False
        
        for tool_call in result.tool_calls:
            tool_name = tool_call["name"]
            tool_input = tool_call["args"]
            
            print_tool_call(tool_name, tool_input)
            
            # Execute the tool
            if tool_name in tool_map:
                tool_result = await tool_map[tool_name].ainvoke(tool_input)
                tool_result_str = str(tool_result)
                
                # Check if tool returned an error
                if "Access denied" in tool_result_str or "Error:" in tool_result_str:
                    has_error = True
                    print(f"\n{Colors.RED}❌ Error: {tool_result_str}{Colors.RESET}")
                    tool_messages.append(ToolMessage(
                        content=f"Error: {tool_result_str}. Please inform the user about this permission error.",
                        tool_call_id=tool_call["id"],
                        name=tool_name
                    ))
                else:
                    print_tool_result(tool_name, tool_result_str)
                    tool_messages.append(ToolMessage(
                        content=tool_result_str,
                        tool_call_id=tool_call["id"],
                        name=tool_name
                    ))
        
        # Add all tool messages to history (after AIMessage)
        messages.extend(tool_messages)
        
        # Get final response after tool execution
        final_result = await llm_with_tools.ainvoke(messages)
        return final_result.content
    
    return result.content


async def main():
    print_header()
    
    print(f"{Colors.YELLOW}🔐 Authenticating with Asgardeo (On-Behalf-Of Flow)...{Colors.RESET}")
    
    async with AgentAuthManager(ASGARDEO_CONFIG, AGENT_CONFIG) as auth_manager:
        # Get agent token
        agent_token = await auth_manager.get_agent_token(["openid", "email", "subtract", "multiply", "add", "divide"])

        # Generate user authorization URL
        auth_url, state, code_verifier = auth_manager.get_authorization_url_with_pkce(["openid", "email", "subtract", "multiply", "add", "divide"])

        print(f"\n{Colors.CYAN}Open this URL in your browser to authenticate:{Colors.RESET}")
        print(f"{Colors.BLUE}{auth_url}{Colors.RESET}\n")

        callback = OAuthCallbackServer(port=6274)
        callback.start()

        print(f"{Colors.YELLOW}Waiting for authorization code from redirect...{Colors.RESET}")

        # Wait for redirect
        auth_code, returned_state, error = await callback.wait_for_code()
        callback.stop()

        if auth_code is None:
            print(f"{Colors.RED}❌ Authorization failed or cancelled. Error: {error}{Colors.RESET}")
            return

        print(f"{Colors.GREEN}✅ Received authorization code{Colors.RESET}")

        # Exchange auth code for user token (OBO flow)
        obo_token = await auth_manager.get_obo_token(auth_code, agent_token=agent_token, code_verifier=code_verifier)
    
    print(f"{Colors.GREEN}✅ Authentication successful!{Colors.RESET}\n")
    
    print(f"{Colors.YELLOW}🔌 Connecting to MCP Server...{Colors.RESET}")
    
    # Connect to MCP Server with Auth Header (using OBO token)
    client = MultiServerMCPClient(
        {
            "mcp_server": {
                "transport": "streamable_http",
                "url": os.getenv("MCP_SERVER_URL"),
                "headers": {
                    "Authorization": f"Bearer {obo_token.access_token}"
                }
            }
        }
    )
    
    # Get tools and display them
    tools = await client.get_tools()
    
    if not tools:
        print(f"{Colors.RED}❌ No tools available from MCP server. Please check your connection.{Colors.RESET}")
        return
    
    print(f"{Colors.GREEN}✅ Connected to MCP Server!{Colors.RESET}\n")
    print_tools_available(tools)
    
    # Convert MCP tools to LangChain format with Pydantic schemas
    tools = convert_mcp_tools_to_langchain(tools)
    
    # LLM (Azure OpenAI) + LangChain Agent
    # Initialize Google Gemini Agent
    if not os.getenv("GOOGLE_API_KEY"):
        print(f"{Colors.RED}❌ Error: GOOGLE_API_KEY not found in environment.{Colors.RESET}")
        sys.exit(1)

    llm = ChatGoogleGenerativeAI(
        model=os.getenv("GOOGLE_MODEL_NAME", "gemini-3-flash-preview"),
        temperature=float(os.getenv("GOOGLE_TEMPERATURE", "0.9"))
    )
    
    # Bind tools to LLM (Google Gemini supports tool calling)
    llm_with_tools = llm.bind_tools(tools)
    
    # Build system prompt with available tools
    tool_names = [tool.name for tool in tools]
    system_prompt = f"""You are a helpful assistant that MUST use the available MCP tools to answer questions.

AVAILABLE TOOLS: {', '.join(tool_names)}

IMPORTANT RULES:
1. For ANY calculation, arithmetic, or math operation, you MUST use the appropriate tool. Do NOT calculate mentally.
2. Always prefer using tools over answering from your own knowledge when a relevant tool is available.
3. If a user asks a question that can be answered by a tool, USE THE TOOL.
4. After using a tool, report the result to the user.

Remember: Your primary purpose is to demonstrate MCP tool usage. Always use tools when applicable."""
    
    # Conversation history with system prompt
    conversation_history = [SystemMessage(content=system_prompt)]
    
    print(f"{Colors.DIM}{'═' * 60}{Colors.RESET}")
    print(f"{Colors.CYAN}Start chatting! The agent will use MCP tools to help you.{Colors.RESET}")
    print(f"{Colors.DIM}{'═' * 60}{Colors.RESET}\n")
    
    # Interactive chat loop
    while True:
        try:
            user_input = print_user_prompt()
            
            # Check for exit commands
            if user_input.lower().strip() in ['quit', 'exit', 'bye', 'q']:
                print(f"\n{Colors.YELLOW}👋 Goodbye! Thanks for chatting.{Colors.RESET}\n")
                break
            
            # Skip empty input
            if not user_input.strip():
                continue
            
            # Add user message to history
            conversation_history.append(HumanMessage(content=user_input))
            
            # Run LLM with streaming output
            final_response = await run_agent_with_streaming(
                llm_with_tools,
                conversation_history,
                tools
            )
            
            # Update conversation history with the response
            # Only add the final AI response to keep history clean
            if final_response:
                conversation_history.append(AIMessage(content=final_response))
                print_agent_response(final_response)
            else:
                print(f"\n{Colors.RED}❌ No response from agent.{Colors.RESET}\n")
            
        except KeyboardInterrupt:
            print(f"\n\n{Colors.YELLOW}👋 Interrupted. Goodbye!{Colors.RESET}\n")
            break
        except Exception as e:
            print(f"\n{Colors.RED}❌ Error: {str(e)}{Colors.RESET}\n")
            # Continue the loop instead of breaking
            continue


# Run app
if __name__ == "__main__":
    asyncio.run(main())