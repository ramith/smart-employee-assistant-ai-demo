FROM python:3.12-slim

WORKDIR /app

# Build context is the repo root (see docker-compose.yml) so that common/
# is available alongside hr-server/. Sprint 0 task 13: hr-server imports
# common.auth (introspection feature-flagged OFF).
COPY common ./common
COPY hr-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hr-server/ ./hr-server
WORKDIR /app/hr-server

USER 10014

EXPOSE 8000

CMD ["python", "main.py"]
