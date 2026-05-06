FROM python:3.12-slim

WORKDIR /app

# common/ is at the repo root; copied via build-context "." in docker-compose
COPY common ./common
COPY orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY orchestrator/ ./orchestrator
WORKDIR /app/orchestrator

USER 10014

EXPOSE 8080

CMD ["python", "main.py"]
