#!/bin/bash
set -euo pipefail
# [WHY] KV에 DEVFORGE_DATABASE_URL이 없다(F4). mcp entrypoint와 동일하게 pod 내부 DSN을 구성.
# deps from pip-cache (like mcp entrypoint)
pip install --no-index --find-links=/pip-cache --root-user-action=ignore typer rich structlog pyyaml asyncpg alembic pgvector pydantic pydantic-settings sse-starlette oci psutil apprise requests-oauthlib click markdown python-dateutil pyjwt cryptography pyopenssl crc32c circuitbreaker aiohttp yarl multidict frozenlist attrs aiosignal propcache aiohappyeyeballs markdown-it-py oauthlib 2>/dev/null
export PYTHONPATH=/src:/scripts
if [ -z "${DEVFORGE_DATABASE_URL:-}" ] && [ -n "${DEVFORGE_POSTGRES_PASSWORD:-}" ]; then
  export DEVFORGE_DATABASE_URL="postgresql+asyncpg://postgres:${DEVFORGE_POSTGRES_PASSWORD}@127.0.0.1:5432/devforge_app"
fi
exec python3 -m devforge.cli watchdog serve