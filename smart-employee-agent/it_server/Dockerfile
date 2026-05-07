FROM python:3.12-slim

WORKDIR /app

COPY common ./common
COPY it_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY it_server/ ./it_server
# WORKDIR /app/it_server removed — use module form

USER 10014

EXPOSE 8004

CMD ["python", "-m", "it_server.main"]
