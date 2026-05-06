"""IT Server scope checks. Mirrors hr-server/auth/scopes.py shape."""
from typing import Iterable

# IT-specific scopes per docs/scope-policy.md
IT_ASSETS_READ_MCP = "it_assets_read_mcp"
IT_ASSETS_WRITE_MCP = "it_assets_write_mcp"  # reserved


def require_scope(actual_scopes: Iterable[str], required: str) -> bool:
    """Return True iff required is present in actual_scopes."""
    return required in set(actual_scopes)
