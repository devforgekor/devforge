#!/bin/bash
set -euo pipefail
# [WHY] Fail fast with an actionable message: a silent pip failure surfaces later
# as ModuleNotFoundError crash loop (2026-09-23 empty /pip-cache incident).
if ! pip install --no-index --find-links=/pip-cache --root-user-action=ignore \
    typer rich structlog pyyaml asyncpg alembic pgvector >/dev/null 2>&1; then
  echo "FATAL: offline pip install from /pip-cache failed — cache empty or incomplete (/opt/ai_data/pip-cache)" >&2
  exit 1
fi
export PYTHONPATH=/src:/scripts
if [ -z "${DEVFORGE_DATABASE_URL:-}" ] && [ -n "${DEVFORGE_POSTGRES_PASSWORD:-}" ]; then
  export DEVFORGE_DATABASE_URL="postgresql+asyncpg://postgres:${DEVFORGE_POSTGRES_PASSWORD}@127.0.0.1:5432/devforge_app"
fi
exec python3 -m devforge.cli mcp serve --host 0.0.0.0 --port 8000
