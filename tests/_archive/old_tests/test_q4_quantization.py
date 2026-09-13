#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_q4_quantization.py — pytest
"""30B Q3_K_M vs Q4_K_M Proposer comparison.
Loads the original pipeline input, runs 30B Q4_K_M as P, compares with saved Q3 results.

Steps:
1. Start 30B Q4_K_M (review-p mode, same port 8080)
2. Feed the original pipeline input findings
3. Compare output with saved exp_p_r1_norubric.json (Q3 results)

Usage: python3 test_q4_quantization.py"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, '/opt/projects/server/scripts')

from lib.llm_client import MODEL_REGISTRY, call_llm
from pipelines.prj_cycle import (
    MODE_FILE_B,
    PROPOSER_SYSTEM_PROMPT,
    RUBRIC,
    kill_all,
    wait_health,
    wait_probe,
)
from pipelines.prj_cycle import log as plog

EXPER_DIR = '/opt/projects/server/data/experiment'

# ── Pick Q4_K_M GGUF source ──
# byteshape has Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf (~18.6GB)
# But we don't have it yet — this script prepares the test structure
# and will be run AFTER download completes.

Q4_MODEL = "proposer"  # re-use key, port 8080
Q4_MODE = "review-p"
Q4_FILE = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
Q3_RESULT_PATH = f'{EXPER_DIR}/exp_p_r1_norubric_r1_norubric.json'

# Ensure MODEL_REGISTRY has Qwen30B
assert Q4_MODEL in MODEL_REGISTRY, "Qwen30B not in MODEL_REGISTRY"

# ── Load Q3 baseline ──
print(f'Loading Q3 baseline: {Q3_RESULT_PATH}')
with open(Q3_RESULT_PATH) as f:
    q3_data = json.load(f)
q3_result = q3_data.get('result', {})
q3_findings = q3_result.get('findings', [])
print(f'  Q3 findings: {len(q3_findings)}')

# ── Load original pipeline input ──
INPUT_PATH = '/opt/projects/server/data/experiment/pipeline_state_r1_norubric.json'
with open(INPUT_PATH) as f:
    state = json.load(f)
pipeline_input = state['input']
input_findings = pipeline_input.get('findings', [])
severity_dist = pipeline_input.get('severity_distribution', {})

print(f'  Pipeline input findings: {len(input_findings)}')
print(f'  Severity: {severity_dist}')

# Build context (same format as prj_cycle build_context("prj_p"))
context_parts = [
    "=== PIPELINE CONTEXT ===\n",
    f"Total findings: {len(input_findings)}",
    f"Severity distribution: {severity_dist}",
    f"\n=== FINDINGS ({len(input_findings)}) ===",
]
for f in input_findings:
    context_parts.append(
        f"  {f.get('id','?')} [{f.get('severity','?')}/{f.get('category','?')}]: "
        f"{f.get('description','')[:200]}"
    )
context_parts.append(f"\nSource files: {pipeline_input.get('source_files', {})}")
CONTEXT = '\n'.join(context_parts)

PROPOSER_PROMPT = PROPOSER_SYSTEM_PROMPT + f'\n\n{RUBRIC}'

def start_model():
    plog("  Starting 30B Q4_K_M...")
    with open(MODE_FILE_B, 'w') as f:
        f.write(f'MODE={Q4_MODE}')
    kill_all()
    subprocess.run(['systemctl', '--user', 'start', 'container-devforge-swap.service'],
                   capture_output=True, timeout=60)
    ok = wait_health(8080)
    if ok:
        plog("  :8080 health OK")
        ok = wait_probe(8080, Q4_MODE, timeout=600)
    if ok:
        plog("  :8080 ready")
    else:
        plog("  ❌ Failed to start 30B Q4_K_M")
    return ok

def stop_model():
    kill_all()
    time.sleep(2)

def extract_json(content):
    s = content.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1: s = s[first_nl + 1:]
        if s.endswith("```"): s = s[:-3].rstrip()
    return json.loads(s)

# ── Run and compare ──
print(f'\n{"="*60}')
print('30B Q4_K_M vs Q3_K_M Proposer Comparison')
print(f'{"="*60}')
print(f'File: /opt/ai_data/models/gguf/{Q4_FILE}')
print(f'Exists: {os.path.exists(f"/opt/ai_data/models/gguf/{Q4_FILE}")}')

model_path = f'/opt/ai_data/models/gguf/{Q4_FILE}'
if not os.path.exists(model_path):
    print('\n⚠️  Q4 file not found. Script prepared but cannot run.')
    print('   Download: hugggingface-cli download byteshape/... or')
    print(f'   curl -Lo {model_path} <url>')
    print('\nWhen ready, re-run: python3 test_q4_quantization.py')
    sys.exit(0)

if not start_model():
    sys.exit(1)

print('\n  Calling 30B Q4_K_M as Proposer...')
t0 = time.monotonic()
try:
    for use_json in [True, False]:
        try:
            r = call_llm(
                [{"role": "system", "content": PROPOSER_PROMPT},
                 {"role": "user", "content": CONTEXT}],
                model=Q4_MODEL, max_tokens=4096, temperature=0.1,
                timeout=7200, json_mode=use_json, return_meta=True)
            content = r["content"]
            q4_result = extract_json(content)
            elapsed = time.monotonic() - t0
            q4_findings = q4_result.get('findings', [])
            plog(f"  Q4 results: {len(q4_findings)} findings")
            break
        except Exception as e:
            plog(f"  json={use_json}: {str(e)[:120]}")
            if use_json:
                continue
            raise

    # ── Compare ──
    print(f'\n{"="*70}')
    print(f'{"Metric":<30} {"Q3_K_M":<20} {"Q4_K_M":<20}')
    print(f'{"-"*30} {"-"*20} {"-"*20}')
    print(f'{"Findings count":<30} {len(q3_findings):<20} {len(q4_findings):<20}')

    # Severity comparison
    q3_sev = {}
    q4_sev = {}
    for f in q3_findings:
        s = f.get('severity', 'unknown')
        q3_sev[s] = q3_sev.get(s, 0) + 1
    for f in q4_findings:
        s = f.get('severity', 'unknown')
        q4_sev[s] = q4_sev.get(s, 0) + 1
    print(f'{"Severity (Q3)":<30} {str(q3_sev):<40}')
    print(f'{"Severity (Q4)":<30} {str(q4_sev):<40}')

    # Category comparison
    q3_cat = {}
    q4_cat = {}
    for f in q3_findings:
        c = f.get('category', 'unknown')
        q3_cat[c] = q3_cat.get(c, 0) + 1
    for f in q4_findings:
        c = f.get('category', 'unknown')
        q4_cat[c] = q4_cat.get(c, 0) + 1
    print(f'{"Categories (Q3)":<30} {str(len(q3_cat)):<40}')
    print(f'{"Categories (Q4)":<30} {str(len(q4_cat)):<40}')

    print(f'\nElapsed: {elapsed:.0f}s')
    print(f'JSON mode: {"yes" if use_json else "no (fallback)"}')

    # Save results
    result = {
        "baseline": {"model": "Qwen3-Coder-30B-A3B Q3_K_M", "file": "Qwen3-Coder-30B-A3B-Instruct-Q3_K_M.gguf",
                     "findings_count": len(q3_findings), "findings": q3_findings},
        "test": {"model": "Qwen3-Coder-30B-A3B Q4_K_M", "file": Q4_FILE,
                 "findings_count": len(q4_findings), "findings": q4_findings,
                 "elapsed_s": round(elapsed, 1), "usage": r.get("usage", {})},
        "pipeline_input": {"findings_count": len(input_findings), "severity": severity_dist},
    }
    out = f'{EXPER_DIR}/exp_30b_q4_comparison.json'
    with open(out, 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f'\nSaved: {out}')

except Exception as e:
    plog(f"❌ Error: {e}")

stop_model()
