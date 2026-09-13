#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_proposer_quantization.py — pytest
"""30B Q4_K_S vs Q3_K_M P role comparison test.

Starts 30B Q4_K_S, runs P role with same input as Q3_K_M baseline,
compares findings count/quality/timing.

Usage: python3 test_proposer_quantization.py"""
import json
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
PORT = 8080

MODEL_REGISTRY["Qwen30B_Q4KS"] = {"port": 8080, "temp": 0.10, "max_tokens": 4096, "timeout": 7200}

RUBRIC_APPEND = f'\n\n{RUBRIC}'
PROPOSER_PROMPT = PROPOSER_SYSTEM_PROMPT + RUBRIC_APPEND

def build_context():
    """Build same P role context as prj_cycle.run_propose_review_judge()."""
    state = json.load(open(f'{EXPER_DIR}/pipeline_state_r1_norubric.json'))
    inp = state.get('input', {})
    pv = state.get('python_verify', {})
    tv = state.get('30b_verify', {})

    lines = ["=== INPUT FINDINGS ==="]
    for f in inp.get('findings', []):
        lines.append(f"  [{f['severity']}] {f['category']}: {f['description']}")

    lines.append(f"\n=== PYTHON VERIFY ({pv.get('total_findings',0)} findings) ===")
    for issue in pv.get('issues', []):
        lines.append(f"  [{issue.get('severity','?')}] {issue.get('description','')[:150]}")

    lines.append("\n=== 30B VERIFY ===")
    lines.append(f"  Verdict: {tv.get('final_verdict','?')}")
    lines.append(f"  Confidence: {tv.get('confidence','?')}")
    lines.append(f"  Action: {tv.get('action','?')}")
    lines.append(f"  Summary: {tv.get('summary','')[:300]}")

    return "\n".join(lines)


def start_model():
    plog("  Starting review-p-q4ks...")
    with open(MODE_FILE_B, 'w') as f:
        f.write('MODE=review-p-q4ks')
    kill_all()
    subprocess.run(['systemctl', '--user', 'start', 'container-devforge-swap.service'],
                   capture_output=True, timeout=60)
    ok = wait_health(PORT, timeout=1200)
    if ok:
        plog(f"  :{PORT} health OK")
        ok = wait_probe(PORT, "review-p-q4ks", timeout=600)
    if ok:
        plog(f"  :{PORT} ready")
    else:
        plog("  Failed to start review-p-q4ks")
    return ok


def run_p():
    t0 = time.monotonic()
    context = build_context()
    plog(f"  Context length: {len(context)} chars")

    try:
        r = call_llm(
            [{"role": "system", "content": PROPOSER_PROMPT},
             {"role": "user", "content": context}],
            model="Qwen30B_Q4KS", max_tokens=4096, temperature=0.1,
            timeout=7200, json_mode=False, return_meta=True)

        content = r["content"].strip()
        if content.startswith("```"):
            first_nl = content.find("\n")
            if first_nl != -1:
                content = content[first_nl + 1:]
            if content.endswith("```"):
                content = content[:-3].rstrip()
        brace_start = content.find("{")
        brace_end = content.rfind("}")
        if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
            content = content[brace_start:brace_end + 1]
        result = json.loads(content)
        elapsed = time.monotonic() - t0

        findings = result.get("findings", [])
        timing = r.get("timings", {})
        usage = r.get("usage", {})

        plog(f"  Q4_K_S P: {len(findings)} findings in {elapsed:.0f}s "
             f"(pred={timing.get('predicted_per_second',0):.1f} t/s)")

        return {
            "findings": findings,
            "rubric_evaluation": result.get("rubric_evaluation", {}),
            "elapsed_s": round(elapsed, 1),
            "timings": timing,
            "usage": usage,
        }
    except Exception as e:
        plog(f"  Failed: {str(e)[:200]}")
        return {"error": str(e)[:200]}


# ── Main ──
if start_model():
    q4ks = run_p()
    kill_all()

    # Load Q3_K_M baseline
    baseline = json.load(open(f'{EXPER_DIR}/exp_p_r1_norubric_r1_norubric.json'))
    b_findings = baseline.get("result", {}).get("findings", [])
    b_timing = baseline.get("timings", {})
    b_elapsed = baseline.get("elapsed_ms", 0) / 1000

    # Print comparison
    print(f'\n{"="*70}')
    print('30B P Role Comparison: Q4_K_S vs Q3_K_M')
    print(f'{"="*70}')

    if "error" in q4ks:
        print(f'\n  Q4_K_S: ERROR — {q4ks["error"]}')
    else:
        q_findings = q4ks["findings"]
        print('\n--- Q3_K_M (baseline) ---')
        print(f'  Findings: {len(b_findings)}')
        print(f'  Time: {b_elapsed:.0f}s ({b_timing.get("predicted_per_second",0):.1f} t/s)')
        print(f'  Prompt: {b_timing.get("prompt_n",0)} tokens → '
              f'{b_timing.get("prompt_ms",0)/1000:.0f}s ({b_timing.get("prompt_per_second",0):.1f} t/s)')

        print('\n--- Q4_K_S (test) ---')
        print(f'  Findings: {len(q_findings)}')
        print(f'  Time: {q4ks["elapsed_s"]:.0f}s ({q4ks["timings"].get("predicted_per_second",0):.1f} t/s)')
        print(f'  Prompt: {q4ks["timings"].get("prompt_n",0)} tokens → '
              f'{q4ks["timings"].get("prompt_ms",0)/1000:.0f}s ({q4ks["timings"].get("prompt_per_second",0):.1f} t/s)')

        # Compare severity distribution
        print('\n--- Severity Distribution ---')
        def sev_dist(fs):
            d = {}
            for f in fs:
                s = f.get("severity", "unknown")
                d[s] = d.get(s, 0) + 1
            return d
        print(f'  Q3_K_M: {sev_dist(b_findings)}')
        print(f'  Q4_K_S: {sev_dist(q_findings)}')

        # Compare findings
        b_ids = {f.get("id") for f in b_findings}
        q_ids = {f.get("id") for f in q_findings}
        common = b_ids & q_ids
        only_b = b_ids - q_ids
        only_q = q_ids - b_ids
        print('\n--- Overlap ---')
        print(f'  Common: {len(common)}')
        print(f'  Only in Q3_K_M: {len(only_b)} {sorted(only_b)[:5]}')
        print(f'  Only in Q4_K_S: {len(only_q)} {sorted(only_q)[:5]}')

        # Sample findings
        print('\n--- Q4_K_S Findings (first 3) ---')
        for f in q_findings[:3]:
            print(f'  {f.get("id")} [{f.get("severity")}/{f.get("category")}]: {f.get("description","")[:120]}')

    # Save
    out = f'{EXPER_DIR}/test_proposer_quantization.json'
    with open(out, 'w') as f:
        json.dump({
            "test": "30B P role Q4_K_S vs Q3_K_M",
            "q4ks": q4ks,
            "q3km_baseline": {
                "findings": b_findings,
                "timings": b_timing,
                "elapsed_s": b_elapsed,
            }
        }, f, indent=2, ensure_ascii=False)
    print(f'\nSaved: {out}')
else:
    print("Failed to start Q4_K_S model")
