#!/bin/bash
# 32B IQ4_XS baseline test — 9 selected tasks (renumbered 1-9 by difficulty)
# L1→L16: slack_operator, auto_commit_guard, code_mod_pipeline, key_rotator

set -e

cd /opt/projects/server
TASKS=(1 2 3 4 5 6 7 8 9)
LOGDIR="/var/tmp/code_mod_tests"
mkdir -p "$LOGDIR"

echo "========================================================"
echo "32B IQ4_XS Baseline Test — $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "9 tasks: ${TASKS[*]}"
echo "Log dir: $LOGDIR"
echo "========================================================"

total_start=$(date +%s)
passed=0
failed=0
results=""

for tid in "${TASKS[@]}"; do
    echo ""
    echo ">>> Task $tid starting at $(date '+%H:%M:%S') <<<"
    task_start=$(date +%s)

    if python3 scripts/code_mod_pipeline.py --local-only --task "$tid" 2>&1 | tee "$LOGDIR/baseline_task${tid}.log"; then
        elapsed=$(( $(date +%s) - task_start ))
        echo ">>> Task $tid: PASS (${elapsed}s) <<<"
        passed=$((passed + 1))
        results="$results\n  T$tid: PASS (${elapsed}s)"
    else
        elapsed=$(( $(date +%s) - task_start ))
        echo ">>> Task $tid: FAIL (${elapsed}s) <<<"
        failed=$((failed + 1))
        results="$results\n  T$tid: FAIL (${elapsed}s)"
    fi
done

total_elapsed=$(( $(date +%s) - total_start ))
echo ""
echo "========================================================"
echo "Baseline Complete: ${total_elapsed}s total"
echo "Passed: $passed / ${#TASKS[@]}"
echo "Failed: $failed"
echo -e "Results:$results"
echo "========================================================"
