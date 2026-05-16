FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY cli.py .
COPY healthcheck.py .

EXPOSE 8000

ENV DEVFORGE_DATABASE_URL=postgresql://devforge:devforge_app_2026@data-pod:5432/devforge_app

CMD ["uvicorn", "app.mcp_server:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
