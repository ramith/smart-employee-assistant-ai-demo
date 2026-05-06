"""HR Agent specialist stub — Sprint 0.

Sprint 1 implements:
- POST /a2a (JSON-RPC 2.0 single endpoint, method=`message/send`)
- GET /.well-known/agent-card.json
- JWT validation: exact aud, scope, nested act allowlist
- Re-mint via RFC 8693 to call hr-server (Hop 4)

For now, /health + a placeholder agent-card.
"""
import logging
import os

from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="hr-agent", version="0.1.0-sprint0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/.well-known/agent-card.json")
async def agent_card() -> dict:
    """Sprint 0 placeholder; Sprint 1 returns the real card."""
    return {
        "schema_version": "v3-custom",
        "name": "HR Agent",
        "description": "Handles HR queries. (Sprint 0 stub)",
        "url": os.getenv("HR_AGENT_CANONICAL_URL", "https://hr.smart-employee.local/a2a"),
        "api_version": "0.1.0",
        "skills": [],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "auth": {
            "scheme": "oauth2",
            "issuer": os.getenv("ASGARDEO_ISSUER", ""),
            "audience": os.getenv("HR_AGENT_CANONICAL_URL", "https://hr.smart-employee.local/a2a"),
        },
    }


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HR_AGENT_HOST", "0.0.0.0")
    port = int(os.getenv("HR_AGENT_PORT", "8001"))
    logger.info("Starting hr-agent on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
