#!/bin/bash
# nightly_batch.sh — DevForge nightly pipeline (03:00 UTC)
# Phase 1: link_turns (light)
# Phase 2: review pipeline (14B→32B, stops/resumes debate containers)
# Phase 3: embed_turns (heavy, Gemini API)
# All output to journald via systemd service.

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
        sleep $((2 ** i))  # 2s, 4s, 8s backoff
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
    echo "MODE=$mode" > "$MODE_FILE"
    systemctl --user restart container-devforge-swap 2>&1 || {
        echo "[$(LOG_TS)] ERROR: failed to restart container-devforge-swap" >&2
        return 1
    }
    return 0
}

# ── Phase 1: light jobs ──────────────────────────────────────

link_ok=true
retry "link_turns" 3 python3 "$SCRIPTS_DIR/link_turns.py" || link_ok=false

# ── Phase 2: review pipeline (14B → 32B sequential) ──────────

review_14b_ok=true
review_32b_ok=true
debate_restored=true

queue_count=$(cd "$SCRIPTS_DIR" && python3 -c "
from lib.db import psql
r = psql(\"SELECT COUNT(*) FROM activity_log WHERE queue_status='unprocessed' AND type IN ('review','debate_result')\")
print(r.strip() if r else '0')
" 2>/dev/null)
echo "[$(LOG_TS)] Review queue: $queue_count items"

if [ "$queue_count" = "0" ] || [ -z "$queue_count" ]; then
    echo "[$(LOG_TS)] Review queue empty — skipping review pipeline"
else
    # Stage 1: Qwen-14B review (Pod A stays up, Pod B switches to review-14b)
    echo "[$(LOG_TS)] === Review Stage 1: Qwen-14B ==="
    if switch_mode "review-14b" && wait_for_model 8081 "Qwen-14B" 300; then
        retry "review_14b" 2 python3 "$SCRIPTS_DIR/review_consumer.py" --stage 14b || review_14b_ok=false
    else
        echo "[$(LOG_TS)] Failed to start review-14b mode" >&2
        review_14b_ok=false
    fi

    # Stage 2: Qwen-32B final verification (Pod A must stop first)
    echo "[$(LOG_TS)] === Review Stage 2: Qwen-32B ==="
    echo "[$(LOG_TS)] Stopping Pod A (needed for 32B memory)..."
    systemctl --user stop container-devforge-qwen 2>&1 || true
    sleep 5

    if switch_mode "review-32b" && wait_for_model 8081 "Qwen-32B" 600; then
        retry "review_32b" 2 python3 "$SCRIPTS_DIR/review_consumer.py" --stage 32b || review_32b_ok=false
    else
        echo "[$(LOG_TS)] Failed to start review-32b mode" >&2
        review_32b_ok=false
    fi

    # Restore: Pod A + Pod B back to debate mode
    echo "[$(LOG_TS)] === Restoring debate mode ==="
    systemctl --user start container-devforge-qwen 2>&1 || true
    sleep 5
    if switch_mode "debate"; then
        wait_for_model 8081 "Qwen3-4B (debate)" 60 || true
        wait_for_model 8082 "Phi-mini-MoE (debate)" 60 || true
        wait_for_model 8080 "DeepSeek-V2-Lite (Pod A)" 120 || true
    else
        debate_restored=false
        echo "[$(LOG_TS)] WARNING: failed to restore debate mode" >&2
    fi
fi

# ── Phase 3: heavy jobs ───────────────────────────────────────
set -a && source ~/.config/devforge/secrets.env && set +a

embed_ok=true
retry "embed_turns" 2 python3 "$SCRIPTS_DIR/embed_turns.py" || embed_ok=false

# ── Status summary (consumed by 9 AM Slack hook) ──────────────
cat > "$STATUS_FILE" <<YAML
timestamp: "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
link_turns: $($link_ok && echo ok || echo failed)
review_14b: $($review_14b_ok && echo ok || echo failed)
review_32b: $($review_32b_ok && echo ok || echo failed)
debate_restored: $($debate_restored && echo ok || echo failed)
embed_turns: $($embed_ok && echo ok || echo failed)
YAML

echo "[$(LOG_TS)] nightly_batch complete"

# ── End any lingering Claude Code session (triggers SessionEnd hooks) ──
CLAUDE_PID=$(pgrep -x claude 2>/dev/null || true)
if [ -n "$CLAUDE_PID" ]; then
    echo "[$(LOG_TS)] Ending Claude Code session (PID $CLAUDE_PID)"
    kill -TERM $CLAUDE_PID 2>/dev/null || true
    sleep 10  # wait for SessionEnd hooks
    echo "[$(LOG_TS)] Session ended"
fi
