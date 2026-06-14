#!/bin/bash
# DevForge Full Background Pipeline — Qwen Q8 → NextCoder Q8 verify → night_debate → PRJ
# Logs to /tmp/full_pipeline.log
set -e

LOG="/tmp/full_pipeline.log"
exec > "$LOG" 2>&1

ts() { echo "[$(date +'%H:%M:%S')]"; }

echo "$(ts) =============================================="
echo "$(ts) DevForge Full Background Pipeline START"
echo "$(ts) =============================================="

# =============================================================
# PHASE 0: Qwen Q8 results (check DB - was pre-started)
# =============================================================
echo "$(ts) PHASE 0: Qwen Q8 verify status..."
COUNT=$(podman exec postgres psql -U devforge -d devforge_app -tA -c "SELECT COUNT(*) FROM review_facts WHERE fact_type='verify_result' AND source='day_verify_qwen_q8';" 2>/dev/null || echo "0")
echo "$(ts)   Qwen Q8 DB rows: $COUNT"

podman exec postgres psql -U devforge -d devforge_app -c "
SELECT source, COUNT(*), ROUND(AVG(gen_rate)::numeric,2) as avg_tok_s,
       ROUND(AVG(elapsed_ms)::numeric,0) as avg_ms
FROM review_facts WHERE fact_type='verify_result' AND source='day_verify_qwen_q8'
GROUP BY source;"

# =============================================================
# PHASE 1: NextCoder Q8 Verify
# =============================================================
echo "$(ts) PHASE 1: NextCoder Q8 verify..."
cd /opt/projects/server/scripts

# Switch test_14b_q8 to NextCoder Q8 in pod_manager.py
python3 -c "
with open('lib/pod_manager.py') as f:
    lines = f.readlines()
new_lines, in_block = [], False
for line in lines:
    if '\"test_14b_q8\"' in line:
        in_block = True; new_lines.append(line)
    elif in_block and line.strip().startswith('\"file\":'):
        new_lines.append('        \"file\": \"NextCoder-14B-Q8_0.gguf\",\n')
    elif in_block and '},' in line and len(line.strip()) <= 3:
        in_block = False; new_lines.append(line)
    else:
        new_lines.append(line)
with open('lib/pod_manager.py', 'w') as f:
    f.writelines(new_lines)
print('test_14b_q8 -> NextCoder Q8')
"

python3 -c "
from lib.pod_manager import start_pod_b
ok = start_pod_b('test-q8', 8083)
print(f'NEXTCODER LOAD: {\"OK\" if ok else \"FAIL\"}')
exit(0 if ok else 1)
"

cd pipelines
python3 day_verify.py --limit 10 --model nextcoder_q8 2>&1
echo "$(ts) NextCoder Q8 verify complete!"

echo "$(ts) NextCoder Q8 results:"
podman exec postgres psql -U devforge -d devforge_app -c "
SELECT source, COUNT(*), ROUND(AVG(gen_rate)::numeric,2) as avg_tok_s,
       ROUND(AVG(elapsed_ms)::numeric,0) as avg_ms
FROM review_facts WHERE fact_type='verify_result' AND source='day_verify_nextcoder_q8'
GROUP BY source;"

echo "$(ts) === VERIFY COMPARISON ==="
podman exec postgres psql -U devforge -d devforge_app -c "
SELECT source, COUNT(*) as total, ROUND(AVG(gen_rate)::numeric,2) as avg_tok_s,
       ROUND(AVG(elapsed_ms)::numeric,0) as avg_ms
FROM review_facts WHERE fact_type='verify_result'
  AND source IN ('day_verify_qwen_q8','day_verify_nextcoder_q8')
GROUP BY source ORDER BY source;"

echo "$(ts) Memory:"
free -h

# =============================================================
# PHASE 2: night_debate with Q8 models
# =============================================================
echo "$(ts) PHASE 2: night_debate..."
cd /opt/projects/server/scripts/pipelines
python3 night_debate_pipeline.py 2>&1
echo "$(ts) night_debate complete!"

free -h

# =============================================================
# PHASE 3: PRJ test
# =============================================================
echo "$(ts) PHASE 3: PRJ test..."
cd /opt/projects/server/scripts/pipelines
python3 prj_cycle.py --limit 1 2>&1
echo "$(ts) PRJ test complete!"

free -h

# =============================================================
# FINAL
# =============================================================
echo "$(ts) =============================================="
echo "$(ts) FINAL REPORT"
echo "$(ts) =============================================="
podman exec postgres psql -U devforge -d devforge_app -c "
SELECT source, COUNT(*) as total, ROUND(AVG(gen_rate)::numeric,2) as avg_tok_s,
       ROUND(AVG(elapsed_ms)::numeric,0) as avg_ms
FROM review_facts WHERE fact_type='verify_result'
  AND source IN ('day_verify_qwen_q8','day_verify_nextcoder_q8')
GROUP BY source ORDER BY source;"
free -h
swapon --show
echo "$(ts) FULL PIPELINE COMPLETE"
