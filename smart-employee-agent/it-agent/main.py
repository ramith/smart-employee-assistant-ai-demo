"""IT Asset Agent specialist stub — Sprint 0.

Mirrors hr-agent/. Wraps it-server via MCP for tool dispatch (Hop 5).

Sprint 1 implements:
- POST /a2a (JSON-RPC 2.0)
- GET /.well-known/agent-card.json
- JWT validation: aud=https://it.smart-employee.local/a2a, scope=it_assets_read_mcp
- Re-mint via RFC 8693 to call it-server (Hop 5)
"""
import logging
import os

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="it-agent", version="0.1.0-sprint0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/.well-known/agent-card.json")
async def agent_card() -> dict:
    return {
        "schema_version": "v3-custom",
        "name": "IT Asset Agent",
        "description": "Handles IT asset queries: laptops, devices, assignments. (Sprint 0 stub)",
        "url": os.getenv("IT_AGENT_CANONICAL_URL", "https://it.smart-employee.local/a2a"),
        "api_version": "0.1.0",
        "skills": [],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "auth": {
            "scheme": "oauth2",
            "issuer": os.getenv("ASGARDEO_ISSUER", ""),
            "audience": os.getenv("IT_AGENT_CANONICAL_URL", "https://it.smart-employee.local/a2a"),
        },
    }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("IT_AGENT_HOST", "0.0.0.0")
    port = int(os.getenv("IT_AGENT_PORT", "8002"))
    logger.info("Starting it-agent on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
