"""Orchestrator service stub — Sprint 0.

The user-facing chat agent. Sprint 1 implements:
- PKCE login with `requested_actor=<orchestrator-agent>`
- discover_agents tool exposed to LangChain/Gemini
- RFC 8693 token-exchange per A2A call (Hops 3a/3b)
- Header-callable injection of bearer tokens into A2A client

Sprint 2 adds:
- `/auth/backchannel-logout` endpoint
- Cache-bust dispatcher to specialists
- terminate_by_sid + asyncio.Event for streaming cancellation

For now, this is a /health-only stub so docker compose can build the topology.
"""
import logging
import os

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="orchestrator", version="0.1.0-sprint0")


@app.get("/health")
async def health() -> dict:
    """Health probe; no auth (per security NIT — no version/build leak)."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("ORCHESTRATOR_HOST", "0.0.0.0")
    port = int(os.getenv("ORCHESTRATOR_PORT", "8080"))
    logger.info("Starting orchestrator on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
