#!/bin/bash
# weekly_enrich_rebuild.sh — Weekly enrich few-shot diversity rebuild
# Called by devforge-weekly-enrich-rebuild.timer
# Tasks:
#   1. Quality check (CONTRADICTION rate week-over-week)
#   2. Diversity-first pick from feedback_examples
#   3. Update config/enrich_few_shot.yaml
#   4. Cycle Pod B if running (next day_cycle picks up new prompts)
#
# Schedule: weekly, Monday 03:00 KST (= Sunday 18:00 UTC)

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
LOG() { echo "[$(LOG_TS)] $*"; }
SCRIPT_DIR="/opt/projects/server/scripts"

LOG "weekly_enrich_rebuild start"

# ── Phase 1: Quality check ──
LOG "  Quality check..."
QC=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from lib.enrich_few_shot import quality_check
result = quality_check()
if result is None:
    print('skip')
elif result:
    print('pass')
else:
    print('fail')
" 2>&1)

QC_STATUS=$(echo "$QC" | tail -1)
if [ "$QC_STATUS" = "fail" ]; then
    LOG "  QUALITY CHECK FAILED — CONTRADICTION rate worsened. Skipping rebuild."
    LOG "weekly_enrich_rebuild skipped (quality regression)"
    exit 0
fi
LOG "  Quality check: $QC_STATUS"

# ── Phase 2: Diversity rebuild ──
LOG "  Rebuilding few-shot examples..."
python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from lib.enrich_few_shot import rebuild
result = rebuild(dry_run=False)
print(f'  action={result[\"action\"]} slot={result.get(\"slot\",\"?\")} total={result.get(\"total_examples\",0)}')
" 2>&1
RB_EXIT=$?

if [ $RB_EXIT -ne 0 ]; then
    LOG "  REBUILD FAILED (exit=$RB_EXIT)" >&2
    LOG "weekly_enrich_rebuild FAILED"
    exit 1
fi

LOG "weekly_enrich_rebuild done"
