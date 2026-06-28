#!/bin/bash
# day_cycle.sh — async pipeline (pipeline_state-driven)
# pipeline_state flow: pending → batching → cleaned → embedded → scanned → extracted → enriched → verified
# Batch reservation at start: 10 pending → batching
# Each phase queries pipeline_state, each script self-reports completion via UPDATE.
# Light → Heavy execution order:
#   System Sync       — code-structure + duckdns + worklog
#   Pod A Health       — check :8080 (router mode, models loaded dynamically)
#   Text Preprocess   — text_clean.py (batching → cleaned, language-aware)
#   FTS5 Refresh      — local_index refresh
#   Day Embedding     — embed_batch.py (:8081, cleaned → embedded)
#   Day Entity Scan   — entity_scan.py (embedded → scanned, deterministic, regex+DB, no LLM)
#   Day Extract       — extract.py (:8082, scanned → extracted)
#   Day Enrich        — enrich.py (:8082, extracted → enriched)
#   Day Verify        — day_verify.py (:8082, enriched → verified)
# Each phase has its own budget check. Mid-cycle timeout carries forward in pipeline_state.
#
# Secrets: DUCKDNS_TOKEN in ~/.config/devforge/secrets.env
# Server philosophy: Slow but complete. Pod A router (:8080) loads reranker/tiny/polisher on demand.

set -o pipefail

MAX_CYCLE_SEC=21600
START_TS=$(date +%s)
LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
LOG() { echo "[$(LOG_TS)] $*"; }
BUDGET() { echo $(( MAX_CYCLE_SEC - ($(date +%s) - START_TS) )); }

# ── Budget gate: skip heavy phase if solo turns need more budget ────
# Pool (light) turns are always fast — only solo heavy turns are gated.
# Returns 0 (proceed) or 1 (skip).
_budget_gate() {
    local state="$1" cps="$2" overhead="$3"
    local budget_now solo_chars
    budget_now=$(BUDGET)
    [ "$budget_now" -lt 120 ] && return 1  # <2min → skip any heavy phase

    solo_chars=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
        "SELECT COALESCE(SUM(est_chars), 0) FROM turns WHERE pipeline_state = '$state' AND est_chars > 5000" 2>/dev/null || echo "0")
    solo_chars="${solo_chars:-0}"

    [ "$solo_chars" -le 0 ] && return 0  # no solo turns → always proceed

    local est=$(( solo_chars / cps + overhead ))
    [ "$est" -le 0 ] && est=60

    if [ "$budget_now" -lt "$est" ]; then
        # Allow partial process: if ≥600s, some pool + 1 solo fits
        if [ "$budget_now" -ge 600 ]; then
            LOG "  Budget gate: solo ~${est}s > ${budget_now}s, but ≥600s — partial OK"
            return 0
        fi
        LOG "  Budget gate: solo ~${est}s needed ≤ ${budget_now}s — deferring"
        return 1
    fi
    return 0
}

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

ensure_pod_b() {
    local target_mode="$1" model_key="$2" skip_probe="${3:-false}"
    local timeout="${4:-600}"

    # Port map (Pod B fixed ports)
    local port="8082"
    case "$model_key" in
        embed|embeder)      port=8081 ;;
        extractor|day|review-r|day-extractor|day-verifier) port=8082 ;;
        verify-enrich|judge|review-j|test-qwen|test-nextcoder) port=8083 ;;
        verifier|verify) port=8084 ;;
    esac

    local current_mode=""
    [ -f "$MODE_ENV" ] && current_mode=$(grep '^MODE=' "$MODE_ENV" | cut -d= -f2)
    if [ "$current_mode" = "$target_mode" ] && curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        LOG "  Pod B already $target_mode (:${port}) — skip restart"
        return 0
    fi

    # Check test heartbeat before restart
    if python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from lib.db import psql_json
rows = psql_json(\"SELECT pulse_id FROM watchdog_pulses WHERE pulse_id LIKE 'heartbeat_test_%' AND status = 'IN_PROGRESS' LIMIT 1\")
if rows:
    print(f'  Test active ({rows[0][\"pulse_id\"]}) — skip Pod B restart')
    sys.exit(0)
sys.exit(1)
" 2>&1; then
        LOG "  Pod B restart skipped (test heartbeat active)"
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

# ── In-flight check ─────────────────────────────────────────────────
IN_FLIGHT=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT count(*)::int FROM turns WHERE pipeline_state NOT IN ('pending', 'verified')" 2>/dev/null || echo "0")
IN_FLIGHT=${IN_FLIGHT:-0}

if [ "$IN_FLIGHT" -gt 0 ]; then
    LOG "In-flight turns: ${IN_FLIGHT} — resuming from pipeline_state"
elif [ "$IN_FLIGHT" -eq 0 ]; then
    # No in-flight — reserve 10 fresh from pending
    LOG "=== Batch reservation ==="
    BATCH_COUNT=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c "
      WITH batch AS (
        SELECT id FROM turns WHERE pipeline_state = 'pending'
        ORDER BY created_at ASC LIMIT 50
      ), upd AS (
        UPDATE turns SET pipeline_state = 'batching'
        FROM batch WHERE turns.id = batch.id
      )
      SELECT count(*)::text FROM batch
    " 2>/dev/null || echo "0")
    if [ "${BATCH_COUNT:-0}" -gt 0 ]; then
        LOG "Reserved ${BATCH_COUNT} turns (pending -> batching)"
    else
        LOG "No pending turns — cycle complete"
        exit 0
    fi
fi

# ── Pod A health check (only — router handles model switching dynamically) ──
LOG "=== Pod A: health check (:8080) ==="
curl -sf "http://127.0.0.1:8080/health" >/dev/null 2>&1 \
    && LOG "  Pod A (:8080) healthy" \
    || LOG "  Pod A (:8080) unhealthy — watchdog handles recovery"

# ── Text Preprocess (text_clean) ─────────────────────
NEED_CLEAN=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT count(*)::int FROM turns WHERE pipeline_state = 'batching'" 2>/dev/null || echo "0")
NEED_CLEAN=${NEED_CLEAN:-0}

if [ "$NEED_CLEAN" -gt 0 ]; then
    LOG "=== Text Preprocess (${NEED_CLEAN} batching turns) ==="
    python3 "$PIPELINE_DIR/text_clean.py" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    BUDGET=$(BUDGET)
    LOG "  Text Preprocess exit=$RC, elapsed=${ELAPSED}s"
    LOG "Budget=${BUDGET}s"
    [ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0
else
    LOG "=== Text Preprocess: skip (0 batching turns) ==="
fi

# ── est_chars update (post-clean, pre-heavy) ──
# est_chars is now set by text_clean.py via tiktoken.
# This is a compat fallback for turns processed before the merge.
LOG "=== est_chars update ==="
podman exec postgres psql -U devforge -d devforge_app -c "
  UPDATE turns SET est_chars = GREATEST(
    LENGTH(COALESCE(user_turn_clean, user_turn, '')),
    LENGTH(COALESCE(text_clean, text, '')),
    LENGTH(COALESCE(thinking_clean, thinking, ''))
  ) WHERE pipeline_state = 'cleaned' AND est_chars = 0" >/dev/null 2>&1

# ── FTS5 Refresh (text_clean 기준) ─────────
LOG "=== FTS5 Refresh ==="
python3 "$PIPELINE_DIR/fts5_refresh.py" 2>&1

# ── Day Embedding (:8081) ─────
NEED_EMBED=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT count(*)::int FROM turns WHERE pipeline_state IN ('cleaned', 'polished')" 2>/dev/null || echo "0")
NEED_EMBED=${NEED_EMBED:-0}

if [ "$NEED_EMBED" -gt 0 ]; then
    LOG "=== Day Embedding (${NEED_EMBED} cleaned turns) ==="
    ensure_pod_b "embeder" "embeder" false 1200
    python3 "$PIPELINE_DIR/embed_batch.py" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    LOG "  Embed exit=$RC, elapsed=${ELAPSED}s"
    [ $(BUDGET) -le 60 ] && LOG "Budget exhausted" && exit 0
else
    LOG "=== Day Embedding: skip (0 cleaned turns) ==="
fi

# ── Entity Scan (no LLM, no Pod B) ──
NEED_SCAN=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT count(*)::int FROM turns WHERE pipeline_state = 'embedded'" 2>/dev/null || echo "0")
if [ "$NEED_SCAN" -gt 0 ]; then
    LOG "=== Entity Scan (${NEED_SCAN} embedded turns) ==="
    python3 "$PIPELINE_DIR/entity_scan.py" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    BUDGET=$(BUDGET)
    LOG "  Entity scan exit=$RC, elapsed=${ELAPSED}s"
    LOG "Budget=${BUDGET}s"
    [ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0
fi

# ── Feedback Embedding (feedback_examples for pgvector NLI) ──
NEED_FEEDBACK_EMBED=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT COUNT(*) FROM feedback_examples fe LEFT JOIN embeddings e ON e.source_type='feedback_example' AND e.source_id=fe.id AND e.model_name='qwen3-embedding-8b-v1' WHERE e.id IS NULL" 2>/dev/null || echo "0")
NEED_FEEDBACK_EMBED=${NEED_FEEDBACK_EMBED:-0}
if [ "$NEED_FEEDBACK_EMBED" -gt 0 ]; then
    LOG "=== Feedback Embedding (${NEED_FEEDBACK_EMBED} unembedded feedback examples) ==="
    ensure_pod_b "embeder" "embeder" false 600
    python3 "$PIPELINE_DIR/embed_batch.py" --feedback 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    LOG "  Feedback embed exit=$RC, elapsed=${ELAPSED}s"
    [ $(BUDGET) -le 60 ] && LOG "Budget exhausted" && exit 0
else
    LOG "=== Feedback Embedding: skip (0 unembedded feedback examples) ==="
fi

# ── Day Extract (:8082) ──
NEED_EXTRACT=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT count(*)::int FROM turns WHERE pipeline_state = 'scanned'" 2>/dev/null || echo "0")
if [ "$NEED_EXTRACT" -gt 0 ]; then
    _budget_gate "scanned" 15 120 || { LOG "Budget insufficient for extract — deferring"; exit 0; }
    LOG "=== Day Extract (:8082, ${NEED_EXTRACT} scanned turns) ==="
    ensure_pod_b "day-extract" "$(_day_phase_model day_extract)" false 1200
    python3 "$PIPELINE_DIR/extract.py" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    BUDGET=$(BUDGET)
    [ $RC -eq 124 ] && LOG "  Extract timed out" || LOG "  Extract exit=$RC"
    LOG "Budget=${BUDGET}s"
    [ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0
fi

# ── Extract Fail Alert: failed/noise turns → Slack with classification buttons ──
if [ -f /var/tmp/extract_fail_report.json ]; then
    python3 "$SCRIPT_DIR/lib/slack_interactive.py" --send-extract-fail 2>&1 || true
fi

# ── Noise Marker 처리: 사용자 확인된 건 처리, 미확인은 Telegram ───
podman exec postgres psql -U devforge -d devforge_app -c "
UPDATE turns SET pipeline_state = 'verified'
FROM review_facts rf
WHERE rf.turn_id = turns.id
  AND rf.fact_type = 'noise_marker'
  AND rf.user_verdict = 'CONFIRM'
  AND turns.pipeline_state = 'scanned';
" 2>/dev/null

podman exec postgres psql -U devforge -d devforge_app -c "
DELETE FROM review_facts rf
USING turns
WHERE rf.turn_id = turns.id
  AND rf.fact_type = 'noise_marker'
  AND rf.user_verdict = 'REJECT'
  AND turns.pipeline_state = 'scanned';
" 2>/dev/null

NOISE_PENDING=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT COUNT(*) FROM review_facts WHERE fact_type='noise_marker' AND user_verdict IS NULL AND telegram_notified_at IS NULL" 2>/dev/null || echo "0")
if [ "${NOISE_PENDING:-0}" -gt 0 ]; then
    LOG "  ${NOISE_PENDING} noise markers - sending Slack"
    python3 "$SCRIPT_DIR/lib/slack_interactive.py" --send-noise-alert 2>&1 || true
fi

# ── NEUTRAL Auto-Resolve: GROUNDED/UNGROUNDED는 시스템 처리 ───
podman exec postgres psql -U devforge -d devforge_app -c "
UPDATE review_facts SET user_verdict = 'GROUNDED'
WHERE nli_llm = 'NEUTRAL' AND user_verdict IS NULL
  AND nli_verdict = 'GROUNDED'
  AND telegram_notified_at IS NULL
  AND created_at > now() - interval '24 hours'
  AND source = 'extract_pipeline';
" 2>/dev/null

podman exec postgres psql -U devforge -d devforge_app -c "
UPDATE review_facts SET user_verdict = 'UNGROUNDED'
WHERE nli_llm = 'NEUTRAL' AND user_verdict IS NULL
  AND nli_verdict = 'UNGROUNDED'
  AND telegram_notified_at IS NULL
  AND created_at > now() - interval '24 hours'
  AND source = 'extract_pipeline';
" 2>/dev/null

# ── NEUTRAL Gate: 정말 애매한 (AMBIGUOUS) 것만 Slack → stop cycle ──
NEUTRAL_AMB=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT COUNT(*) FROM review_facts WHERE source='extract_pipeline' AND nli_llm='NEUTRAL' AND user_verdict IS NULL AND nli_verdict='AMBIGUOUS' AND telegram_notified_at IS NULL" 2>/dev/null || echo "0")
if [ "${NEUTRAL_AMB:-0}" -gt 0 ]; then
    LOG "  ${NEUTRAL_AMB} NEUTRAL+AMBIGUOUS facts - Slack alert + exit"
    python3 "$SCRIPT_DIR/lib/slack_interactive.py" --send-alert 2>&1 || true
    exit 0
fi

# ── Reranker Recovery: re-score RERANKER_ERROR facts ──
NEED_RECOVER=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT count(*)::int FROM review_facts WHERE faithful_method = 'reranker_err'" 2>/dev/null || echo "0")
NEED_RECOVER=${NEED_RECOVER:-0}
if [ "$NEED_RECOVER" -gt 0 ]; then
    LOG "=== Reranker Recovery (${NEED_RECOVER} RERANKER_ERROR facts) ==="
    python3 "$PIPELINE_DIR/reranker_recover.py" 2>&1
    RC=$?
    if [ $RC -eq 1 ]; then
        LOG "  Reranker recover skipped (Pod A unhealthy)"
    else
        LOG "  Reranker recover exit=$RC"
    fi
    [ $(BUDGET) -le 60 ] && LOG "Budget exhausted" && exit 0
else
    LOG "=== Reranker Recovery: skip (0 RERANKER_ERROR facts) ==="
fi

# ── Day Enrich (:8082) ──
NEED_ENRICH=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT count(*)::int FROM turns WHERE pipeline_state = 'extracted'" 2>/dev/null || echo "0")
if [ "$NEED_ENRICH" -gt 0 ]; then
    _budget_gate "extracted" 20 60 || { LOG "Budget insufficient for enrich — deferring"; exit 0; }
    LOG "=== Day Enrich (:8082, ${NEED_ENRICH} extracted turns) ==="
    ensure_pod_b "day-enricher" "day-enricher" false 300
    python3 "$PIPELINE_DIR/enrich.py" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    BUDGET=$(BUDGET)
    [ $RC -eq 124 ] && LOG "  Enrich timed out" || LOG "  Enrich exit=$RC"
    LOG "Budget=${BUDGET}s"
    [ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0
fi

# ── Day Verify (:8082) ──
NEED_VERIFY=$(podman exec postgres psql -U devforge -d devforge_app -t -A -c \
  "SELECT count(*)::int FROM turns WHERE pipeline_state = 'enriched'" 2>/dev/null || echo "0")
if [ "$NEED_VERIFY" -gt 0 ]; then
    _budget_gate "enriched" 25 30 || { LOG "Budget insufficient for verify — deferring"; exit 0; }
    LOG "=== Day Verify (:8082, ${NEED_VERIFY} enriched turns) ==="
    ensure_pod_b "day-verifier" "day-verifier" false 300
    python3 "$PIPELINE_DIR/day_verify.py" 2>&1
    RC=$?
    ELAPSED=$(( $(date +%s) - START_TS ))
    BUDGET=$(BUDGET)
    [ $RC -eq 124 ] && LOG "  Verify timed out" || LOG "  Verify exit=$RC"
    LOG "Budget=${BUDGET}s"
    [ $BUDGET -le 60 ] && LOG "Budget exhausted" && exit 0
fi

TOTAL=$(( $(date +%s) - START_TS ))
LOG "day_cycle complete (${TOTAL}s)"
