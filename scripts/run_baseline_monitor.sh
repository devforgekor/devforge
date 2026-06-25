#!/bin/bash
# DevForge Baseline Monitor v1.0
# Runs extract pipeline silently, saves results to files (token-saving mode)
set -e

BASE="/opt/projects/server"
EVAL_DIR="$BASE/data/eval"
TS=$(date -u +"%Y%m%dT%H%M%SZ")
RUN_FILE="$EVAL_DIR/baseline_run_${TS}.json"
SUM_FILE="$EVAL_DIR/baseline_summary_${TS}.yaml"

mkdir -p "$EVAL_DIR"

echo "[monitor] Starting extract pipeline baseline run..."
echo "[monitor] Run file: $RUN_FILE"
echo "[monitor] Summary: $SUM_FILE"

START_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Run pipeline, redirect all stdout to log
LOG=$(mktemp)
cd "$BASE"
python3 scripts/pipelines/extract.py --limit 50 --json > "$LOG" 2>&1 || true
PIPELINE_EXIT=$?

END_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Parse JSON result from last line
RESULT_JSON=$(tail -1 "$LOG" 2>/dev/null || echo '{"ok":false}')
echo "$RESULT_JSON" > "$RUN_FILE"

# Extract key metrics for YAML summary
PROCESSED=$(echo "$RESULT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('processed',0))" 2>/dev/null || echo "0")
FAILED=$(echo "$RESULT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('failed',0))" 2>/dev/null || echo "0")
FACTS=$(echo "$RESULT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('facts',0))" 2>/dev/null || echo "0")
ELAPSED=$(echo "$RESULT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('elapsed_s',0))" 2>/dev/null || echo "0")

# Query DB for per-turn timing stats
DB_STATS=$(podman exec postgres psql -U devforge -d devforge_app -t -A -F ',' \
  "SELECT COUNT(*) AS turns, COUNT(*) FILTER (WHERE rf.source='extract_marker') AS failures, COALESCE(AVG(rf.prompt_tokens),0)::int AS avg_prompt_tok, COALESCE(AVG(rf.gen_tokens),0)::int AS avg_gen_tok, COALESCE(AVG(rf.elapsed_ms),0)::int AS avg_elapsed_ms, MAX(rf.elapsed_ms)::int AS max_elapsed_ms FROM review_facts rf WHERE rf.created_at >= '${START_TS}'::timestamptz - interval '1 hour';" 2>/dev/null || echo "0,0,0,0,0,0")
IFS=',' read -r TURNS FAILURES AVG_PT AVG_GT AVG_EL MAX_EL <<< "$DB_STATS"

# Build YAML summary
cat > "$SUM_FILE" << YAML
baseline_run:
  timestamp: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
  kst: $(TZ='Asia/Seoul' date +"%Y-%m-%d %H:%M:%S KST")
  pipeline: pipelines/extract.py --limit 50
  status: $([ "$PIPELINE_EXIT" -eq 0 ] && echo "ok" || echo "error")

  summary:
    turns_attempted: 50
    turns_processed: ${PROCESSED}
    turns_failed: ${FAILED}
    total_facts: ${FACTS}
    elapsed_seconds: ${ELAPSED}

  per_turn_metrics:
    avg_prompt_tokens: ${AVG_PT}
    avg_gen_tokens: ${AVG_GT}
    avg_latency_ms: ${AVG_EL}
    max_latency_ms: ${MAX_EL}
    failure_count: ${FAILURES}

  files:
    run_log: baseline_run_${TS}.json
    summary: baseline_summary_${TS}.yaml
YAML

echo ""
echo "=== Baseline Run Complete ==="
echo "  Turns: ${PROCESSED} processed, ${FAILED} failed"
echo "  Facts: ${FACTS}"
echo "  Time:  ${ELAPSED}s"
echo "  Avg prompt tok: ${AVG_PT} | Avg gen tok: ${AVG_GT} | Avg latency: ${AVG_EL}ms"
echo ""
echo "  Files:"
echo "    $RUN_FILE"
echo "    $SUM_FILE"

# Cleanup
rm -f "$LOG"
