#!/usr/bin/env bash
# prj_cycle.py → test_27b_optimization.py 순차 실행
set -e
cd /opt/projects/server/scripts
export PYTHONPATH=/opt/projects/server/scripts

LOG_DIR=/opt/projects/server/data/experiment
mkdir -p "$LOG_DIR"
PRJ_LOG="$LOG_DIR/prj_cycle_$(date +%Y%m%d_%H%M).log"
V27_LOG="$LOG_DIR/test_27b_opt_$(date +%Y%m%d_%H%M).log"

echo "[$(date -u +%H:%M:%S)] Starting prj_cycle.py (Round 1: no rubric → Round 2: rubric)..." | tee -a "$PRJ_LOG"
python3 prj_cycle.py >> "$PRJ_LOG" 2>&1
RC=$?
echo "[$(date -u +%H:%M:%S)] prj_cycle.py exit code: $RC" | tee -a "$PRJ_LOG"

echo "[$(date -u +%H:%M:%S)] Starting test_27b_optimization.py..." | tee -a "$V27_LOG"
python3 test_27b_optimization.py >> "$V27_LOG" 2>&1
RC=$?
echo "[$(date -u +%H:%M:%S)] test_27b_optimization.py exit code: $RC" | tee -a "$V27_LOG"

echo "[$(date -u +%H:%M:%S)] ALL DONE." | tee -a "$PRJ_LOG" "$V27_LOG"
