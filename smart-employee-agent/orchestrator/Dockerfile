FROM python:3.12-slim

WORKDIR /app

# common/ is at the repo root; copied via build-context "." in docker-compose
COPY common ./common
COPY orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY orchestrator/ ./orchestrator
# Bundle the SPA so the orchestrator can serve it same-origin (avoids cross-origin
# cookie issues in dev). Mounted at "/" by orchestrator.main.
COPY client/index.html client/app.js client/styles.css ./client_static/
# Agent-card fixtures (registry source). Pointed at via AGENT_CARDS_DIR.
COPY tests/fixtures/agent_cards ./agent_cards
# WORKDIR /app/orchestrator removed — use module form

USER 10014

EXPOSE 8080

CMD ["python", "-m", "orchestrator.main"]
