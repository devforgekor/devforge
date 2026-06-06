#!/bin/bash
# 15m_cycle.sh — 30-min cycle with two phases
#   $1 = extract  (:00/:30) → DuckDNS + worklog + extract_pipeline
#   $1 = classify (:15/:45) → 주간 사전검토 P(day_p)→R(day_r)→J(day_j)
# Skips during nightly pipeline (MODE=night).

set -o pipefail

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
PHASE="${1:-extract}"
echo "[$(LOG_TS)] 15m_cycle phase=$PHASE"

# ── Night window guard ───────────────────────────────────────────────
MODE_FILE="/opt/ai_data/scripts/current-system-mode.env"
if [ -f "$MODE_FILE" ] && grep -q "MODE=night" "$MODE_FILE"; then
    echo "[$(LOG_TS)] 15m_cycle skipped (MODE=night)"
    exit 0
fi

case "$PHASE" in
    extract)
        # ── DuckDNS DDNS update ─────────────────────────────────────────
        if curl -s -o /dev/null -w "%{http_code}" \
            "https://www.duckdns.org/update?domains=devforgekor&token=776d9654-5af7-4814-8a8d-8f6183e5e2f7&ip=&verbose=true" \
            2>/dev/null | grep -q 200; then
            echo "[$(LOG_TS)] duckdns OK"
        else
            echo "[$(LOG_TS)] duckdns FAILED" >&2
        fi

        # ── Worklog auto-generation ─────────────────────────────────────
        if python3 /opt/projects/server/scripts/worklog_generator.py 2>&1; then
            echo "[$(LOG_TS)] worklog_generator OK"
        else
            echo "[$(LOG_TS)] worklog_generator FAILED" >&2
        fi

        # ── Fact extraction ────────────────────────────────────────────
        if python3 /opt/projects/server/scripts/extract_pipeline.py --limit 50 2>&1; then
            echo "[$(LOG_TS)] extract_pipeline OK"
        else
            echo "[$(LOG_TS)] extract_pipeline FAILED" >&2
        fi
        ;;

    classify)
        # ── Day pre-review (P->R->J via day_p/day_r/day_j) ────────────
        if python3 /opt/projects/server/scripts/classify_pipeline.py --limit 5 2>&1; then
            echo "[$(LOG_TS)] classify_pipeline OK"
        else
            echo "[$(LOG_TS)] classify_pipeline FAILED" >&2
        fi
        ;;
esac

echo "[$(LOG_TS)] 15m_cycle complete (phase=$PHASE)"