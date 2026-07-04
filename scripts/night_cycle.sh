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
#   Night Debate          — proposer(:8081) → reflector(:8082) → judge(:8083)
#   Night Verify          — verifier(:8084) final gate via review_consumer.py
#   Day Mode Restore      — inference container extractor(:8082) restore
#   Proxy Audit           — proxy_reviewer.py (DeepSeek Pro verify audit)

set -o pipefail

# ── PID Lock (single-instance guard) ────────────────────
NIGHT_CYCLE_LOCK="/tmp/devforge-night-cycle.lock"
exec 200>"$NIGHT_CYCLE_LOCK"
flock -n 200 || { echo "[$(LOG_TS)] night_cycle already running — exit"; exit 0; }

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
STATUS_FILE="/opt/projects/server/data/nightly_status.yaml"
MODE_FILE="/opt/ai_data/scripts/current-system-mode.env"
SCRIPTS_DIR="/opt/projects/server/scripts"
MODEL_CTL="$SCRIPTS_DIR/lib/model_ctl.sh"
if [ -f "$MODEL_CTL" ]; then
    source "$MODEL_CTL"
fi

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

switch_inference() {
    local model_key="$1"
    local port="${2:-$(_model_port "$model_key")}"
    echo "[$(LOG_TS)] Switching inference to $model_key (:$port)..."
    _ensure_model "$model_key" "$port" false 600 || {
        echo "[$(LOG_TS)] ERROR: failed to start inference as $model_key" >&2
        return 1
    }
    return 0
}

stop_llm_services() {
    local label="$1"
    echo "[$(LOG_TS)] [$label] Stopping LLM-adjacent services (slack/telegram in svc.pod — no-op)..."
    # slack/telegram-bot now run inside svc.pod — no need to stop
    # Other aux timers:
    for tmr in activity-summarizer-safety.timer; do
        systemctl --user stop "$tmr" 2>&1 || true
    done
    echo "[$(LOG_TS)] [$label] All non-critical service timers stopped"
}

start_llm_services() {
    local label="$1"
    echo "[$(LOG_TS)] [$label] Restarting service timers..."
    for tmr in activity-summarizer-safety.timer; do
        systemctl --user start "$tmr" 2>&1 || true
    done
    echo "[$(LOG_TS)] [$label] Service timers restored"
}

# --- Night mode activation ---
_set_mode night

# ── Server Validation ────────────────────────────
# Lightweight — runs before any mode switching to snapshot daytime state.

validation_ok=true
echo "[$(LOG_TS)] === Server Validation ==="
if python3 "$SCRIPTS_DIR/state_collector/main.py" --validate; then
    echo "[$(LOG_TS)] Server validation OK"
else
    validation_ok=false
    echo "[$(LOG_TS)] Server validation had issues (non-fatal)" >&2
fi

# ── Test Heartbeat Check (test running?) ──────────────────────────────
ACTIVE_TEST=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPTS_DIR')
from lib.db import psql_json
rows = psql_json(\"SELECT pulse_id FROM watchdog_pulses WHERE pulse_id LIKE 'heartbeat_test_%' AND status = 'IN_PROGRESS' LIMIT 1\")
if rows:
    print(rows[0]['pulse_id'])
" 2>/dev/null)
if [ -n "$ACTIVE_TEST" ]; then
    echo "[$(LOG_TS)] Test active ($ACTIVE_TEST) — skip night cycle"
    _set_mode day
    _restored=true
    exit 0
fi

# ── Night Debate ── (queue consumer) ──────────────
# night_cycle.py --queue handles its own container management
# (kill_all → sequential P→R→J model loading on inference container).
# Reads pending extract_results from activity_log.
# On success: queue_status → 'reviewed' (consumed by Night Verify).

review_ok=true

echo "[$(LOG_TS)] === Night Debate ==="
if ! python3 "$SCRIPTS_DIR/pipelines/night_cycle.py" --queue --limit 5; then
    review_ok=false
    echo "[$(LOG_TS)] night_cycle.py --queue FAILED" >&2
fi

# ── Night Verify (verifier :8084) ───────────────────

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
    echo "[$(LOG_TS)] Verify queue empty — skipping verifier verify"
else
    echo "[$(LOG_TS)] === Night Verify (verifier) ==="
    stop_llm_services "verify"
    sleep 3

    if switch_inference "verifier" && wait_for_model 8084 "verifier" 600; then
        retry "verify" 2 python3 "$SCRIPTS_DIR/pipelines/review_consumer.py" || verify_ok=false
    else
        echo "[$(LOG_TS)] Failed to start verify mode" >&2
        verify_ok=false
    fi
fi

# ── Day Mode Restore ───────────────────────────────

day_restored=true

echo "[$(LOG_TS)] === Night → Day transition ==="
if ! switch_inference "day-extractor"; then
    day_restored=false
    echo "[$(LOG_TS)] FATAL: switch_mode day failed" >&2
else
    start_llm_services "day-restore"
    # Inference extractor mode on :8082
    if ! wait_for_model 8082 "inference extractor (day)" 300; then
        day_restored=false
        echo "[$(LOG_TS)] FATAL: extractor (:8082) not responding after restore" >&2
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
echo "[$(LOG_TS)] == Daily structure sync =="
if systemctl --user start devforge-daily-structure.service 2>/dev/null; then
    echo "[$(LOG_TS)] Daily structure sync OK"
else
    echo "[$(LOG_TS)] Daily structure sync FAILED — KST 09:00 timer will retry" >&2
fi

# ── Proxy Audit (DeepSeek Pro) ──────────────────────

proxy_ok=true

echo "[$(LOG_TS)] === Proxy Audit (DeepSeek Pro) ==="
if python3 "$SCRIPTS_DIR/pipelines/proxy_reviewer.py" --limit 50; then
    echo "[$(LOG_TS)] DeepSeek Pro review OK"
else
    proxy_ok=false
    echo "[$(LOG_TS)] DeepSeek Pro review had issues (non-fatal)" >&2
fi

# ── Monthly FTS5 Rebuild (1st only) ─────────────────
# FTS5 incremental sync via turn_watcher covers daily updates.
# Monthly full rebuild is a safety net to recover from any drift.
if [ "$(date +%d)" = "01" ]; then
    echo "[$(LOG_TS)] === Monthly FTS5 Rebuild ==="
    python3 -c "
import sys; sys.path.insert(0, '$SCRIPTS_DIR')
from lib.search.local_index import FTS5Index
r = FTS5Index().rebuild()
print(f'FTS5 rebuild: {r[\"inserted\"]}/{r[\"total\"]} turns in {r[\"elapsed_s\"]}s')
" || echo "[$(LOG_TS)] FTS5 rebuild FAILED (non-fatal)" >&2
fi

# ── Status summary (consumed by 9 AM Slack hook) ──────────────
cat > "$STATUS_FILE" <<YAML
timestamp: "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
validation: $($validation_ok && echo ok || echo failed)
review: $($review_ok && echo ok || echo failed)
verify: $($verify_ok && echo ok || echo skipped)
day: $($day_restored && echo ok || echo failed)
YAML

echo "[$(LOG_TS)] night_cycle complete"

# ── Chain: trigger backup ──────────────────────────────────
echo "[$(LOG_TS)] triggering devforge-backup-safety.service..."
systemctl --user start devforge-backup-safety.service
