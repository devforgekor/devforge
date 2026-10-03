#!/bin/bash
set -euo pipefail

exec /scripts/deploy/kv-fetch-env.py /bin/bash -c '
  if [ -z "${DEVFORGE_POSTGRES_PASSWORD:-}" ]; then
    echo "FATAL: DEVFORGE_POSTGRES_PASSWORD 미주입 — psql 인증 불가" >&2
    exit 1
  fi
  if [ -z "${DEVFORGE_DATABASE_URL:-}" ]; then
    export DEVFORGE_DATABASE_URL="postgresql+asyncpg://postgres:${DEVFORGE_POSTGRES_PASSWORD}@127.0.0.1:5432/devforge_app"
  fi
  # [WHY] 일부 코드는 scripts/lib/db.py 의 `psql -h 127.0.0.1 -U postgres` 경로를 쓴다.
  #       psql 은 DEVFORGE_POSTGRES_PASSWORD 를 읽지 않으므로 PGPASSWORD 로 옮긴다
  #       (pg_hba=scram-sha-256, 2026-10-03).
  export PGPASSWORD="$DEVFORGE_POSTGRES_PASSWORD"
  exec python3 -m uvicorn devforge_fastapi.app:app --host 0.0.0.0 --port 8002 \
    --proxy-headers --forwarded-allow-ips="*" --log-level info
'
