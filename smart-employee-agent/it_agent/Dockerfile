FROM python:3.12-slim

WORKDIR /app

COPY common ./common
COPY it_agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY it_agent/ ./it_agent
# WORKDIR /app/it_agent removed — use module form

USER 10014

EXPOSE 8002

CMD ["python", "-m", "it_agent.main"]
