FROM python:3.12-slim

WORKDIR /app

# Build context is the repo root (see docker-compose.yml) so that common/
# is available alongside hr_server/. Sprint 0 task 13: hr_server imports
# common.auth (introspection feature-flagged OFF).
COPY common ./common
COPY hr_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hr_server/ ./hr_server
# WORKDIR /app/hr_server removed — use module form

USER 10014

EXPOSE 8000

CMD ["python", "-m", "hr_server.main"]
