#!/bin/bash
# DSV2 Lite R 역할 테스트 (Qwen14B R 결과와 비교)
set -e

P_EXP="/opt/projects/server/data/experiment/exp_p_rot1_r1_norubric_r1_norubric.json"
R_EXP_Q14="/opt/projects/server/data/experiment/exp_r_rot1_r1_norubric_r1_norubric.json"
EXPER_DIR="/opt/projects/server/data/experiment"
MODE_FILE_B="/opt/ai_data/scripts/current-mode-pod-b.env"
TIMESTAMP=$(date -u +%H:%M:%S)
mkdir -p "$EXPER_DIR"

log() { echo "[$TIMESTAMP] $*"; }

# Read P findings
P_FINDINGS=$(python3 -c "
import json
with open('$P_EXP') as f:
    data = json.load(f)
findings = data.get('result', {}).get('findings', [])
print(json.dumps(findings))
")
P_COUNT=$(echo "$P_FINDINGS" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
log "P findings: $P_COUNT 개"

# Read Qwen14B R results for comparison
R_Q14=$(python3 -c "
import json
with open('$R_EXP_Q14') as f:
    data = json.load(f)
verdicts = data.get('result', {}).get('verdicts', [])
accepts = [v['id'] for v in verdicts if v['verdict']=='accept']
rejects = [v['id'] for v in verdicts if v['verdict']=='reject']
print(json.dumps({'accepts': accepts, 'rejects': rejects, 'total': len(verdicts)}))
")
log "Qwen14B R results: $(echo "$R_Q14" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(f"{len(d[\"accepts\"])} accept, {len(d[\"rejects\"])} reject")')"

# Kill all containers
log "Podman stop all..."
systemctl --user stop devforge-swap 2>/dev/null || true
sleep 3

# Start DSV2 Lite on Pod B
log "Loading DSV2 Lite Q8.0..."
echo "MODE=review-dsv2" > "$MODE_FILE_B"
systemctl --user start devforge-swap 2>/dev/null || true

# Wait for health
RETRIES=120
for i in $(seq 1 $RETRIES); do
    if curl -sf http://127.0.0.1:8080/v1/chat/completions \
         -H "Content-Type: application/json" \
         -d '{"messages":[{"role":"user","content":"ping"}],"max_tokens":1,"stream":false}' > /dev/null 2>&1; then

        # Check actual response
        RESP=$(curl -sf http://127.0.0.1:8080/v1/chat/completions \
              -H "Content-Type: application/json" \
              -d '{"messages":[{"role":"user","content":"Say pong"}],"max_tokens":10,"stream":false}' 2>/dev/null)
        CONTENT=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'][:50])" 2>/dev/null)
        log "DSV2 Lite ready! (content: $CONTENT)"
        break
    fi
    sleep 5
    if [ $((i % 12)) -eq 0 ]; then
        log "  waiting... ($((i*5))s)"
    fi
done

# Run R role inference using call_llm
log "Running DSV2 Lite as R..."
OUTPUT_FILE="$EXPER_DIR/exp_r_dsv2_comparison.json"
python3 -c "
import json, sys
sys.path.insert(0, '/opt/projects/server/scripts')
from lib.llm_client import call_llm

P_EXP = '$P_EXP'
with open(P_EXP) as f:
    p_data = json.load(f)
findings = p_data.get('result', {}).get('findings', [])

SYS_R = '''You are a review reflector. For each finding submitted by the Proposer, decide ACCEPT or REJECT. Be precise -- if the finding is valid, ACCEPT it. If it is not a real issue or duplicates another, REJECT it.

Return JSON:
{
  \"verdicts\": [
    {\"id\": \"F001\", \"verdict\": \"accept\", \"reason\": \"concise justification\"},
    {\"id\": \"F002\", \"verdict\": \"reject\", \"reason\": \"concise justification\"}
  ]
}'''

user_msg = f'Proposer findings:\n{json.dumps(findings, ensure_ascii=False, indent=2)[:4000]}'
messages = [
    {'role': 'system', 'content': SYS_R},
    {'role': 'user', 'content': user_msg}
]

result = call_llm(messages, model='DeepSeekV2Lite', max_tokens=2048, timeout=900, return_meta=True)
print(json.dumps(result, ensure_ascii=False, indent=2))
" 2>&1 | tee "$OUTPUT_FILE"

log "DSV2 Lite R 결과 저장: $OUTPUT_FILE"

# Compare with Qwen14B
log "
===== R 역할 비교 ====="
python3 -c "
import json

with open('$OUTPUT_FILE') as f:
    dsv2_data = json.load(f)
dsv2_content = dsv2_data.get('content', '{}')
dsv2 = json.loads(dsv2_content) if isinstance(dsv2_content, str) else dsv2_content
dsv2_verdicts = dsv2.get('result', dsv2).get('verdicts', dsv2.get('verdicts', []))
if not dsv2_verdicts:
    dsv2_verdicts = dsv2.get('verdicts', [])

with open('$R_EXP_Q14') as f:
    q14_data = json.load(f)
q14_verdicts = q14_data.get('result', {}).get('verdicts', [])

dsv2_acc = {v['id']: v for v in dsv2_verdicts if v.get('verdict') == 'accept'}
dsv2_rej = {v['id']: v for v in dsv2_verdicts if v.get('verdict') == 'reject'}
q14_acc = {v['id']: v for v in q14_verdicts if v.get('verdict') == 'accept'}
q14_rej = {v['id']: v for v in q14_verdicts if v.get('verdict') == 'reject'}

all_ids = sorted(set(list(dsv2_acc.keys()) + list(dsv2_rej.keys()) + list(q14_acc.keys()) + list(q14_rej.keys())))

agreed = []
disagreed = []
for fid in all_ids:
    dsv2_v = 'accept' if fid in dsv2_acc else 'reject'
    q14_v = 'accept' if fid in q14_acc else 'reject'
    if dsv2_v == q14_v:
        agreed.append(fid)
    else:
        disagreed.append({'id': fid, 'dsv2': dsv2_v, 'q14': q14_v})

print(f'DSV2 Lite:  {len(dsv2_acc)} accept, {len(dsv2_rej)} reject ({len(dsv2_verdicts)} total)')
print(f'Qwen14B:    {len(q14_acc)} accept, {len(q14_rej)} reject ({len(q14_verdicts)} total)')
print(f'일치:       {len(agreed)}/{len(all_ids)} ({len(agreed)*100//len(all_ids)}%)')
print(f'불일치:     {len(disagreed)}/{len(all_ids)}')
if disagreed:
    print()
    print('=== 불일치 findings ===')
    for d in disagreed:
        print(f'  {d[\"id\"]}: DSV2={d[\"dsv2\"]} vs Qwen14B={d[\"q14\"]}')

# Timing comparison
dsv2_elapsed = dsv2_data.get('elapsed_ms', 0)
q14_elapsed = q14_data.get('elapsed_ms', 0)
if dsv2_elapsed and q14_elapsed:
    print()
    print(f'DSV2 Lite:  {dsv2_elapsed/1000:.1f}s')
    print(f'Qwen14B:    {q14_elapsed/1000:.1f}s')
    print(f'비율:       {dsv2_elapsed/q14_elapsed:.1f}x')

dsv2_usage = dsv2_data.get('usage', {})
q14_usage = q14_data.get('usage', {})
if dsv2_usage and q14_usage:
    print(f'DSV2 tokens: {dsv2_usage.get(\"total_tokens\",\"?\")} ({dsv2_usage.get(\"completion_tokens\",\"?\")} gen)')
    print(f'Qwen14B tokens: {q14_usage.get(\"total_tokens\",\"?\")} ({q14_usage.get(\"completion_tokens\",\"?\")} gen)')
"
