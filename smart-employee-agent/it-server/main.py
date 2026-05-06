"""IT Server — Single-Process Composition.

Mirrors hr-server/main.py exactly: Starlette app with MCP catch-all + REST
/health. Sprint 0 scaffold; Sprint 1 adds tool registration + JWT verifier.
"""
import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount

import uvicorn

import config
from mcp_server.server import build_app as build_mcp_app
from rest_api.server import routes as rest_routes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_app() -> Starlette:
    """Compose REST routes + MCP catch-all into one Starlette app with CORS."""
    mcp_app = build_mcp_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(app):
            yield

    routes = [*rest_routes(), Mount("/", app=mcp_app)]

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=config.ALLOWED_ORIGINS,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


app = build_app()


if __name__ == "__main__":
    logger.info(
        "Starting IT server on %s:%d (introspect_enabled=%s, expected_aud=%s)",
        config.HOST, config.PORT, config.INTROSPECT_ENABLED, config.EXPECTED_AUD,
    )
    uvicorn.run(app, host=config.HOST, port=config.PORT)
