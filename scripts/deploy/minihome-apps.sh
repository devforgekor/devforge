#!/bin/bash
# minihome-apps.sh — 자체 호스팅 3개 Next.js 앱 (minihome4u.duckdns.org) 배포
# Called by: 수동 (agent_docs/DEPLOYMENT.md 게이트4)
# Tasks: npm install → next build → systemd 재시작 → 상태 출력
#
# [WHY] Vercel Hobby가 조직 소유 PRIVATE 저장소(devforgekor/minihome) 연결을
#       거부(HTTP 409)해 minihome·miniebook·news를 자체 서버로 옮겼다.
#       push 자동 배포 대신 이 스크립트로 수동 반영한다.
#
# 사용법:
#   minihome-apps.sh                       # 3개 전부 배포
#   minihome-apps.sh --only news           # 1개만
#   minihome-apps.sh --only news --only miniebook
#   minihome-apps.sh --no-install          # node_modules 신뢰, install 생략
#   minihome-apps.sh status                # 상태만 출력

set -uo pipefail

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
LOG() { echo "[$(LOG_TS)] $*"; }

ROOT="/opt/workspace/minihome"

# 앱 | 소스 상대경로 | 빌드 시 basePath | systemd 단위 | 포트
APPS=(
  "minihome|apps/minihome||minihome-web|8200"
  "miniebook|apps/ebooklib/apps/frontend|/miniebook|miniebook-web|8201"
  "news|apps/news/web|/news|news-web|8202"
)

SELECTED=()
DO_INSTALL=1
MODE=deploy

while [ $# -gt 0 ]; do
  case "$1" in
    --only) SELECTED+=("${2:-}"); shift 2 ;;
    --no-install) DO_INSTALL=0; shift ;;
    status) MODE=status; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) LOG "알 수 없는 인자: $1" >&2; exit 2 ;;
  esac
done

want() {
  [ ${#SELECTED[@]} -eq 0 ] && return 0
  local s
  for s in "${SELECTED[@]}"; do [ "$s" = "$1" ] && return 0; done
  return 1
}

status_all() {
  printf "%-12s %-10s %-6s %s\n" "APP" "STATE" "PORT" "UNIT"
  local e name _dir _bp unit port
  for e in "${APPS[@]}"; do
    IFS='|' read -r name _dir _bp unit port <<<"$e"
    printf "%-12s %-10s %-6s %s\n" \
      "$name" "$(systemctl --user is-active "$unit" 2>/dev/null || echo unknown)" \
      "$port" "$unit"
  done
}

if [ "$MODE" = "status" ]; then
  status_all
  exit 0
fi

FAILED=0
for e in "${APPS[@]}"; do
  IFS='|' read -r name dir base_path unit port <<<"$e"
  want "$name" || continue

  src="$ROOT/$dir"
  if [ ! -d "$src" ]; then
    LOG "SKIP $name (소스 없음: $src)" >&2
    continue
  fi

  LOG "=== $name ==="
  cd "$src" || { FAILED=1; continue; }

  if [ "$DO_INSTALL" -eq 1 ]; then
    LOG "  npm install"
    if ! npm install --no-audit --no-fund >/dev/null; then
      LOG "  FAIL $name: npm install" >&2
      FAILED=1
      continue
    fi
  fi

  LOG "  next build (basePath=${base_path:-루트})"
  if [ -n "$base_path" ]; then
    if ! NEXT_PUBLIC_BASE_PATH="$base_path" npm run build >/dev/null; then
      LOG "  FAIL $name: build" >&2
      FAILED=1
      continue
    fi
  else
    if ! npm run build >/dev/null; then
      LOG "  FAIL $name: build" >&2
      FAILED=1
      continue
    fi
  fi

  LOG "  restart $unit"
  if ! systemctl --user restart "$unit"; then
    LOG "  FAIL $name: restart" >&2
    FAILED=1
    continue
  fi

  sleep 3
  state=$(systemctl --user is-active "$unit" 2>/dev/null || echo unknown)
  if [ "$state" = "active" ]; then
    LOG "  OK $name → :$port ($state)"
  else
    LOG "  FAIL $name: $state" >&2
    FAILED=1
  fi
done

LOG "--- 상태 ---"
status_all
exit $FAILED
