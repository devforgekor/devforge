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
# New turns 0 → Embed skip → Extract skip → 55 min all verify
# Server philosophy: Slow but complete.

set -o pipefail

MAX_CYCLE_SEC=3300
START_TS=$(date +%s)
LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
LOG() { echo "[$(LOG_TS)] $*"; }
BUDGET() { echo $(( MAX_CYCLE_SEC - ($(date +%s) - START_TS) )); }

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

# ── Phase 0: System (Fast, ~30s) ────────────────────────────────────
LOG "=== Phase 0/3: code-structure ==="
if python3 "$SCRIPT_DIR/gen_architecture.py" --check-structure 2>&1; then
    LOG "  code-structure OK"
else
    LOG "  code-structure FAILED" >&2
fi

LOG "=== Phase 0/3: duckdns ==="
if curl -s -o /dev/null -w "%{http_code}" \
    "https://www.duckdns.org/update?domains=devforgekor&token=776d9654-5af7-4814-8a8d-8f6183e5e2f7&ip=&verbose=true" \
    2>/dev/null | grep -q 200; then
    LOG "  duckdns OK"
else
    LOG "  duckdns FAILED" >&2
fi

LOG "=== Phase 0/3: worklog ==="
if timeout 240 python3 "$PIPELINE_DIR/worklog_generator.py" 2>&1; then
    LOG "  worklog OK"
else
    RC=$?
    [ $RC -eq 124 ] && LOG "  worklog TIMEOUT" || LOG "  worklog FAILED (exit=$RC)" >&2
fi

ELAPSED=$(( $(date +%s) - START_TS ))
BUDGET=$(BUDGET)
LOG "Phase 0 done in ${ELAPSED}s — remaining budget=${BUDGET}s"
[ $BUDGET -le 120 ] && LOG "Budget exhausted" && exit 0

# ── Phase 1: Embed (f16 batch, pre-check) ───────────────────────────
NEED_EMBED=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT COUNT(*)::int FROM turns WHERE embedding_f16 IS NULL" 2>/dev/null || echo "0")
NEED_EMBED=${NEED_EMBED:-0}

if [ "$NEED_EMBED" -gt 0 ]; then
    LOG "=== Phase 1/3: Embed (f16 — ${NEED_EMBED} unembedded turns) ==="
    ensure_pod_b "embed" "embed" true 1200
    timeout -k 10 "$BUDGET" python3 "$PIPELINE_DIR/embed_batch.py" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    BUDGET=$(BUDGET)
    [ $RC -eq 124 ] && LOG "  Embed timed out" || LOG "  Embed exit=$RC"
    LOG "Budget=${BUDGET}s"
    [ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0
else
    LOG "=== Phase 1/3: Embed (skip — 0 unembedded turns) ==="
fi

# ── Phase 2: Extract Chain (day mode) ───────────────────────────────
LOG "=== Phase 2/3: Extract (7B Q8 — extract → MCP → py verify) ==="
ensure_pod_b "day" "extractor" false 600
BUDGET=$(BUDGET)
timeout -k 10 "$BUDGET" python3 "$PIPELINE_DIR/day_cycle.py" 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START_TS ))
BUDGET=$(BUDGET)
[ $RC -eq 124 ] && LOG "  Extract timed out" || LOG "  Extract exit=$RC"
LOG "Budget=${BUDGET}s"
[ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0

# ── Phase 3: Verify (잔여 예산 전부) ───────────────────────────────
LOG "=== Phase 3/3: Verify (14B — review-j mode :8083) ==="
ensure_pod_b "review-j" "judge" false 600
BUDGET=$(BUDGET)
timeout -k 10 "$BUDGET" python3 "$PIPELINE_DIR/day_verify.py" 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START_TS ))
[ $RC -eq 124 ] && LOG "  Verify timed out" || LOG "  Verify exit=$RC"

TOTAL=$(( $(date +%s) - START_TS ))
LOG "day_cycle complete (${TOTAL}s)"
