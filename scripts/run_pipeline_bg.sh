#!/usr/bin/env bash
# prj_cycle.py → test_verify_optimization.py 순차 실행
set -e
cd /opt/projects/server/scripts
export PYTHONPATH=/opt/projects/server/scripts

LOG_DIR=/opt/projects/server/data/experiment
mkdir -p "$LOG_DIR"
PRJ_LOG="$LOG_DIR/prj_cycle_$(date +%Y%m%d_%H%M).log"
VERIFY_LOG="$LOG_DIR/test_verify_opt_$(date +%Y%m%d_%H%M).log"

echo "[$(date -u +%H:%M:%S)] Starting prj_cycle.py (Round 1: no rubric → Round 2: rubric)..." | tee -a "$PRJ_LOG"
python3 prj_cycle.py >> "$PRJ_LOG" 2>&1
RC=$?
echo "[$(date -u +%H:%M:%S)] prj_cycle.py exit code: $RC" | tee -a "$PRJ_LOG"

echo "[$(date -u +%H:%M:%S)] Starting test_verify_optimization.py..." | tee -a "$VERIFY_LOG"
python3 ../tests/test_verify_optimization.py >> "$VERIFY_LOG" 2>&1
RC=$?
echo "[$(date -u +%H:%M:%S)] test_verify_optimization.py exit code: $RC" | tee -a "$VERIFY_LOG"

echo "[$(date -u +%H:%M:%S)] ALL DONE." | tee -a "$PRJ_LOG" "$VERIFY_LOG"
