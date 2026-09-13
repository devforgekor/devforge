#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_verify_optimized_params.py — pytest
"""27B Q4_K_M verify — optimized handoff-only prompt (~2000 tok).

Tests: 27B as verifier with handoff-only context. Compares to old 27B run (6000tok full context).
Usage: python3 test_verify_optimized_params.py"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, '/opt/projects/server/scripts')
from lib.llm_client import MODEL_REGISTRY, call_llm
from pipelines.prj_cycle import MODE_FILE_B, RUBRIC, kill_all, wait_health, wait_probe
from pipelines.prj_cycle import VERIFIER_SYSTEM_PROMPT as PRJ_VERIFY
from pipelines.prj_cycle import log as plog

EXPER_DIR = '/opt/projects/server/data/experiment'
PORT = 8081

MODEL_REGISTRY["verifier"] = {"port": 8081, "temp": 0.10, "max_tokens": 3072, "timeout": 7200}

VERIFIER_SYSTEM_PROMPT = PRJ_VERIFY + f'\n\n{RUBRIC}'

# Optimized context: handoff JSON only (~2000 tokens vs 6000)
context = json.dumps({
    "handoff_llm_r": json.load(open(f'{EXPER_DIR}/exp_handoff_llm_r1_norubric_r1_norubric.json')),
    "handoff_python": json.load(open(f'{EXPER_DIR}/exp_handoff_py_r1_norubric_r1_norubric.json')),
    "handoff_r": json.load(open(f'{EXPER_DIR}/exp_handoff_r_r1_norubric_r1_norubric.json')),
}, indent=2)

def start_model():
    plog("  Starting verify mode (27B Q4_K_M on :8081)...")
    with open(MODE_FILE_B, 'w') as f:
        f.write('MODE=verify')
    kill_all()
    subprocess.run(['systemctl', '--user', 'start', 'container-devforge-swap.service'],
                   capture_output=True, timeout=60)
    ok = wait_health(PORT, timeout=1200)
    if ok:
        plog(f"  :{PORT} health OK")
        ok = wait_probe(PORT, "verify", timeout=600)
    if ok:
        plog(f"  :{PORT} ready")
    else:
        plog("  Failed to start verify mode")
    return ok

def run_verify():
    t0 = time.monotonic()
    plog(f"  Context: {len(context)} chars")
    try:
        r = call_llm(
            [{"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
             {"role": "user", "content": context}],
            model="verifier", max_tokens=3072, temperature=0.1,
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
        timing = r.get("timings", {})
        v = result.get("final_verdict","?")
        c = result.get("confidence","?")
        plog(f"  27B verify: verdict={v} confidence={c} ({elapsed:.0f}s, {timing.get('predicted_per_second',0):.1f} t/s)")
        return {"result": result, "elapsed_s": round(elapsed, 1), "timings": timing, "usage": r.get("usage",{})}
    except Exception as e:
        plog(f"  Failed: {str(e)[:200]}")
        return {"error": str(e)[:200]}

# ── Main ──
if start_model():
    res = run_verify()
    kill_all()

    # Load old 27B baseline (original 6000tok context)
    baseline_path = f'{EXPER_DIR}/exp_v27b_r1_norubric_r1_norubric.json'
    old = json.load(open(baseline_path)) if os.path.exists(baseline_path) else {}
    old_r = old.get("result", {})
    old_t = old.get("timings", {})

    print(f'\n{"="*70}')
    print('27B Q4_K_M Verify — Optimized Prompt vs Original')
    print(f'{"="*70}')

    if "error" in res:
        print(f'\n  Optimized: ERROR — {res["error"]}')
    else:
        r = res["result"]
        print('\n--- Original (6000tok full context) ---')
        if old:
            print(f'  verdict={old_r.get("final_verdict","?")} confidence={old_r.get("confidence","?")}')
            print(f'  action={old_r.get("action","?")}')
            print(f'  time={old.get("elapsed_ms",0)/1000:.0f}s '
                  f'(prompt={old_t.get("prompt_per_second",0):.1f} t/s, '
                  f'pred={old_t.get("predicted_per_second",0):.1f} t/s)')
            print(f'  prompt_tokens={old_t.get("prompt_n","?")} pred_tokens={old_t.get("predicted_n","?")}')

        print('\n--- Optimized (2000tok handoff only) ---')
        print(f'  verdict={r.get("final_verdict","?")} confidence={r.get("confidence","?")}')
        print(f'  action={r.get("action","?")}')
        t = res["timings"]
        print(f'  time={res["elapsed_s"]}s '
              f'(prompt={t.get("prompt_per_second",0):.1f} t/s, '
              f'pred={t.get("predicted_per_second",0):.1f} t/s)')
        print(f'  prompt_tokens={t.get("prompt_n","?")} pred_tokens={t.get("predicted_n","?")}')

        print('\n--- Verification Items ---')
        for item in r.get("verification_items", []):
            print(f'  [{item.get("result","?"):>5}] {item.get("check","")[:120]}')

        print('\n--- Feedback ---')
        for k, fb in r.get("feedback", {}).items():
            print(f'  {k}: score={fb.get("score","?")} | {", ".join(fb.get("improvements",[]) or [])}')

    out = f'{EXPER_DIR}/test_verify_optimized_params.json'
    with open(out, 'w') as f:
        json.dump({"test": "27B verify optimized", "result": res, "baseline": old}, f, indent=2, ensure_ascii=False)
    print(f'\nSaved: {out}')
    print(f'{"="*70}')
else:
    print("Failed to start 27B")
