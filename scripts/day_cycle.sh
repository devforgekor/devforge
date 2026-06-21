#!/bin/bash
# day_cycle.sh — hourly cycle (:00)
# Light → Heavy execution order
# Overall flow: watch=0 entry → text_clean → polish → fts5 → embed → entity_scan
#   → Pod B extract → enrich → swap → verify (cycle complete)
# Each phase is independent with its own budget check.
# Pipeline Steps:
#   System Sync       — code-structure + duckdns + worklog  (no Pod B needed)
#   Text Preprocess   — text_clean.py : text_clean (NFKC/공백/이모지 전처리)
#   Pod A Reranker    — ensure reranker :8080 is healthy
#   Day Polish        — polish_batch.py (Kiwi-only, --no-llm)
#   FTS5 Refresh      — local_index refresh (text_clean_polished)
#   Day Embedding     — embed_batch.py (:8081) — text_clean_polished
#   Day Entity Scan   — entity_scan.py (deterministic, regex+DB, no LLM)
#   Day Pipeline (Pod B, swap sequential)  — extract model :8082 extract+enrich → swap → verify model :8082 verify
#     → Entity Scan → Extract (with entity context) → Enrich → model swap → Verify
#
# Secrets: DUCKDNS_TOKEN in ~/.config/devforge/secrets.env
# Server philosophy: Slow but complete. Pod A reranker always on :8080.

set -o pipefail

MAX_CYCLE_SEC=3300
START_TS=$(date +%s)
LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
LOG() { echo "[$(LOG_TS)] $*"; }
BUDGET() { echo $(( MAX_CYCLE_SEC - ($(date +%s) - START_TS) )); }

# ── PID Lock (single-instance guard) ────────────────────
DAY_CYCLE_LOCK="/tmp/devforge-day-cycle.lock"
exec 200>"$DAY_CYCLE_LOCK"
flock -n 200 || { echo "[$(LOG_TS)] day_cycle already running — exit"; exit 0; }

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
MODE_ENV_A="/opt/ai_data/scripts/current-mode-pod-a.env"

LOG "day_cycle start"

# ── Helpers ──────────────────────────────────────────────────────────

# Resolve day cycle phase role to physical model key (SSOT: pod_manager.DAY_PHASE_MODELS)
_day_phase_model() {
    python3 -c "
import sys; sys.path.insert(0, '/opt/projects/server/scripts')
from lib.pod_manager import DAY_PHASE_MODELS
print(DAY_PHASE_MODELS['$1'])
"
}

ensure_dual_day() {
    local skip_probe="${1:-false}"
    local timeout="${2:-600}"

    # Check if both servers are already healthy
    local ok_8082=false; local ok_8083=false
    curl -sf "http://127.0.0.1:8082/health" >/dev/null 2>&1 && ok_8082=true
    curl -sf "http://127.0.0.1:8083/health" >/dev/null 2>&1 && ok_8083=true
    if $ok_8082 && $ok_8083; then
        LOG "  Pod B already dual-day (:8082 + :8083) — skip restart"
        return 0
    fi

    # Check protection before restart
    if python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from lib.protection import active_contexts
ctx = active_contexts()
tests = [c for c in ctx if c.startswith('test_')]
if tests:
    print(f'  [protect] test active: {tests[0]} — skip Pod B restart')
    sys.exit(0)
sys.exit(1)
" 2>&1; then
        LOG "  Pod B restart skipped (test protection active)"
        return 0
    fi

    LOG "  Starting Pod B in swap-day mode (day-extractor :8082 → day-verifier :8082)..."
    local probe_opt=""; [ "$skip_probe" = true ] && probe_opt=", skip_probe=True"
    if ! timeout "$timeout" python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from lib.pod_manager import ensure_dual_day
sys.exit(0 if ensure_dual_day() else 1)
" 2>&1; then
        LOG "  [warn] dual-day start failed — continuing anyway"
        return 1
    fi
    return 0
}

ensure_pod_b() {
    local target_mode="$1" model_key="$2" skip_probe="${3:-false}"
    local timeout="${4:-600}"

    # Port map (Pod B fixed ports)
    local port="8082"
    case "$model_key" in
        embed)      port=8081 ;;
        polish|extractor|day|review-r|day-extractor|day-verifier) port=8082 ;;
        verify-enrich|judge|review-j|test-qwen|test-nextcoder) port=8083 ;;
        verifier|verify) port=8084 ;;
    esac

    local current_mode=""
    [ -f "$MODE_ENV" ] && current_mode=$(grep '^MODE=' "$MODE_ENV" | cut -d= -f2)
    if [ "$current_mode" = "$target_mode" ] && curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        LOG "  Pod B already $target_mode (:${port}) — skip restart"
        return 0
    fi

    # Check protection before restart
    if python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from lib.protection import active_contexts
ctx = active_contexts()
tests = [c for c in ctx if c.startswith('test_')]
if tests:
    print(f'  [protect] test active: {tests[0]} — skip Pod B restart')
    sys.exit(0)
sys.exit(1)
" 2>&1; then
        LOG "  Pod B restart skipped (test protection active)"
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
if [ "$LIVENESS_AGE" -gt 3900 ] 2>/dev/null; then
    LOG "  WATCHDOG STALE: ${LIVENESS_AGE}s — no liveness for >1 cycle"
    _slack_alert \
        "Watchdog Dead Man's Switch" \
        "watchdog_main last liveness ${LIVENESS_AGE}s ago (threshold: 1 cycle=3900s). Run: systemctl --user status devforge-watchdog" \
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

# ── Protection Check (test pipelines active?) ────────────────
ACTIVE_PROTECT=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from lib.protection import active_contexts
ctx = active_contexts()
if ctx:
    print(' '.join(ctx))
" 2>/dev/null)
if [ -n "$ACTIVE_PROTECT" ]; then
    LOG "Protection active ($ACTIVE_PROTECT) — skip Pod B stages"
    exit 0
fi

# ── Pod A Reranker (Pod A reranker, :8080) — ensure always running ────────
LOG "=== Pod A: Reranker check ==="
if curl -sf "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
    LOG "  Pod A reranker (:8080) healthy"
else
    LOG "  Pod A reranker NOT healthy — restarting"
    python3 -c "
import sys; sys.path.insert(0, '"$SCRIPT_DIR"')
from lib.pod_manager import start_pod_a
sys.exit(0 if start_pod_a('reranker', 8080) else 1)
" 2>&1
fi
ELAPSED=$(( $(date +%s) - START_TS ))
LOG "Pod A check done in ${ELAPSED}s"

# ── Text Preprocess (text_clean) — first step after watch=0 write ─
NEED_CLEAN=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT COUNT(*)::int FROM turns WHERE text_clean IS NULL OR text_clean = ''" 2>/dev/null || echo "0")
NEED_CLEAN=${NEED_CLEAN:-0}

if [ "$NEED_CLEAN" -gt 0 ]; then
    LOG "=== Text Preprocess (${NEED_CLEAN} turns need text_clean) ==="
    python3 "$PIPELINE_DIR/text_clean.py" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    BUDGET=$(BUDGET)
    LOG "  Text Preprocess exit=$RC, elapsed=${ELAPSED}s"
    LOG "Budget=${BUDGET}s"
    [ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0
else
    LOG "=== Text Preprocess: skip (0 turns need text_clean) ==="
fi

# ── Day Polish (token-based batch limit) ──────────
NEED_POLISH=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT COUNT(*)::int FROM turns WHERE text_clean IS NOT NULL AND text_clean_polished IS NULL" 2>/dev/null || echo "0")
NEED_POLISH=${NEED_POLISH:-0}

if [ "$NEED_POLISH" -gt 0 ]; then
    LOG "=== Day Polish (Kiwi-only — ${NEED_POLISH} turns pending) ==="
    python3 "$PIPELINE_DIR/polish_batch.py" --no-llm 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    LOG "  Polish exit=$RC, elapsed=${ELAPSED}s"
    [ $(BUDGET) -le 60 ] && LOG "Budget exhausted" && exit 0
else
    LOG "=== Day Polish: skip (0 turns to polish) ==="
fi

# ── FTS5 Refresh ... (text_clean_polished 기준) ─────────
LOG "=== FTS5 Refresh ==="
python3 "$PIPELINE_DIR/fts5_refresh.py" 2>&1

# ── Day Embedding (token-based batch limit) ─────
NEED_EMBED=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT COUNT(*)::int FROM turns t LEFT JOIN embeddings e ON e.source_type='turn' AND e.source_id=t.id AND e.model_name='qwen3-embedding-8b-v1' WHERE e.id IS NULL" 2>/dev/null || echo "0")
NEED_EMBED=${NEED_EMBED:-0}

if [ "$NEED_EMBED" -gt 0 ]; then
    LOG "=== Day Embedding (${NEED_EMBED} unembedded turns) ==="
    ensure_pod_b "embed" "embed" true 1200
    python3 "$PIPELINE_DIR/embed_batch.py" --limit 10 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    LOG "  Embed exit=$RC, elapsed=${ELAPSED}s"
    [ $(BUDGET) -le 60 ] && LOG "Budget exhausted" && exit 0
else
    LOG "=== Day Embedding: skip (0 unembedded turns) ==="
fi

# ── Entity Scan (no LLM, no Pod B) ──
LOG "=== Entity Scan ==="
python3 "$PIPELINE_DIR/entity_scan.py" --limit 10 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START_TS ))
BUDGET=$(BUDGET)
LOG "  Entity scan exit=$RC, elapsed=${ELAPSED}s"
LOG "Budget=${BUDGET}s"
[ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0

# ── Day Pipeline: Extract → Enrich → swap → Verify ─
LOG "=== Day Extract + Enrich (:8082) ==="
ensure_pod_b "day-extract" "$(_day_phase_model day_extract)" true 1200
python3 "$PIPELINE_DIR/extract.py" --limit 10 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START_TS ))
BUDGET=$(BUDGET)
[ $RC -eq 124 ] && LOG "  Extract timed out" || LOG "  Extract exit=$RC"
LOG "Budget=${BUDGET}s"

python3 "$PIPELINE_DIR/enrich.py" --limit 10 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START_TS ))
BUDGET=$(BUDGET)
[ $RC -eq 124 ] && LOG "  Enrich timed out" || LOG "  Enrich exit=$RC"
LOG "Budget=${BUDGET}s"
[ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0

# Swap model: stop day-extractor, start day-verifier on :8082
LOG "=== Day Verify (:8082) — model swap ==="
ensure_pod_b "day-verify" "$(_day_phase_model day_verify)" true 1200
python3 "$PIPELINE_DIR/day_verify.py" --limit 10 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - START_TS ))
BUDGET=$(BUDGET)
[ $RC -eq 124 ] && LOG "  Verify timed out" || LOG "  Verify exit=$RC"
LOG "Budget=${BUDGET}s"
[ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0

TOTAL=$(( $(date +%s) - START_TS ))
LOG "day_cycle complete (${TOTAL}s)"
