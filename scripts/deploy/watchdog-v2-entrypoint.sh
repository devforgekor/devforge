#!/bin/bash
set -euo pipefail
# [WHY] KV에 DEVFORGE_DATABASE_URL이 없다(F4). devforge 패키지를 소스에서 설치.
pip install --root-user-action=ignore -e /opt/projects/server 2>/dev/null
export PYTHONPATH=/opt/projects/server/src:/scripts
if [ -z "${DEVFORGE_DATABASE_URL:-}" ] && [ -n "${DEVFORGE_POSTGRES_PASSWORD:-}" ]; then
  export DEVFORGE_DATABASE_URL="postgresql+asyncpg://postgres:${DEVFORGE_POSTGRES_PASSWORD}@127.0.0.1:5432/devforge_app"
fi
exec python3 -m devforge.cli watchdog serve