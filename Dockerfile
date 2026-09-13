# ── Stage 1: Builder ──
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    postgresql-server-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry for dependency resolution (falls back to pip)
COPY pyproject.toml ./

# Build wheel cache
RUN pip wheel --no-cache-dir --wheel-dir /wheels -e ".[dev]" 2>/dev/null || \
    pip wheel --no-cache-dir --wheel-dir /wheels -e .

# ── Stage 2: Runtime ──
FROM python:3.11-slim AS runtime

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels and install only runtime deps
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* 2>/dev/null; \
    pip install --no-cache-dir fastapi uvicorn sse-starlette

# Copy source
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY pyproject.toml ./

# Install in editable mode (for CLI entry point)
RUN pip install --no-cache-dir -e .

# Create non-root user
RUN useradd -m -u 1000 opc && \
    mkdir -p /app/data && \
    chown -R opc:opc /app

USER opc

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000 8100

# Default command — can be overridden to run MCP server on port 8100
CMD ["uvicorn", "devforge.adapters.driving.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
