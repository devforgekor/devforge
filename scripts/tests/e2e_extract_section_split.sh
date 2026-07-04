#!/bin/bash
# E2E Pipeline Test — section-split extract
# Runs: embed_batch → entity_scan → extract → enrich → day_verify
# Auto-recovers 8082. Registers test protection. Saves snapshots per phase.
set +e  # DON'T exit on error — we handle errors ourselves
cd /opt/projects/server/scripts || exit 1

LIMIT=3
PARALLEL=1
TS=$(date -u +%Y%m%d_%H%M%S)
SNAPSHOT_DIR="data/eval"
REPORT="$SNAPSHOT_DIR/e2e_full_report_$TS.json"
mkdir -p "$SNAPSHOT_DIR"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

log "============================================================"
log "E2E PIPELINE TEST — section-split extract (BATCH_LIMIT=$LIMIT)"
log "============================================================"
log "Started at: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
log "KST: $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S')"
log ""

# Register protection
python3 -c "
import sys; sys.path.insert(0, '.')
from lib.test_common import test_setup
test_setup('e2e_extract_section_split', 'E2E test: section-split extract pipeline')
print('Protection registered.')
"

PY="python3 -B"
recover_8082() {
    log "[recovery] Reloading 8082..."
    $PY -c "
import sys; sys.path.insert(0, '.')
from lib.pod_manager import ensure_model
ensure_model('day-extractor', skip_if_healthy=False)
print('8082 ready')
" 2>&1 || true
}
heartbeat() {
    $PY -c "
import sys; sys.path.insert(0, '.')
from lib.test_common import test_heartbeat
test_heartbeat('$1')
" 2>/dev/null || true
}

# ═══════════════════════════════════════════════════════════════
# Phase 1: embed_batch (SKIPPED — 8081 requires inference mode switch)
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 1/5: embed_batch (SKIPPED — 8081 down)"
log "============================================================"
$PY -c "
import sys; sys.path.insert(0, '.')
import json
with open('$SNAPSHOT_DIR/e2e_embed_batch_$TS.json', 'w') as f:
    json.dump({'skipped': True, 'reason': '8081 requires separate inference mode'}, f, indent=2)
print('  embed_batch skipped')
"

heartbeat "phase:entity_scan"

# ═══════════════════════════════════════════════════════════════
# Phase 2: entity_scan
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 2/5: entity_scan"
log "============================================================"
t0=$(date +%s)
$PY pipelines/entity_scan.py --limit=$LIMIT 2>&1
rc=$?
elapsed=$(($(date +%s) - t0))
log "entity_scan exit=$rc elapsed=${elapsed}s"

$PY -c "
import sys; sys.path.insert(0, '.')
import json
with open('$SNAPSHOT_DIR/e2e_entity_scan_$TS.json', 'w') as f:
    json.dump({'elapsed_s': $elapsed}, f, indent=2)
print('  snapshot saved')
"

heartbeat "phase:extract"

# ═══════════════════════════════════════════════════════════════
# Phase 3: extract (section-split)
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 3/5: extract — section-split (the main event)"
log "============================================================"
extract_ok=false
for attempt in 1 2 3; do
    t0=$(date +%s)
    log "  extract attempt $attempt/3..."
    $PY pipelines/extract.py --limit=$LIMIT --parallel=$PARALLEL 2>&1
    rc=$?
    elapsed=$(($(date +%s) - t0))

    # Check actual facts extracted (exit code 0 doesn't guarantee facts)
    fact_count=$($PY -c "
import sys; sys.path.insert(0, '.')
from lib.db import psql_json
rows = psql_json(
    'SELECT count(*) AS cnt FROM review_facts '
    'WHERE source = \'extract_pipeline\' '
    'AND created_at > now() - interval \'2 hours\''
) or [{'cnt': 0}]
print(rows[0]['cnt'])
" 2>/dev/null || echo "0")

    if [ "$fact_count" -gt 0 ] 2>/dev/null; then
        log "extract OK — ${fact_count} facts in ${elapsed}s"
        extract_ok=true
        break
    else
        log "[WARN] extract attempt $attempt: rc=$rc, facts=$fact_count after ${elapsed}s"
        if [ $attempt -lt 3 ]; then
            recover_8082
            heartbeat "extract retry $attempt"
        fi
    fi
done

if [ "$extract_ok" = false ]; then
    log "[FATAL] extract failed 3 attempts"
    $PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.test_common import test_complete
test_complete('extract_failed')
d = {'meta': {'status': 'extract_failed'}, 'snapshots': {}}
with open('$REPORT', 'w') as f: json.dump(d, f, indent=2)
print('partial report saved')
"
    exit 1
fi

$PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.db import psql_json
rows = psql_json(
    'SELECT fact_type, count(*) AS cnt FROM review_facts '
    'WHERE source = \'extract_pipeline\' '
    'AND created_at > now() - interval \'2 hours\' '
    'GROUP BY fact_type ORDER BY fact_type'
) or []
by_type = {r['fact_type']: r['cnt'] for r in rows}
total = sum(by_type.values())
with open('$SNAPSHOT_DIR/e2e_extract_$TS.json', 'w') as f:
    json.dump({'by_type': by_type, 'total': total, 'elapsed_s': $elapsed}, f, indent=2)
print(f'  extracted {total} facts: {by_type}')
"

heartbeat "phase:enrich"

# ═══════════════════════════════════════════════════════════════
# Phase 4: enrich
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 4/5: enrich (NLI + TLDR)"
log "============================================================"
for attempt in 1 2 3; do
    t0=$(date +%s)
    log "  enrich attempt $attempt/3..."
    $PY pipelines/enrich.py --limit=$LIMIT 2>&1
    rc=$?
    elapsed=$(($(date +%s) - t0))
    if [ $rc -eq 0 ]; then
        log "enrich OK in ${elapsed}s"
        break
    else
        log "[WARN] enrich attempt $attempt failed (rc=$rc) after ${elapsed}s"
        if [ $attempt -lt 3 ]; then
            recover_8082
            heartbeat "enrich retry $attempt"
        fi
    fi
done

$PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.db import psql_json
rows = psql_json(
    'SELECT COALESCE(nli_verdict, \\'pending\\') AS verdict, count(*) AS cnt FROM review_facts '
    'WHERE source = \'enrich\' '
    'AND created_at > now() - interval \'2 hours\' '
    'GROUP BY COALESCE(nli_verdict, \\'pending\\')'
) or []
by_result = {r['verdict']: r['cnt'] for r in rows}
total = sum(by_result.values())
with open('$SNAPSHOT_DIR/e2e_enrich_$TS.json', 'w') as f:
    json.dump({'by_result': by_result, 'total': total, 'elapsed_s': $elapsed}, f, indent=2)
print(f'  enriched {total} facts: {by_result}')
"

heartbeat "phase:day_verify"

# ═══════════════════════════════════════════════════════════════
# Phase 5: day_verify
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 5/5: day_verify"
log "============================================================"
t0=$(date +%s)
$PY pipelines/day_verify.py --limit=$LIMIT 2>&1
rc=$?
elapsed=$(($(date +%s) - t0))
log "day_verify exit=$rc elapsed=${elapsed}s"

$PY -c "
import sys; sys.path.insert(0, '.')
import json
with open('$SNAPSHOT_DIR/e2e_day_verify_$TS.json', 'w') as f:
    json.dump({'elapsed_s': $elapsed}, f, indent=2)
print('  snapshot saved')
"

# ═══════════════════════════════════════════════════════════════
# Full Report
# ═══════════════════════════════════════════════════════════════
log ""
log "============================================================"
log "E2E TEST COMPLETE"
log "============================================================"

$PY -c "
import sys; sys.path.insert(0, '.')
import json, glob
snapshots = {}
for f in sorted(glob.glob('$SNAPSHOT_DIR/e2e_*_$TS.json')):
    key = f.replace('$SNAPSHOT_DIR/e2e_', '').replace('_$TS.json', '')
    with open(f) as fh:
        try: snapshots[key] = json.load(fh)
        except: snapshots[key] = {'error': 'parse failed'}
report = {
    'meta': {
        'timestamp': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
        'batch_limit': $LIMIT,
        'test': 'E2E section-split extract pipeline',
        'watchdog': 'active',
        'model': 'Qwen3-8B-Q8_0 (:8082)',
        'kst': \"$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S')\",
    },
    'snapshots': snapshots,
}
with open('$REPORT', 'w') as f:
    json.dump(report, f, indent=2, default=str)
print(f'Full report: $REPORT')
"

# Cleanup
$PY -c "
import sys; sys.path.insert(0, '.')
from lib.test_common import test_complete
test_complete('E2E completed')
print('Test protection cleaned up.')
"

log "Done at: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
log "KST: $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S')"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Snapshots saved to: $SNAPSHOT_DIR/e2e_*_$TS.json"
echo "Full report:       $REPORT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
