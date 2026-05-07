FROM python:3.12-slim

WORKDIR /app

COPY common ./common
COPY hr_agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hr_agent/ ./hr_agent
# WORKDIR /app/hr_agent removed — use module form

USER 10014

EXPOSE 8001

CMD ["python", "-m", "hr_agent.main"]
