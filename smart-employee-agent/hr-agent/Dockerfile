FROM python:3.12-slim

WORKDIR /app

COPY common ./common
COPY hr-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hr-agent/ ./hr-agent
WORKDIR /app/hr-agent

USER 10014

EXPOSE 8001

CMD ["python", "main.py"]
