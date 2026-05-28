#!/bin/bash
# nightly_batch.sh — DevForge nightly pipeline (KST 01:00 / UTC 16:00)
# Light → heavy order. All output to journald via systemd service.
#
# Phase 1: link_turns          light — commit/turn links
# Phase 2: collect_turns       light — safety net for missed sessions
# Phase 3: state_collector     light — MOTD + state.yaml snapshot
# Phase 4: auto_mode           light — user tasks (review mode)
# Phase 5: review_worker       mid  — DeepSeek extract + 14B debate (:8080 + :8081)
# Phase 6: verify (32B)        heavy — final verdict, Pod A stopped
# Phase 7: restore debate      Pod A+B back to debate mode
# Phase 8: embed_turns         mid  — Gemini API (external)

set -o pipefail

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
STATUS_FILE="/opt/projects/server/data/nightly_status.yaml"
MODE_FILE="/opt/ai_data/scripts/current-mode.env"
SCRIPTS_DIR="/opt/projects/server/scripts"

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

switch_mode() {
    local mode="$1"
    echo "[$(LOG_TS)] Switching Pod B to $mode..."
    printf '%s' "MODE=$mode" > "${MODE_FILE}.tmp" && mv "${MODE_FILE}.tmp" "$MODE_FILE"
    systemctl --user restart container-devforge-swap 2>&1 || {
        echo "[$(LOG_TS)] ERROR: failed to restart container-devforge-swap" >&2
        return 1
    }
    return 0
}

# ── Phase 1-3: light jobs (no mode switching) ──────────────────

link_ok=true
collect_ok=true
state_ok=true

retry "link_turns" 3 python3 "$SCRIPTS_DIR/link_turns.py" || link_ok=false
retry "collect_turns" 2 python3 "$SCRIPTS_DIR/collect_turns.py" || collect_ok=false  # safety net, non-fatal
retry "state_collector" 2 python3 "$SCRIPTS_DIR/state_collector/main.py" || state_ok=false  # non-fatal

# ── Phase 4: auto_mode (review mode) ───────────────────────────

auto_mode_ok=true

auto_task_count=$(cd "$SCRIPTS_DIR" && python3 -c "
import sys, re
task_file = '/opt/projects/server/data/auto_tasks.md'
try:
    content = open(task_file).read()
    content_no_comments = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)
    tasks = re.findall(r'^## (.+)$', content_no_comments, re.MULTILINE)
    real_tasks = [t for t in tasks if not t.startswith('Example:')]
    print(len(real_tasks))
except Exception:
    print(0)
" 2>/dev/null)

if [ "${auto_task_count:-0}" -gt 0 ]; then
    echo "[$(LOG_TS)] Auto mode: $auto_task_count user task(s)"
    if bash "$SCRIPTS_DIR/auto_mode.sh"; then
        echo "[$(LOG_TS)] Auto mode complete"
    else
        echo "[$(LOG_TS)] Auto mode FAILED" >&2
        auto_mode_ok=false
    fi
else
    echo "[$(LOG_TS)] Auto mode: no user tasks — skipping"
fi

# ── Phase 5: review_worker (DeepSeek + 14B debate) ─────────────

review_ok=true

if switch_mode "review" && wait_for_model 8081 "Qwen-14B" 300; then
    echo "[$(LOG_TS)] === Review Phase: extract + 14B debate ==="
    if ! python3 "$SCRIPTS_DIR/review_worker.py"; then
        review_ok=false
        echo "[$(LOG_TS)] review_worker FAILED" >&2
    fi
else
    echo "[$(LOG_TS)] Failed to start review mode" >&2
    review_ok=false
fi

# ── Phase 6: 32B final verify ──────────────────────────────────

verify_ok=true

queue_count=$(cd "$SCRIPTS_DIR" && python3 -c "
from lib.db import psql
r = psql(\"SELECT COUNT(*) FROM activity_log WHERE queue_status='reviewed' AND type IN ('review','debate_result')\")
import sys
val = r.strip() if r else '0'
print(val if val else '0')
" 2>/dev/null)
echo "[$(LOG_TS)] Verify queue (status='reviewed'): $queue_count items"

if [ "$queue_count" = "0" ] || [ -z "$queue_count" ]; then
    echo "[$(LOG_TS)] Verify queue empty — skipping 32B verify"
else
    echo "[$(LOG_TS)] === Verify Phase: Qwen-32B final ==="
    echo "[$(LOG_TS)] Stopping Pod A (memory for 32B)..."
    systemctl --user stop container-devforge-qwen 2>&1 || true
    sleep 5

    if switch_mode "verify" && wait_for_model 8081 "Qwen-32B" 600; then
        retry "verify" 2 python3 "$SCRIPTS_DIR/review_consumer.py" || verify_ok=false
    else
        echo "[$(LOG_TS)] Failed to start verify mode" >&2
        verify_ok=false
    fi
fi

# ── Phase 7: restore debate mode ───────────────────────────────

debate_restored=true

echo "[$(LOG_TS)] === Restoring debate mode ==="
systemctl --user start container-devforge-qwen 2>&1 || true
sleep 5

if ! switch_mode "debate"; then
    debate_restored=false
    echo "[$(LOG_TS)] FATAL: switch_mode debate failed" >&2
else
    # All three models must be up for debate mode to be functional.
    if ! wait_for_model 8081 "Qwen3-4B (debate)" 60; then
        debate_restored=false
        echo "[$(LOG_TS)] FATAL: Qwen3-4B :8081 not responding after restore" >&2
    fi
    if ! wait_for_model 8082 "Phi-mini-MoE (debate)" 60; then
        debate_restored=false
        echo "[$(LOG_TS)] FATAL: Phi-mini-MoE :8082 not responding after restore" >&2
    fi
    if ! wait_for_model 8080 "DeepSeek-V2-Lite (Pod A)" 120; then
        debate_restored=false
        echo "[$(LOG_TS)] FATAL: DeepSeek :8080 not responding after restore" >&2
    fi
fi

if [ "$debate_restored" = "false" ]; then
    echo "[$(LOG_TS)] CRITICAL: debate restoration failed — daytime NL queries will be blocked" >&2
fi

# ── Phase 8: embed_turns (Gemini API) ──────────────────────────

embed_ok=true
set -a && source ~/.config/devforge/secrets.env && set +a
retry "embed_turns" 2 python3 "$SCRIPTS_DIR/embed_turns.py" || embed_ok=false

# ── Status summary (consumed by 9 AM Slack hook) ──────────────
cat > "$STATUS_FILE" <<YAML
timestamp: "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
link_turns: $($link_ok && echo ok || echo failed)
collect_turns: $($collect_ok && echo ok || echo failed)
state_collector: $($state_ok && echo ok || echo failed)
auto_mode: $($auto_mode_ok && echo ok || echo skipped)
review: $($review_ok && echo ok || echo failed)
verify: $($verify_ok && echo ok || echo failed)
debate_restored: $($debate_restored && echo ok || echo failed)
embed_turns: $($embed_ok && echo ok || echo failed)
YAML

echo "[$(LOG_TS)] nightly_batch complete"
