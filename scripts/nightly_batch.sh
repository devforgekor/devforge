#!/bin/bash
# nightly_batch.sh — DevForge nightly pipeline (KST 03:00 / UTC 18:00)
# All output to journald via systemd service.
#
# System Mode Transition:
#   devforge-nightly.timer fires at UTC 18:00 (KST 03:00)
#   ──> MODE=night  (at script start, /opt/ai_data/scripts/current-system-mode.env)
#   ──> Phase 1..6 pipeline runs
#   ──> Phase 6 restores day mode, MODE=day
#   ──> Phase 7 DeepSeek API audit runs in day mode
#   On crash: EXIT trap restores MODE=day as safety net
#
# Agent mode check:
#   cat /opt/ai_data/scripts/current-system-mode.env           # "MODE=day" or "MODE=night"
#   python3 -c "print(open('/opt/ai_data/scripts/current-system-mode.env').read().strip().split('=')[1])"
#
# Phases:
# Phase 4: review pipeline       mid    — R1(:8083) + Qwen7B(:8080) + Selene(:8081) P→R→J
# Phase 5: verify (27B)          heavy  — production final gate, Pod B 27B(:8081)
# Phase 5b: test_verify (32B)    heavy  — experimental parallel verify, Pod B 32B(:8081)
# Phase 6: restore day                  — Pod A(3B:8082 operator 겸) + Pod B(7B:8080) back to day

set -o pipefail

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
STATUS_FILE="/opt/projects/server/data/nightly_status.yaml"
MODE_FILE_A="/opt/ai_data/scripts/current-mode-pod-a.env"
MODE_FILE_B="/opt/ai_data/scripts/current-mode-pod-b.env"
MODE_FILE="/opt/ai_data/scripts/current-system-mode.env"
SCRIPTS_DIR="/opt/projects/server/scripts"

# System mode trap: always restore to day on exit (crash or normal)
_restored=false
_set_mode() { echo "MODE=$1" > "$MODE_FILE"; echo "[$(LOG_TS)] System mode: $1"; }
trap '_restored || _set_mode day' EXIT

retry() {
    local name="$1"; shift
    local max="${1:-3}"; shift || true

    for i in $(seq 1 "$max"); do
        echo "[$(LOG_TS)] $name (attempt $i/$max)"
        if "$@"; then
            echo "[$(LOG_TS)] $name OK"
            return 0
        fi
        echo "[$(LOG_TS)] $name FAILED (attempt $i/$max)" >&2
        sleep $((2 ** i))
    done
    echo "[$(LOG_TS)] $name ABORTED after $max attempts" >&2
    return 1
}

wait_for_model() {
    local port="$1" label="$2" max_wait="${3:-300}"
    echo "[$(LOG_TS)] Waiting for $label on :$port..."
    for i in $(seq 1 "$max_wait"); do
        if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port/health" 2>/dev/null | grep -q 200; then
            echo "[$(LOG_TS)] $label ready on :$port (${i}s)"
            return 0
        fi
        sleep 2
    done
    echo "[$(LOG_TS)] $label TIMEOUT after ${max_wait}s" >&2
    return 1
}

switch_mode_both() {
    local mode_a="$1"
    local mode_b="$2"
    echo "[$(LOG_TS)] Switching Pod A → $mode_a, Pod B → $mode_b..."
    printf '%s' "MODE=$mode_a" > "${MODE_FILE_A}.tmp" && mv "${MODE_FILE_A}.tmp" "$MODE_FILE_A"
    printf '%s' "MODE=$mode_b" > "${MODE_FILE_B}.tmp" && mv "${MODE_FILE_B}.tmp" "$MODE_FILE_B"
    systemctl --user stop container-devforge-swap 2>&1 || true
    sleep 3  # wait for pasta to release ports 8080-8081
    if systemctl --user start container-devforge-swap 2>&1; then
        return 0
    else
        echo "[$(LOG_TS)] ERROR: failed to start container-devforge-swap (port race)" >&2
        return 1
    fi
}

switch_mode_pod_b() {
    local mode="$1"
    echo "[$(LOG_TS)] Switching Pod B to $mode..."
    printf '%s' "MODE=$mode" > "${MODE_FILE_B}.tmp" && mv "${MODE_FILE_B}.tmp" "$MODE_FILE_B"
    systemctl --user stop container-devforge-swap 2>&1 || true
    sleep 3  # wait for pasta to release ports 8080-8081
    if systemctl --user start container-devforge-swap 2>&1; then
        return 0
    else
        echo "[$(LOG_TS)] ERROR: failed to start container-devforge-swap (port race)" >&2
        return 1
    fi
}

stop_llm_services() {
    local label="$1"
    echo "[$(LOG_TS)] [$label] Stopping LLM services and timers..."
    for svc in activity-summarizer telegram-bot webhook-server; do
        systemctl --user stop "$svc" 2>&1 || true
    done
    for tmr in activity-summarizer.timer devforge-15m-cycle.timer; do
        systemctl --user stop "$tmr" 2>&1 || true
    done
    echo "[$(LOG_TS)] [$label] All non-critical LLM services stopped"
}

start_llm_services() {
    local label="$1"
    echo "[$(LOG_TS)] [$label] Restarting LLM services and timers..."
    for tmr in activity-summarizer.timer devforge-15m-cycle.timer; do
        systemctl --user start "$tmr" 2>&1 || true
    done
    for svc in activity-summarizer telegram-bot webhook-server; do
        systemctl --user start "$svc" 2>&1 || true
    done
    echo "[$(LOG_TS)] [$label] LLM services restored"
}

# --- Night mode activation ---
_set_mode night

# ── Phase 1: Server Validation ────────────────────────────
# Lightweight — runs before any mode switching to snapshot daytime state.

validation_ok=true
echo "[$(LOG_TS)] === Phase 1: Server Validation ==="
if python3 "$SCRIPTS_DIR/state_collector/main.py" --validate; then
    echo "[$(LOG_TS)] Server validation OK"
else
    validation_ok=false
    echo "[$(LOG_TS)] Server validation had issues (non-fatal)" >&2
fi

# ── Phase 4: P-R-J Review Pipeline (queue consumer) ──────────────
# prj_cycle.py --queue handles its own container management
# (kill_all → sequential P→R→J model loading on Pod B :8080).
# Reads pending extract_results from activity_log.
# On success: queue_status → 'reviewed' (consumed by Phase 5 review_consumer.py).

review_ok=true

echo "[$(LOG_TS)] === Phase 4: P-R-J Review Pipeline ==="
if ! python3 "$SCRIPTS_DIR/prj_cycle.py" --queue --limit 5; then
    review_ok=false
    echo "[$(LOG_TS)] prj_cycle.py --queue FAILED" >&2
fi

# ── Phase 5: 27B production verify ──────────────────────────────

verify_ok=true

queue_count=$(cd "$SCRIPTS_DIR" && python3 -c "
from lib.db import psql
r = psql(\"SELECT COUNT(*) FROM activity_log WHERE queue_status='reviewed' AND type IN ('review','debate_result','extract_result')\")
import sys
val = r.strip() if r else '0'
print(val if val else '0')
" 2>/dev/null)
echo "[$(LOG_TS)] Verify queue (status='reviewed'): $queue_count items"

if [ "$queue_count" = "0" ] || [ -z "$queue_count" ]; then
    echo "[$(LOG_TS)] Verify queue empty — skipping 27B verify"
else
    echo "[$(LOG_TS)] === Phase 5: 27B production verify ==="
    stop_llm_services "verify"
    echo "[$(LOG_TS)] Stopping Pod A (memory for 27B)..."
    systemctl --user stop container-devforge-qwen 2>&1 || true
    sleep 5

    if switch_mode_pod_b "verify" && wait_for_model 8081 "Qwen3.6-27B" 600; then
        retry "verify" 2 python3 "$SCRIPTS_DIR/review_consumer.py" || verify_ok=false
    else
        echo "[$(LOG_TS)] Failed to start verify mode" >&2
        verify_ok=false
    fi
fi

# ── Phase 5b: 32B experimental test_verify (parallel consumer) ──

test_verify_ok=true

test_queue_count=$(cd "$SCRIPTS_DIR" && python3 -c "
from lib.db import psql
r = psql(\"SELECT COUNT(*) FROM activity_log WHERE queue_status IN ('reviewed','done') AND type IN ('review','debate_result','extract_result') AND (body->'test_verify_result' IS NULL OR body->'test_verify_result' = 'null'::jsonb)\")
import sys
val = r.strip() if r else '0'
print(val if val else '0')
" 2>/dev/null)
echo "[$(LOG_TS)] Test-verify queue (not yet 32B-reviewed): $test_queue_count items"

if [ "$test_queue_count" = "0" ] || [ -z "$test_queue_count" ]; then
    echo "[$(LOG_TS)] Test-verify queue empty — skipping 32B test verify"
else
    echo "[$(LOG_TS)] === Phase 5b: 32B experimental test_verify ==="
    if switch_mode_pod_b "verify_test" && wait_for_model 8081 "Qwen2.5-Coder-32B" 900; then
        retry "test_verify" 1 python3 "$SCRIPTS_DIR/test_review_consumer.py" || test_verify_ok=false
    else
        echo "[$(LOG_TS)] Failed to start verify_test mode" >&2
        test_verify_ok=false
    fi
fi

# ── Phase 6: restore day mode ───────────────────────────────

day_restored=true

echo "[$(LOG_TS)] === Night → Day transition ==="
if ! switch_mode_both "day" "day"; then
    day_restored=false
    echo "[$(LOG_TS)] FATAL: switch_mode day failed" >&2
else
    start_llm_services "day-restore"
    # Pod B 7B on :8080
    if ! wait_for_model 8080 "7B (Pod B)" 300; then
        day_restored=false
        echo "[$(LOG_TS)] FATAL: 7B :8080 not responding after restore" >&2
    fi
    # Pod A 3B(:8082) operator+refuter
    echo "[$(LOG_TS)] Restarting Pod A (3B operator+refuter)..."
    systemctl --user restart container-devforge-qwen 2>&1 || true
    sleep 5
    if ! wait_for_model 8082 "3B (Pod A)" 60; then
        echo "[$(LOG_TS)] WARNING: 3B refuter :8082 not responding" >&2
    fi
fi

if [ "$day_restored" = "false" ]; then
    echo "[$(LOG_TS)] CRITICAL: day restoration failed — daytime NL queries will be blocked" >&2
	fi

# ── Day mode restored ─────────────────────────────────────
_set_mode day
_restored=true

# ── Phase 7: Extract faithfulness test ─────────────────────────

extract_test_ok=true

echo "[$(LOG_TS)] === Phase 7: Extract faithfulness test ==="
if python3 "$SCRIPTS_DIR/nightly_extract_test.py" --model-label Qwen3-4B; then
    echo "[$(LOG_TS)] Extract faithfulness test OK"
else
    extract_test_ok=false
    echo "[$(LOG_TS)] Extract faithfulness test had issues (non-fatal)" >&2
fi

# ── Phase 8: DeepSeek Pro verify audit ──────────────────────

proxy_ok=true

echo "[$(LOG_TS)] === Phase 7: DeepSeek Pro verify audit ==="
if python3 "$SCRIPTS_DIR/proxy_reviewer.py" --limit 50; then
    echo "[$(LOG_TS)] DeepSeek Pro review OK"
else
    proxy_ok=false
    echo "[$(LOG_TS)] DeepSeek Pro review had issues (non-fatal)" >&2
fi

# ── Status summary (consumed by 9 AM Slack hook) ──────────────
cat > "$STATUS_FILE" <<YAML
timestamp: "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
extract_test: $($extract_test_ok && echo ok || echo skipped)
validation: $($validation_ok && echo ok || echo failed)
review: $($review_ok && echo ok || echo failed)
verify: $($verify_ok && echo ok || echo skipped)
test_verify: $($test_verify_ok && echo ok || echo skipped)
day: $($day_restored && echo ok || echo failed)
YAML

echo "[$(LOG_TS)] nightly_batch complete"

# ── Chain: trigger backup ──────────────────────────────────
echo "[$(LOG_TS)] triggering devforge-backup.service..."
systemctl --user start devforge-backup.service
