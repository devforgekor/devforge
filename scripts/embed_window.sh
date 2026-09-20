#!/bin/bash
# embed_window.sh — day-cycle + watchdog pause → embed mode → embed_batch → resume
#
# extract(:8082)와 embedding(:8081)은 상호배타(동시 로드 금지). day-cycle은 물론
# watchdog도 :8082 day 모델을 유지/복구하려 하므로, 임베딩 동안 둘 다 멈춘다.
# (watchdog는 이 윈도우 동안에만 정지 — 다른 감시는 잠시 중단됨)
#
# 사용법:
#   embed_window.sh [limit]     # limit 생략 시 embed_batch 기본 배치
set -uo pipefail

PAUSE="$HOME/.config/devforge/day-cycle.paused"
SERVER_DIR="/opt/projects/server"
LOG() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

LIMIT="${1:-}"
WD_WAS_ACTIVE=0

LOG "embed_window start (limit=${LIMIT:-default})"
mkdir -p "$(dirname "$PAUSE")"
touch "$PAUSE"

# day-cycle 중지 + 잔여 프로세스 정리
systemctl --user stop devforge-day-cycle.service 2>/dev/null || true
for _ in $(seq 1 15); do
    pgrep -f "day_cycle.sh" >/dev/null 2>&1 || break
    sleep 2
done
pkill -f "scripts/day_cycle.sh" 2>/dev/null || true

# watchdog 정지 (embed 중 :8082 부재를 '장애'로 오인해 복구하지 않도록)
if systemctl --user is-active --quiet devforge-watchdog.service; then
    WD_WAS_ACTIVE=1
    systemctl --user stop devforge-watchdog.service 2>/dev/null || true
fi

cd "$SERVER_DIR" || exit 1
if [ -n "$LIMIT" ]; then
    python3 scripts/pipelines/embed_batch.py --limit "$LIMIT"
else
    python3 scripts/pipelines/embed_batch.py
fi
rc=$?

# 복구
if [ "$WD_WAS_ACTIVE" = "1" ]; then
    systemctl --user start devforge-watchdog.service 2>/dev/null || true
fi
rm -f "$PAUSE"
LOG "embed_window done (rc=$rc) — watchdog/day-cycle resumed"

exit "$rc"
