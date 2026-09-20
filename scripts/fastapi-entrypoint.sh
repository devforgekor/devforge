#!/bin/bash
set -euo pipefail

exec /scripts/deploy/kv-fetch-env.py /bin/bash -c '
  if [ -z "${DEVFORGE_DATABASE_URL:-}" ]; then
    export DEVFORGE_DATABASE_URL="postgresql+asyncpg://postgres:${DEVFORGE_POSTGRES_PASSWORD}@127.0.0.1:5432/devforge_app"
  fi
  exec python3 -m uvicorn devforge_fastapi.app:app --host 0.0.0.0 --port 8002 \
    --proxy-headers --forwarded-allow-ips="*" --log-level info
'
