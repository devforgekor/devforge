# ── Stage 1: Builder ──
FROM python:3.14-slim AS builder

# uv (pinned) — installs the exact dependency set from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Build dependencies for native wheels (asyncpg, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Resolve runtime deps from the lock first (cached layer; project not yet installed)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project

# Copy package sources + metadata (README is referenced by pyproject.toml)
COPY src/ ./src/
COPY alembic/ ./alembic/
RUN uv sync --frozen --no-dev --no-editable

# ── Stage 2: Runtime ──
FROM python:3.14-slim AS runtime

WORKDIR /app

# Runtime libraries only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the locked virtualenv (built in stage 1) and put it on PATH
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Alembic migration assets (kept alongside the installed package)
COPY alembic/ ./alembic/
COPY alembic.ini ./alembic.ini

# Non-root user
RUN useradd -m -u 1000 opc && \
    mkdir -p /app/data && \
    chown -R opc:opc /app

USER opc

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000 8100

# Default: FastAPI HTTP API. Override to run MCP server (port 8100):
#   devforge mcp serve --port 8100
CMD ["uvicorn", "devforge.adapters.driving.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
