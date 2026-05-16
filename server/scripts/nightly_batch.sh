#!/bin/bash
# nightly_batch.sh — DevForge nightly pipeline (03:00 UTC)
# Light jobs first (retry 3x on failure), heavy jobs after.
# All output to journald via systemd service.

set -o pipefail

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
STATUS_FILE="/opt/projects/server/docs/nightly_status.yaml"

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

# ── Phase 1: light jobs ──────────────────────────────────────

link_ok=true
retry "link_turns" 3 python3 /opt/projects/server/scripts/link_turns.py || link_ok=false

# ── Phase 2: heavy jobs ───────────────────────────────────────
set -a && source ~/.config/devforge/secrets.env && set +a

embed_ok=true
retry "embed_turns" 2 python3 /opt/projects/server/scripts/embed_turns.py || embed_ok=false
# retry "classify_turns" 2 python3 /opt/projects/server/scripts/classify_turns.py || true

# ── Status summary (consumed by 9 AM Slack hook) ──────────────
cat > "$STATUS_FILE" <<YAML
timestamp: "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
link_turns: $($link_ok && echo ok || echo failed)
embed_turns: $($embed_ok && echo ok || echo failed)
# classify_turns: todo
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
