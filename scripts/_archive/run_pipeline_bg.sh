#!/usr/bin/env bash
# prj_cycle.py → bench_verify_optimization.py 순차 실행
cd /opt/projects/server/scripts
export PYTHONPATH=/opt/projects/server/scripts

LOG_DIR=/opt/projects/server/data/experiment
mkdir -p "$LOG_DIR"
PRJ_LOG="$LOG_DIR/prj_cycle_$(date +%Y%m%d_%H%M).log"
VERIFY_LOG="$LOG_DIR/bench_verify_opt_$(date +%Y%m%d_%H%M).log"

echo "[$(date -u +%H:%M:%S)] Starting prj_cycle.py (Round 1: no rubric → Round 2: rubric)..." | tee -a "$PRJ_LOG"
python3 pipelines/prj_cycle.py >> "$PRJ_LOG" 2>&1
RC1=$?
echo "[$(date -u +%H:%M:%S)] prj_cycle.py exit code: $RC1" | tee -a "$PRJ_LOG"

echo "[$(date -u +%H:%M:%S)] Starting bench_verify_optimization.py..." | tee -a "$VERIFY_LOG"
python3 ../tests/_archive/old_tests/bench_verify_optimization.py >> "$VERIFY_LOG" 2>&1
RC2=$?
echo "[$(date -u +%H:%M:%S)] bench_verify_optimization.py exit code: $RC2" | tee -a "$VERIFY_LOG"

echo "[$(date -u +%H:%M:%S)] ALL DONE." | tee -a "$PRJ_LOG" "$VERIFY_LOG"
