
import json

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
    print("║              🤖 MCP Agent Chat                               ║")
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
