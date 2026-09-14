#!/bin/bash
pip install --no-index --find-links=/pip-cache --root-user-action=ignore typer rich structlog pyyaml asyncpg alembic pgvector 2>/dev/null
export PYTHONPATH=/src:/scripts
exec python3 -m devforge.cli mcp serve --host 0.0.0.0 --port 8000