FROM python:3.12-slim

WORKDIR /app

COPY common ./common
COPY it-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY it-agent/ ./it-agent
WORKDIR /app/it-agent

USER 10014

EXPOSE 8002

CMD ["python", "main.py"]
