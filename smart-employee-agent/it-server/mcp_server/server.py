"""IT MCP Server — Sprint 0 stub.

Mirrors hr-server/mcp_server/server.py shape. Sprint 1 fills in:
- FastMCP app with two tools (it.get_employee_assets, it.get_asset_by_id)
- JWT verifier wired through common/auth/jwt_validator
- Per-request context vars populated from validated claims
- Nested act chain check (allowlist: it-agent + orchestrator-agent)
"""
import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def build_app():
    """Create and configure the FastMCP app for IT.

    Sprint 0: returns an MCP app with no tools registered yet (build succeeds,
    /mcp responds, but no operations are exposed).
    """
    mcp = FastMCP(name="it-server", stateless_http=True, json_response=True)

    # Sprint 1 will register:
    #   @mcp.tool(name="it.get_employee_assets") def get_employee_assets(...)
    #   @mcp.tool(name="it.get_asset_by_id")    def get_asset_by_id(...)

    return mcp.streamable_http_app()
