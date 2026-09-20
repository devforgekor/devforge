#!/bin/bash
pip install --no-index --find-links=/pip-cache --root-user-action=ignore typer rich structlog pyyaml asyncpg alembic pgvector 2>/dev/null
export PYTHONPATH=/src:/scripts
if [ -z "${DEVFORGE_DATABASE_URL:-}" ] && [ -n "${DEVFORGE_POSTGRES_PASSWORD:-}" ]; then
  export DEVFORGE_DATABASE_URL="postgresql+asyncpg://postgres:${DEVFORGE_POSTGRES_PASSWORD}@127.0.0.1:5432/devforge_app"
fi
exec python3 -m devforge.cli mcp serve --host 0.0.0.0 --port 8000
