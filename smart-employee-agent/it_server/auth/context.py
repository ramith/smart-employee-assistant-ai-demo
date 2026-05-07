"""Per-request context vars for IT server. Mirrors hr_server/auth/context.py."""
from contextvars import ContextVar
from typing import Optional

current_user_sub: ContextVar[Optional[str]] = ContextVar("current_user_sub", default=None)
current_scopes: ContextVar[list[str]] = ContextVar("current_scopes", default=[])
current_token_info: ContextVar[Optional[dict]] = ContextVar("current_token_info", default=None)
current_user_first_name: ContextVar[Optional[str]] = ContextVar("current_user_first_name", default=None)
current_user_last_name: ContextVar[Optional[str]] = ContextVar("current_user_last_name", default=None)
