#!/bin/bash
# Batch model test runner — extraction all → Phi-4 once → DeepSeek once
set -e
cd /opt/projects/server/scripts
LOG="/opt/projects/server/model_sequence_$(date +%Y%m%d_%H%M%S).log"

# Load API keys for cross-validation
set -a
source /home/opc/.config/devforge/secrets.env
set +a

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Waiting for current harness to finish..." | tee -a "$LOG"

# Wait for any existing model_test_harness / run_batch to exit
while pgrep -f "python3.*model_test_harness\|python3.*run_batch" >/dev/null 2>&1; do
    sleep 60
done

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting batch test (3 models → Phi-4 once → DeepSeek once)" | tee -a "$LOG"
python3 -u run_batch.py 2>&1 | tee -a "$LOG"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Batch completed." | tee -a "$LOG"
