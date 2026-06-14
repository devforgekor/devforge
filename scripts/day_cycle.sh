#!/bin/bash
# day_cycle.sh — hourly cycle (:00)
# Light → Heavy execution order
#
# Pipeline Steps:
#   System Sync      — code-structure + duckdns + worklog  (no Pod B needed)
#   Day Embedding    — embed_batch.py (8B f16 :8081) — only when new turns exist
#   Day Extract       — day_cycle.py (7B Q8 :8082) — extract → MCP enrich → py verify
#   Day Verify        — day_verify.py (14B :8083) — remaining budget
#
# Secrets: DUCKDNS_TOKEN in ~/.config/devforge/secrets.env
# New turns 0 → Embed skip → Extract skip → 55 min all verify
# Server philosophy: Slow but complete.

set -o pipefail

MAX_CYCLE_SEC=3300
START_TS=$(date +%s)
LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
LOG() { echo "[$(LOG_TS)] $*"; }
BUDGET() { echo $(( MAX_CYCLE_SEC - ($(date +%s) - START_TS) )); }

# Slack alert helper — same format as notifier.py send_alert()
_slack_alert() {
    local title="$1" detail="$2" color="${3:-danger}"
    local secrets_file="$HOME/.config/devforge/secrets.env"
    local token=""; local channel=""
    [ -f "$secrets_file" ] && . "$secrets_file"
    token="${SLACK_BOT_TOKEN:-}"; channel="${SLACK_CHANNEL:-U0APJGD8CBW}"
    [ -z "$token" ] && return 1
    local kst_now
    kst_now=$(TZ=Asia/Seoul date '+%m/%d %H:%M')

    python3 -c "
import json, sys
payload = {
    'channel': sys.argv[1],
    'text': f'[{sys.argv[2]}] {sys.argv[3]}',
    'attachments': [{
        'color': sys.argv[4],
        'blocks': [
            {'type': 'header', 'text': {'type': 'plain_text', 'text': sys.argv[3]}},
            {'type': 'section', 'text': {'type': 'mrkdwn', 'text': sys.argv[5]}},
            {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': f'{sys.argv[2]} KST — day_cycle watchdog check'}]},
        ],
    }],
}
print(json.dumps(payload))
" "$channel" "$kst_now" "$title" "$color" "$detail" \
    | curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -d @- -o /dev/null 2>/dev/null || true
}

SCRIPT_DIR="/opt/projects/server/scripts"
PIPELINE_DIR="$SCRIPT_DIR/pipelines"
MODE_ENV="/opt/ai_data/scripts/current-mode-pod-b.env"

LOG "day_cycle start"

# ── Helpers ──────────────────────────────────────────────────────────

ensure_pod_b() {
    local target_mode="$1" model_key="$2" skip_probe="${3:-false}"
    local timeout="${4:-600}"

    # Port map (Pod B fixed ports)
    local port="8082"  # default: extractor/reflector
    case "$model_key" in
        embed)      port=8081 ;;
        extractor|day|review-r) port=8082 ;;
        judge|review-j) port=8083 ;;
        verifier|verify) port=8084 ;;
    esac

    local current_mode=""
    [ -f "$MODE_ENV" ] && current_mode=$(grep '^MODE=' "$MODE_ENV" | cut -d= -f2)
    if [ "$current_mode" = "$target_mode" ] && curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        LOG "  Pod B already $target_mode (:${port}) — skip restart"
        return 0
    fi
    LOG "  Restarting Pod B → $target_mode (:${port})..."
    local probe_opt=""; [ "$skip_probe" = true ] && probe_opt=", skip_probe=True"
    if ! timeout "$timeout" python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from lib.pod_manager import start_pod_b
sys.exit(0 if start_pod_b('$model_key', $port$probe_opt) else 1)
" 2>&1; then
        LOG "  [warn] Pod B start failed ($target_mode) — continuing anyway"
        return 1
    fi
    return 0
}

# ── Night window guard ───────────────────────────────────────────────
if [ -f "/opt/ai_data/scripts/current-system-mode.env" ] && \
   grep -q "MODE=night" "/opt/ai_data/scripts/current-system-mode.env"; then
    LOG "day_cycle skipped (MODE=night)"
    exit 0
fi

# ── System Sync ────────────────────────────────────
LOG "=== System: code-structure ==="
if python3 "$SCRIPT_DIR/gen_architecture.py" --check-structure 2>&1; then
    LOG "  code-structure OK"
else
    LOG "  code-structure FAILED" >&2
fi

LOG "=== System: duckdns ==="
if curl -s -o /dev/null -w "%{http_code}" \
    "https://www.duckdns.org/update?domains=devforgekor&token=${DUCKDNS_TOKEN:-MISSING}&ip=&verbose=true" \
    2>/dev/null | grep -q 200; then
    LOG "  duckdns OK"
else
    LOG "  duckdns FAILED" >&2
fi


LOG "=== System: watchdog liveness ==="
LIVENESS_AGE=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
    "SELECT EXTRACT(EPOCH FROM (now() - liveness_ts))::int FROM watchdog_liveness WHERE component='watchdog_main'" 2>/dev/null || echo "0")
LIVENESS_AGE=${LIVENESS_AGE:-0}
if [ "$LIVENESS_AGE" -gt 900 ] 2>/dev/null; then
    LOG "  WATCHDOG STALE: ${LIVENESS_AGE}s since last liveness update"
    _slack_alert \
        "Watchdog Dead Man's Switch" \
        "watchdog_main last liveness ${LIVENESS_AGE}s ago. Run: systemctl --user status devforge-watchdog" \
        "danger"
else
    LOG "  watchdog OK (${LIVENESS_AGE}s ago)"
fi

LOG "=== System: worklog ==="
if timeout 240 python3 "$PIPELINE_DIR/worklog_generator.py" 2>&1; then
    LOG "  worklog OK"
else
    RC=$?
    [ $RC -eq 124 ] && LOG "  worklog TIMEOUT" || LOG "  worklog FAILED (exit=$RC)" >&2
fi

ELAPSED=$(( $(date +%s) - START_TS ))
BUDGET=$(BUDGET)
LOG "System sync done in ${ELAPSED}s — remaining budget=${BUDGET}s"
[ $BUDGET -le 120 ] && LOG "Budget exhausted" && exit 0

# ── Day Embedding (f16 batch) ───────────────────────────
NEED_EMBED=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT COUNT(*)::int FROM turns WHERE embedding_f16 IS NULL" 2>/dev/null || echo "0")
NEED_EMBED=${NEED_EMBED:-0}

if [ "$NEED_EMBED" -gt 0 ]; then
    LOG "=== Day Embedding: f16 (${NEED_EMBED} unembedded turns) ==="
    ensure_pod_b "embed" "embed" true 1200
    python3 "$PIPELINE_DIR/embed_batch.py" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    BUDGET=$(BUDGET)
    LOG "  Embed exit=$RC, elapsed=${ELAPSED}s"
    LOG "Budget=${BUDGET}s"
    [ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0
else
    LOG "=== Day Embedding: skip (0 unembedded turns) ==="
fi

# ── Day Extract (7B Q8) ───────────────────────────────
LOG "=== Day Extract: 7B Q8 (extract → MCP → py verify) ==="
ensure_pod_b "day" "extractor" false 600
BUDGET=$(BUDGET)
timeout -k 10 "$BUDGET" python3 "$PIPELINE_DIR/day_cycle.py" 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START_TS ))
BUDGET=$(BUDGET)
[ $RC -eq 124 ] && LOG "  Extract timed out" || LOG "  Extract exit=$RC"
LOG "Budget=${BUDGET}s"
[ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0

# ── Day Verify (14B) ───────────────────────────────
LOG "=== Day Verify: 14B (review-j mode :8083) ==="
ensure_pod_b "review-j" "judge" false 600
BUDGET=$(BUDGET)
timeout -k 10 "$BUDGET" python3 "$PIPELINE_DIR/day_verify.py" 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START_TS ))
[ $RC -eq 124 ] && LOG "  Verify timed out" || LOG "  Verify exit=$RC"

TOTAL=$(( $(date +%s) - START_TS ))
LOG "day_cycle complete (${TOTAL}s)"
