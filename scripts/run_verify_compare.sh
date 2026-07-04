#!/bin/bash
# Background verify runner: Qwen Q8 → NextCoder Q8
set -e
LOG="/tmp/verify_compare.log"
exec > "$LOG" 2>&1

echo "[$(date)] Starting verify comparison..."

# 1. Wait for Qwen Q8 verify to finish (task bbdz5rzld)
echo "[$(date)] Waiting for Qwen Q8 verify..."
OUTPUT="/var/tmp/claude-1000/-home-opc/be1159fa-4067-4f8c-b967-d1e3596f958d/tasks/bbdz5rzld.output"
while true; do
    if [ -f "$OUTPUT" ] && grep -q "Day Verify complete" "$OUTPUT" 2>/dev/null; then
        echo "[$(date)] Qwen Q8 verify complete!"
        break
    fi
    sleep 30
done
sleep 30  # extra settle

# Record Qwen Q8 results
echo "[$(date)] Qwen Q8 DB results:"
podman exec postgres psql -U devforge -d devforge_app -c "
SELECT source, COUNT(*), ROUND(AVG(gen_rate)::numeric,2) as avg_tok_s
FROM review_facts WHERE fact_type='verify_result' AND source LIKE '%qwen_q8%'
GROUP BY source;"

# 2. Switch to NextCoder Q8
echo "[$(date)] Switching to NextCoder Q8..."
cd /opt/projects/server/scripts
python3 -c "
import sys
sys.path.insert(0, '.')
from lib.pod_manager import MODEL_METADATA
# Update test_14b_q8 to NextCoder
for k, v in list(MODEL_METADATA.items()):
    if k == 'test_14b_q8':
        v['file'] = 'NextCoder-14B-Q8_0.gguf'
        v['size'] = '15.0GB'
        break
else:
    MODEL_METADATA['test_14b_q8'] = {
        'file': 'NextCoder-14B-Q8_0.gguf',
        'size': '15.0GB', 'port': 8083, 'mode': 'test-q8',
        'model_name': 'test-nextcoder-q8', 'ctx': 8192, 'cache_ram': 1024,
        'evict_room': 16000, 'memory_check': 16000, 'memory_check_mode': 'warn',
    }
# Also persist — ONLY change test_14b_q8 entry (not reflector)
with open('lib/pod_manager.py') as f:
    lines = f.readlines()
new_lines = []
for line in lines:
    # Only replace the file/size inside test_14b_q8 block
    if '"test_14b_q8"' in line:
        in_block = True
        new_lines.append(line)
    elif in_block and line.strip().startswith('"file":') and 'Qwen2.5-Coder-14B-Instruct-Q8_0' in line:
        new_lines.append('        "file": "NextCoder-14B-Q8_0.gguf",\n')
    elif in_block and line.strip().startswith('"size":') and '15.7GB' in line:
        new_lines.append('        "size": "15.0GB",\n')
    elif in_block and '},' in line and len(line.strip()) <= 3:
        in_block = False
        new_lines.append(line)
    else:
        new_lines.append(line)
with open('lib/pod_manager.py', 'w') as f:
    f.writelines(new_lines)
print('pod_manager.py updated to NextCoder Q8')
"

# 3. Restart inference with NextCoder Q8
echo "[$(date)] Starting inference with NextCoder Q8..."
python3 -c "
import sys
sys.path.insert(0, '/opt/projects/server/scripts')
from lib.pod_manager import ensure_model
ok = ensure_model('test-q8')
print(f'START: {\"OK\" if ok else \"FAIL\"}')
" 2>&1

# 4. Run NextCoder Q8 verify
echo "[$(date)] Running NextCoder Q8 verify..."
cd /opt/projects/server/scripts/pipelines
python3 day_verify.py --limit 10 --model nextcoder_q8 2>&1
echo "[$(date)] NextCoder Q8 verify complete!"

# 5. Comparison report
echo ""
echo "========================================"
echo "COMPARISON RESULTS"
echo "========================================"
podman exec postgres psql -U devforge -d devforge_app -c "
SELECT source, COUNT(*) as total,
       ROUND(AVG(gen_rate)::numeric,2) as avg_tok_s,
       ROUND(AVG(elapsed_ms)::numeric,0) as avg_ms
FROM review_facts
WHERE fact_type='verify_result'
  AND (source LIKE '%qwen_q8%' OR source LIKE '%nextcoder_q8%')
GROUP BY source ORDER BY source;"
echo "[$(date)] All done!"
