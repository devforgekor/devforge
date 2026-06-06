#!/usr/bin/env python3
"""7B Q8_0 verify test — optimized prompt (2000 tok handoff only).

Tests: 7B as verifier with handoff-only context. Compares to existing 27B Q4_K_M result.
Usage: python3 test_7b_verify_opt.py"""
import json, os, sys, time, subprocess
sys.path.insert(0, '/opt/projects/server/scripts')
from lib.llm_client import call_llm, MODEL_REGISTRY
from prj_cycle import kill_all, wait_health, wait_probe, SYS_VERIFY, RUBRIC, MODE_FILE_B, log as plog

EXPER_DIR = '/opt/projects/server/data/experiment'
PORT = 8080

MODEL_REGISTRY["Qwen7B"] = {"port": 8080, "temp": 0.10, "max_tokens": 3072, "timeout": 1800}

SYS_V27_PROMPT = SYS_VERIFY + f'\n\n{RUBRIC}'

# Optimized context: handoff JSON only (~2000 tokens vs 6000)
context = json.dumps({
    "handoff_llm_r": json.load(open(f'{EXPER_DIR}/exp_handoff_llm_r1_norubric_r1_norubric.json')),
    "handoff_python": json.load(open(f'{EXPER_DIR}/exp_handoff_py_r1_norubric_r1_norubric.json')),
    "handoff_r": json.load(open(f'{EXPER_DIR}/exp_handoff_r_r1_norubric_r1_norubric.json')),
}, indent=2)

def start_model():
    plog("  Starting test-7b...")
    with open(MODE_FILE_B, 'w') as f:
        f.write('MODE=test-7b')
    kill_all()
    subprocess.run(['systemctl', '--user', 'start', 'container-devforge-swap.service'],
                   capture_output=True, timeout=60)
    ok = wait_health(PORT, timeout=300)
    if ok:
        plog(f"  :{PORT} health OK")
        ok = wait_probe(PORT, "test-7b", timeout=120)
    if ok:
        plog(f"  :{PORT} ready")
    else:
        plog(f"  Failed to start test-7b")
    return ok

def run_verify():
    t0 = time.monotonic()
    plog(f"  Context: {len(context)} chars")
    try:
        r = call_llm(
            [{"role": "system", "content": SYS_V27_PROMPT},
             {"role": "user", "content": context}],
            model="Qwen7B", max_tokens=3072, temperature=0.1,
            timeout=1800, json_mode=True, return_meta=True)
        result = json.loads(r["content"])
        elapsed = time.monotonic() - t0
        timing = r.get("timings", {})
        v = result.get("final_verdict","?")
        c = result.get("confidence","?")
        plog(f"  7B verify: verdict={v} confidence={c} ({elapsed:.0f}s, {timing.get('predicted_per_second',0):.1f} t/s)")
        return {"result": result, "elapsed_s": round(elapsed, 1), "timings": timing, "usage": r.get("usage",{})}
    except Exception as e:
        plog(f"  Failed: {str(e)[:200]}")
        return {"error": str(e)[:200]}

if start_model():
    res = run_verify()
    kill_all()

    # Load 27B baseline
    v27 = json.load(open(f'{EXPER_DIR}/exp_v27b_r1_norubric_r1_norubric.json'))
    v27r = v27.get("result", {})
    v27t = v27.get("timings", {})

    print(f'\n{"="*70}')
    print('7B Q8_0 Verify (optimized prompt) vs 27B Q4_K_M')
    print(f'{"="*70}')

    if "error" in res:
        print(f'\n  7B: ERROR — {res["error"]}')
    else:
        r = res["result"]
        print(f'\n--- 27B Q4_K_M (baseline, full 6000tok context) ---')
        print(f'  verdict={v27r.get("final_verdict","?")} confidence={v27r.get("confidence","?")}')
        print(f'  action={v27r.get("action","?")}')
        print(f'  time={v27.get("elapsed_ms",0)/1000:.0f}s '
              f'(prompt={v27t.get("prompt_per_second",0):.1f} t/s, '
              f'pred={v27t.get("predicted_per_second",0):.1f} t/s)')
        print(f'  prompt_tokens={v27t.get("prompt_n","?")} pred_tokens={v27t.get("predicted_n","?")}')

        print(f'\n--- 7B Q8_0 (optimized, 2000tok context) ---')
        print(f'  verdict={r.get("final_verdict","?")} confidence={r.get("confidence","?")}')
        print(f'  action={r.get("action","?")}')
        t = res["timings"]
        print(f'  time={res["elapsed_s"]}s '
              f'(prompt={t.get("prompt_per_second",0):.1f} t/s, '
              f'pred={t.get("predicted_per_second",0):.1f} t/s)')
        print(f'  prompt_tokens={t.get("prompt_n","?")} pred_tokens={t.get("predicted_n","?")}')

        # Compare verification items
        print(f'\n--- Verification Items ---')
        for item in r.get("verification_items", []):
            print(f'  [{item.get("result","?"):>5}] {item.get("check","")[:100]}')

        # Feedback scores
        print(f'\n--- Feedback ---')
        for k, fb in r.get("feedback", {}).items():
            print(f'  {k}: score={fb.get("score","?")}')

        # Speedup
        v27_time = v27.get("elapsed_ms", 0) / 1000
        print(f'\n--- Speed ---')
        print(f'  27B: {v27_time:.0f}s | 7B: {res["elapsed_s"]}s | '
              f'speedup: {v27_time/max(res["elapsed_s"],1):.1f}x')

    out = f'{EXPER_DIR}/test_7b_verify_opt.json'
    with open(out, 'w') as f:
        json.dump({"model": "7B Q8_0 opt", "compare": "27B Q4_K_M", "result": res, "v27b": v27},
                  f, indent=2, ensure_ascii=False)
    print(f'\nSaved: {out}')
else:
    print("Failed to start 7B")
