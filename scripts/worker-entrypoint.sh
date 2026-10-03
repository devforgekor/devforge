#!/bin/bash
# worker-entrypoint.sh — DevForge Worker supervisor runner
set -e

export DEVFORGE_DB_TCP=1

# [WHY] scripts/lib/db.py 는 `psql -h 127.0.0.1 -U postgres` 로 붙는다. pg_hba 가
#       scram-sha-256 로 전환되면 PGPASSWORD 가 없으면 인증이 실패하므로, fastapi 와
#       동일하게 KV 를 주입한다 (2026-10-03). 최소 키만 노출한다.
exec /scripts/deploy/kv-fetch-env.py --keys DEVFORGE-POSTGRES-PASSWORD /bin/bash -c '
  if [ -z "${DEVFORGE_POSTGRES_PASSWORD:-}" ]; then
    echo "FATAL: DEVFORGE_POSTGRES_PASSWORD 미주입 — psql 인증 불가" >&2
    exit 1
  fi
  # [WHY] scripts/lib/db.py 는 `psql -h 127.0.0.1 -U postgres` 를 환경 상속으로 실행하고
  #       PGPASSWORD 인자도 주지 않는다. psql 은 DEVFORGE_POSTGRES_PASSWORD 를 읽지 않으므로
  #       PGPASSWORD 로 명시적으로 옮겨야 한다 (pg_hba=scram-sha-256, 2026-10-03).
  export PGPASSWORD="$DEVFORGE_POSTGRES_PASSWORD"
  echo "[worker-entrypoint] Starting worker supervisor..."
  exec python3 /scripts/worker_supervisor.py
'
