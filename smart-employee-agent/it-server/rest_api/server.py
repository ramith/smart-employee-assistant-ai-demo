"""IT REST stub — kept for shape symmetry with hr-server.

The v3 architecture routes IT calls through it-agent → it-server MCP only;
no direct REST. This module exists so it-server's directory shape mirrors
hr-server's exactly. Only a /health endpoint is exposed.
"""
import logging

from starlette.middleware.cors import CORSMiddleware  # re-exported
from starlette.responses import JSONResponse
from starlette.routing import Route

import config

logger = logging.getLogger(__name__)


async def health(_request):
    return JSONResponse({"status": "ok"})


def routes():
    """Return REST routes to compose into the Starlette app."""
    return [
        Route("/health", health, methods=["GET"]),
    ]


# Re-export for main.py to mirror hr-server's import shape
__all__ = ["routes", "CORSMiddleware"]
