#!/bin/bash
# day_extract.sh — Day extract cycle (:00)
# Chain: fast(code-struct → duckdns → worklog) → heavy(day_extract.py)
# Pod B (7B extractor :8082) only. MODE=night → skip.
set -o pipefail
MAX_CYCLE_SEC=1500  # 25분
START_TS=$(date +%s)
LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "[$(LOG_TS)] day_extract start"

# ── Night window guard ───────────────────────────────────────────────
MODE_FILE="/opt/ai_data/scripts/current-system-mode.env"
if [ -f "$MODE_FILE" ] && grep -q "MODE=night" "$MODE_FILE"; then
    echo "[$(LOG_TS)] day_extract skipped (MODE=night)"
    exit 0
fi

# ── Phase 1: Fast (항상 실행) ────────────────────────────────────────
echo "[$(LOG_TS)] === 1/3: code-structure ==="
if python3 /opt/projects/server/scripts/gen_architecture.py --check-structure 2>&1; then
    echo "[$(LOG_TS)] code-structure OK"
else
    echo "[$(LOG_TS)] code-structure FAILED" >&2
fi

echo "[$(LOG_TS)] === 2/3: duckdns ==="
if curl -s -o /dev/null -w "%{http_code}" \
    "https://www.duckdns.org/update?domains=devforgekor&token=776d9654-5af7-4814-8a8d-8f6183e5e2f7&ip=&verbose=true" \
    2>/dev/null | grep -q 200; then
    echo "[$(LOG_TS)] duckdns OK"
else
    echo "[$(LOG_TS)] duckdns FAILED" >&2
fi

echo "[$(LOG_TS)] === 3/3: worklog ==="
if timeout 240 python3 /opt/projects/server/scripts/pipelines/worklog_generator.py 2>&1; then
    echo "[$(LOG_TS)] worklog OK"
else
    RC=$?
    [ $RC -eq 124 ] && echo "[$(LOG_TS)] worklog TIMEOUT" || echo "[$(LOG_TS)] worklog FAILED (exit=$RC)" >&2
fi

# ── Phase 2: Heavy (남은 예산만큼 실행) ─────────────────────────────
ELAPSED=$(( $(date +%s) - START_TS ))
BUDGET=$(( MAX_CYCLE_SEC - ELAPSED ))
echo "[$(LOG_TS)] Fast done in ${ELAPSED}s — Heavy budget=${BUDGET}s"

if [ $BUDGET -le 60 ]; then
    echo "[$(LOG_TS)] Heavy skip — budget exhausted (need >60s, have ${BUDGET}s)"
else
    echo "[$(LOG_TS)] === Heavy: day_extract (extract → MCP enrich, Pod B :8082) ==="
    timeout -k 10 "$BUDGET" python3 /opt/projects/server/scripts/pipelines/day_extract.py 2>&1
    RC=$?
    if [ $RC -eq 124 ]; then
        echo "[$(LOG_TS)] Heavy timed out — checkpoint preserved, next term resumes"
    elif [ $RC -ne 0 ]; then
        echo "[$(LOG_TS)] Heavy failed (exit=$RC)"
    else
        echo "[$(LOG_TS)] Heavy OK"
    fi
fi

TOTAL=$(( $(date +%s) - START_TS ))
echo "[$(LOG_TS)] day_extract complete (${TOTAL}s)"
