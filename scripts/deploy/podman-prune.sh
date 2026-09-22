#!/bin/bash
# Path: systemd:podman-prune.timer
# [WHY] 이미지가 매일 재빌드/재태깅(llama.cpp 등)되어 dangling 레이어가 하루 ~850MB씩 누적된다.
#       48h 이상 된 dangling만 제거해 최근 롤백 여지는 남긴다.
set -euo pipefail

RETENTION_HOURS="${PODMAN_PRUNE_RETENTION_HOURS:-48}"
STORAGE_DIR="${PODMAN_PRUNE_STORAGE_DIR:-/opt/ai_data}"
PODMAN="${PODMAN_BIN:-/usr/bin/podman}"
# [WHY] ~/.local/bin/tr 이 시스템 tr 을 가리는 환경(2026-09-22) — 절대경로로 고정.
DF="/usr/bin/df"
TR="/usr/bin/tr"

avail_before=$("$DF" -BM --output=avail "$STORAGE_DIR" | tail -1 | "$TR" -dc '0-9')
"$PODMAN" image prune -f --filter "until=${RETENTION_HOURS}h"
avail_after=$("$DF" -BM --output=avail "$STORAGE_DIR" | tail -1 | "$TR" -dc '0-9')

echo "podman-prune: reclaimed $((avail_after - avail_before))MB (retention=${RETENTION_HOURS}h, avail=${avail_after}MB)"
