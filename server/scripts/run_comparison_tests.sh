#!/bin/bash
# DevForge 12-Run Comparison Test Runner
# T01, T05, T11 × Pipeline(api/local) + Debate(api/local) = 12 runs
# All evaluated by DeepSeek Pro at the end.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULT_DIR="/var/tmp/comparison_tests"
mkdir -p "$RESULT_DIR"

MODE_FILE="/opt/ai_data/scripts/current-mode.env"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN_LOG="$RESULT_DIR/runlog_${TIMESTAMP}.txt"
EVAL_OUT="$RESULT_DIR/evaluation_${TIMESTAMP}.json"

log() {
    echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$RUN_LOG"
}

# ── Test matrix ──
# Format: "system:task_id:api_flag"
TESTS=(
    # Pipeline (code mode) — 3 runs (local only)
    "pipeline:1:no-api"
    "pipeline:5:no-api"
    "pipeline:11:no-api"
    # Debate (debate mode) — 3 runs (local only)
    "debate:1:no-api"
    "debate:5:no-api"
    "debate:11:no-api"
)
# API tests deferred to tomorrow — add back:
#   "pipeline:1:with-api" "pipeline:5:with-api" "pipeline:11:with-api"
#   "debate:1:with-api" "debate:5:with-api" "debate:11:with-api"

TOTAL=${#TESTS[@]}
CURRENT=0

log "========================================="
log "12-Run Comparison Test — Started"
log "Total: $TOTAL runs"
log "========================================="

# ── Switch container mode ──
# CRITICAL: After 32B pipeline, llama-server holds 20GB+ model in RAM.
# stop+start with only 3s delay is insufficient — the old process may
# not have released memory before the new one starts, causing OOM crashes.
# Fix: stop all LLM containers first, wait for memory release, then start.
switch_mode() {
    local mode="$1"
    log "Switching to $mode mode..."
    echo "MODE=$mode" > "$MODE_FILE"

    # ── Pre-switch memory cleanup ──
    log "  Stopping all LLM containers..."
    systemctl --user stop container-devforge-qwen 2>/dev/null || true
    systemctl --user stop container-devforge-swap 2>/dev/null || true

    # Wait for llama-server processes to fully exit and release memory.
    # 32B model (20GB) + KV cache can take 5-10s for kernel to reclaim.
    log "  Waiting for memory release..."
    for i in $(seq 1 20); do
        if ! pgrep -f "llama-server" > /dev/null 2>&1; then
            log "  llama-server processes exited after ${i}s"
            break
        fi
        sleep 1
    done
    # Force kill any remaining llama-server processes
    pkill -9 -f "llama-server" 2>/dev/null || true
    sleep 5  # let kernel reclaim memory

    # Verify swap is not exhausted (fails if swap > 90% full)
    local swap_used=$(free | awk '/Swap:/ {print $3}')
    local swap_total=$(free | awk '/Swap:/ {print $2}')
    if [[ "$swap_total" -gt 0 ]]; then
        local swap_pct=$(( swap_used * 100 / swap_total ))
        log "  Swap: ${swap_used}/${swap_total} KB (${swap_pct}%)"
        if [[ $swap_pct -gt 90 ]]; then
            log "  WARNING: Swap nearly full — dropping caches"
            echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null 2>&1 || true
        fi
    fi

    case "$mode" in
        code)
            systemctl --user start container-devforge-swap
            log "  Waiting for 32B model to load on :8081 (up to 420s)..."
            for i in $(seq 1 140); do
                if curl -s http://127.0.0.1:8081/health 2>/dev/null | grep -q '"status":"ok"'; then
                    log "  32B model ready after ${i}0s"
                    return 0
                fi
                sleep 10
            done
            log "  ERROR: 32B model failed to load"
            return 1
            ;;
        debate)
            systemctl --user start container-devforge-swap
            log "  Waiting for debate supervisor + Selene on :8082 (up to 120s)..."
            for i in $(seq 1 24); do
                if curl -s http://127.0.0.1:8082/health 2>/dev/null | grep -q '"status":"ok"'; then
                    log "  Debate mode ready after ${i}5s"
                    return 0
                fi
                sleep 5
            done
            log "  ERROR: Debate mode failed to start"
            return 1
            ;;
    esac
}

# ── Read task description from YAML ──
get_task_question() {
    local task_id="$1"
    python3 -c "
import yaml, sys
with open('$SCRIPT_DIR/../code_mod_test_tasks.yaml') as f:
    config = yaml.safe_load(f)
for t in config['tasks']:
    if t['id'] == $task_id:
        desc = t['description'].strip().replace('\n', ' ')
        print(f\"File: {t['file']}\\nTask: {desc}\")
        sys.exit(0)
print('Unknown task')
" 2>/dev/null
}

# ── Run pipeline test ──
run_pipeline() {
    local task_id="$1" api_flag="$2"
    local extra_args=""
    [[ "$api_flag" == "with-api" ]] && extra_args="--with-api"
    log "  [pipeline] Task $task_id $api_flag starting..."
    cd "$SCRIPT_DIR"
    python3 code_mod_pipeline.py --task "$task_id" --local-only $extra_args \
        >> "$RESULT_DIR/pipeline_t${task_id}_${api_flag}.log" 2>&1
    local rc=$?
    log "  [pipeline] Task $task_id $api_flag → exit=$rc"
    return $rc
}

# ── Run debate test ──
run_debate() {
    local task_id="$1" api_flag="$2"
    local question
    question=$(get_task_question "$task_id")
    local extra_args="--skip-drag"
    [[ "$api_flag" == "with-api" ]] && extra_args="$extra_args --with-api"
    log "  [debate] Task $task_id $api_flag starting..."
    log "  [debate] question: ${question:0:120}..."
    cd "$SCRIPT_DIR"
    python3 debate.py --question "$question" --method drag --mode debate $extra_args \
        >> "$RESULT_DIR/debate_t${task_id}_${api_flag}.log" 2>&1
    local rc=$?
    log "  [debate] Task $task_id $api_flag → exit=$rc"
    return $rc
}

# ── Main ──
CURRENT_MODE=""
FAILED=()

for test in "${TESTS[@]}"; do
    IFS=':' read -r system task_id api_flag <<< "$test"
    CURRENT=$((CURRENT + 1))

    log ""
    log "── [$CURRENT/$TOTAL] $system task=$task_id api=$api_flag ──"

    # Switch mode if needed
    if [[ "$system" == "pipeline" && "$CURRENT_MODE" != "code" ]]; then
        switch_mode code || { log "FATAL: mode switch failed"; exit 1; }
        CURRENT_MODE="code"
    elif [[ "$system" == "debate" && "$CURRENT_MODE" != "debate" ]]; then
        switch_mode debate || { log "FATAL: mode switch failed"; exit 1; }
        CURRENT_MODE="debate"
    fi

    # Run
    if [[ "$system" == "pipeline" ]]; then
        if run_pipeline "$task_id" "$api_flag"; then
            log "  [$CURRENT/$TOTAL] PASS"
        else
            log "  [$CURRENT/$TOTAL] FAIL"
            FAILED+=("$test")
        fi
    else
        if run_debate "$task_id" "$api_flag"; then
            log "  [$CURRENT/$TOTAL] PASS"
        else
            log "  [$CURRENT/$TOTAL] FAIL"
            FAILED+=("$test")
        fi
    fi
done

log ""
log "========================================="
log "12-Run Test Complete"
log "Failed: ${#FAILED[@]} / $TOTAL"
for f in "${FAILED[@]}"; do
    log "  FAILED: $f"
done
log "Results: $RESULT_DIR"
log "Log: $RUN_LOG"
log "========================================="
