#!/usr/bin/env bash
# open-newhand — NewHand Protocol 마무리: handover.yaml → 커밋 → 푸시
# OpenCode 세션 내에서 AI가 호출 (핸드오버 요약은 AI가 먼저 작성)
set -euo pipefail

HANDOVER="/opt/projects/server/handover.yaml"
PROJECT_DIR="/opt/projects/server"

if [ ! -f "$HANDOVER" ]; then
  echo "[ERROR] handover.yaml 없음" >&2
  exit 1
fi

cd "$PROJECT_DIR"

# 변경 없으면 종료
if git diff --quiet && git diff --cached --quiet && [ "$(git ls-files --others --exclude-standard | wc -l)" -eq 0 ]; then
  echo "변경사항 없음 — 종료"
  exit 0
fi

# 커밋 메시지 생성 (handover last_checkpoint.summary)
MSG=$(python3 -c "
import yaml
with open('$HANDOVER') as f:
    d = yaml.safe_load(f)
ck = d.get('last_checkpoint', {})
summary = ck.get('summary', 'OpenCode session') or 'OpenCode session'
print(summary[:200])
")

git add -A
git commit -m "$MSG"
git push
echo "[DONE] $MSG"
