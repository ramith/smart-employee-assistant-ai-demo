FROM python:3.12-slim

WORKDIR /app

COPY common ./common
COPY it-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY it-server/ ./it-server
WORKDIR /app/it-server

USER 10014

EXPOSE 8004

CMD ["python", "main.py"]
