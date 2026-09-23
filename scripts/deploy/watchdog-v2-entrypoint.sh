#!/bin/bash
set -euo pipefail
# [WHY] pip -e 설치는 소스 경로 고정. DSN 폴백은 KV 미주입 시 방어용 (F4는 2026-09-23 해소).
pip install --root-user-action=ignore -e /opt/projects/server 2>/dev/null
export PYTHONPATH=/opt/projects/server/src:/scripts
if [ -z "${DEVFORGE_DATABASE_URL:-}" ] && [ -n "${DEVFORGE_POSTGRES_PASSWORD:-}" ]; then
  export DEVFORGE_DATABASE_URL="postgresql+asyncpg://postgres:${DEVFORGE_POSTGRES_PASSWORD}@127.0.0.1:5432/devforge_app"
fi
exec python3 -m devforge.cli watchdog serve