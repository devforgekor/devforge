#!/bin/bash
# night_cycle.sh — DevForge nightly pipeline (KST 03:00 / UTC 18:00)
# All output to journald via systemd service.
#
# System Mode Transition:
#   devforge-night-cycle.timer fires at UTC 18:00 (KST 03:00)
#   ──> MODE=night  (at script start)
#   ──> Day Mode Restore at end sets MODE=day
#   ──> On crash: EXIT trap restores MODE=day as safety net
#
# Pipeline Steps:
#   Server Validation     — state_collector --validate (snapshot before switching)
#   Night Review (P-R-J)  — 30B(:8081) → 14B(:8082) → NextCoder 14B(:8083)
#   Night Verify          — 27B(:8084) final gate via review_consumer.py
#   Day Mode Restore      — Pod B extractor(:8082) + Pod A reserved(:8080)
#   Extract Test          — nightly_extract_test.py (faithfulness check)
#   Proxy Audit           — proxy_reviewer.py (DeepSeek Pro verify audit)

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
    # Pod A: MODE=reserved only (hardcodes model in its entrypoint)
    printf '%s' "MODE=$mode_a" > "${MODE_FILE_A}.tmp" && mv "${MODE_FILE_A}.tmp" "$MODE_FILE_A"
    # Pod B: full env via pod_manager (MODEL_FILE, PORT, CTX_SIZE, etc.)
    python3 -c "
import sys; sys.path.insert(0, '$SCRIPTS_DIR')
from lib.pod_manager import _write_mode_env
_write_mode_env('$mode_b', 8082)
" 2>&1 || {
        echo "[$(LOG_TS)] WARNING: _write_mode_env failed — Pod B may not start"
    }
    systemctl --user stop container-devforge-pod-b 2>&1 || true
    sleep 3  # wait for pasta to release ports 8081-8084
    if systemctl --user start container-devforge-pod-b 2>&1; then
        return 0
    else
        echo "[$(LOG_TS)] ERROR: failed to start container-devforge-pod-b (port race)" >&2
        return 1
    fi
}

switch_mode_pod_b() {
    local mode="$1"
    local port="${2:-8082}"
    echo "[$(LOG_TS)] Switching Pod B to $mode (:$port)..."
    python3 -c "
import sys; sys.path.insert(0, '$SCRIPTS_DIR')
from lib.pod_manager import _write_mode_env
_write_mode_env('$mode', $port)
" 2>&1 || {
        echo "[$(LOG_TS)] WARNING: _write_mode_env failed — Pod B may not start"
    }
    systemctl --user stop container-devforge-pod-b 2>&1 || true
    sleep 3  # wait for pasta to release ports 8081-8084
    if systemctl --user start container-devforge-pod-b 2>&1; then
        return 0
    else
        echo "[$(LOG_TS)] ERROR: failed to start container-devforge-pod-b (port race)" >&2
        return 1
    fi
}

stop_llm_services() {
    local label="$1"
    echo "[$(LOG_TS)] [$label] Stopping LLM services and timers..."
    for svc in activity-summarizer telegram-bot slack; do
        systemctl --user stop "$svc" 2>&1 || true
    done
    for tmr in activity-summarizer.timer devforge-day-cycle.timer; do
        systemctl --user stop "$tmr" 2>&1 || true
    done
    echo "[$(LOG_TS)] [$label] All non-critical LLM services stopped"
}

start_llm_services() {
    local label="$1"
    echo "[$(LOG_TS)] [$label] Restarting LLM services and timers..."
    for tmr in activity-summarizer.timer devforge-day-cycle.timer; do
        systemctl --user start "$tmr" 2>&1 || true
    done
    for svc in activity-summarizer telegram-bot slack; do
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
# night_cycle.py --queue handles its own container management
# (kill_all → sequential P→R→J model loading on Pod B).
# Reads pending extract_results from activity_log.
# On success: queue_status → 'reviewed' (consumed by Phase 5 review_consumer.py).

review_ok=true

echo "[$(LOG_TS)] === Phase 4: P-R-J Review Pipeline ==="
if ! python3 "$SCRIPTS_DIR/pipelines/night_cycle.py" --queue --limit 5; then
    review_ok=false
    echo "[$(LOG_TS)] night_cycle.py --queue FAILED" >&2
fi

# ── Phase 5: Production verify (night_verify) ───────────────────

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
    echo "[$(LOG_TS)] === Phase 5: Production verify (night_verify) ==="
    stop_llm_services "verify"
    echo "[$(LOG_TS)] Stopping Pod A (memory for 27B)..."
    systemctl --user stop container-devforge-pod-a 2>&1 || true
    sleep 5

    if switch_mode_pod_b "verify" 8084 && wait_for_model 8084 "Qwen3.6-27B" 600; then
        retry "verify" 2 python3 "$SCRIPTS_DIR/pipelines/review_consumer.py" || verify_ok=false
    else
        echo "[$(LOG_TS)] Failed to start verify mode" >&2
        verify_ok=false
    fi
fi

# ── Phase 6: restore day mode ───────────────────────────────

day_restored=true

echo "[$(LOG_TS)] === Night → Day transition ==="
if ! switch_mode_both "reserved" "day"; then
    day_restored=false
    echo "[$(LOG_TS)] FATAL: switch_mode day failed" >&2
else
    start_llm_services "day-restore"
    # Pod B extractor mode on :8082
    if ! wait_for_model 8082 "Pod B extractor (day)" 300; then
        day_restored=false
        echo "[$(LOG_TS)] FATAL: extractor (:8082) not responding after restore" >&2
    fi
    # Pod A reserved(:8080)
    echo "[$(LOG_TS)] Restarting Pod A (reserved:8080)..."
    systemctl --user restart container-devforge-pod-a 2>&1 || true
    sleep 5
    if ! wait_for_model 8080 "Pod A (reserved)" 60; then
        echo "[$(LOG_TS)] WARNING: Pod A :8080 not responding" >&2
    fi
fi

if [ "$day_restored" = "false" ]; then
    echo "[$(LOG_TS)] CRITICAL: day restoration failed — daytime NL queries will be blocked" >&2
	fi

# ── Day mode restored ─────────────────────────────────────
_set_mode day
_restored=true

# ── Daily structure sync (chain: state_collector → gen_architecture) ─
# Primary run after nightly. Falls back to KST 09:00 timer on failure.
echo "[$(LOG_TS)] == Daily structure sync == "
if systemctl --user start devforge-daily-structure.service 2>/dev/null; then
    echo "[$(LOG_TS)] Daily structure sync OK"
else
    echo "[$(LOG_TS)] Daily structure sync FAILED — KST 09:00 timer will retry" >&2
fi

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

echo "[$(LOG_TS)] === Phase 8: DeepSeek Pro verify audit ==="
if python3 "$SCRIPTS_DIR/pipelines/proxy_reviewer.py" --limit 50; then
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
day: $($day_restored && echo ok || echo failed)
YAML

echo "[$(LOG_TS)] night_cycle complete"

# ── Chain: trigger backup ──────────────────────────────────
echo "[$(LOG_TS)] triggering devforge-backup.service..."
systemctl --user start devforge-backup.service
