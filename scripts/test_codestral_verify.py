#!/usr/bin/env python3
"""Codestral-22B Q4_K_M verify role test — same data as 27B verify.
Compares verifier output: Codestral-22B vs existing 27B Q4_K_M result.

Usage: python3 test_codestral_verify.py"""
import json, os, sys, time, subprocess
sys.path.insert(0, '/opt/projects/server/scripts')
from lib.llm_client import call_llm, MODEL_REGISTRY
from prj_cycle import kill_all, wait_health, wait_probe, SYS_V27, RUBRIC, MODE_FILE_B, log as plog

EXPER_DIR = '/opt/projects/server/data/experiment'
PORT = 8080

MODEL_REGISTRY["Codestral"] = {"port": 8080, "temp": 0.10, "max_tokens": 4096, "timeout": 7200}

RUBRIC_APPEND = f'\n\n{RUBRIC}'
SYS_V27_PROMPT = SYS_V27 + RUBRIC_APPEND

context = json.dumps({
    "handoff_llm_r": json.load(open(f'{EXPER_DIR}/exp_handoff_llm_r1_norubric_r1_norubric.json')),
    "handoff_python": json.load(open(f'{EXPER_DIR}/exp_handoff_py_r1_norubric_r1_norubric.json')),
    "handoff_r": json.load(open(f'{EXPER_DIR}/exp_handoff_r_r1_norubric_r1_norubric.json')),
}, indent=2)

RUBRIC_APPEND = f'\n\n{RUBRIC}'
SYS_V27_PROMPT = SYS_V27 + RUBRIC_APPEND

def start_model(mode):
    plog(f"  Starting {mode}...")
    with open(MODE_FILE_B, 'w') as f:
        f.write(f'MODE={mode}')
    kill_all()
    subprocess.run(['systemctl', '--user', 'start', 'container-devforge-swap.service'],
                   capture_output=True, timeout=60)
    ok = wait_health(PORT, timeout=900)
    if ok:
        plog(f"  :{PORT} health OK")
        ok = wait_probe(PORT, mode, timeout=600)
    if ok:
        plog(f"  :{PORT} ready")
    else:
        plog(f"  ❌ Failed to start {mode}")
    return ok

def run_verify():
    t0 = time.monotonic()
    try:
        timeout_s = 3600
        r = call_llm(
            [{"role": "system", "content": SYS_V27_PROMPT},
             {"role": "user", "content": context}],
            model="Codestral", max_tokens=3072, temperature=0.1,
            timeout=timeout_s, json_mode=False, return_meta=True)
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
        plog(f"  Codestral verify: verdict={result.get('final_verdict','?')} confidence={result.get('confidence','?')}")
        return {"result": result, "elapsed_s": round(elapsed, 1)}
    except Exception as e:
        plog(f"  ❌ Verify failed: {str(e)[:200]}")
        return {"error": str(e)[:200]}

# ── Main ──
if start_model("j-test-codestral"):
    result = run_verify()
    kill_all()

    # Print comparison
    v27 = json.load(open(f'{EXPER_DIR}/exp_v27b_r1_norubric_r1_norubric.json'))
    print(f'\n{"="*60}')
    print('Codestral-22B Q4_K_M vs 27B Q4_K_M — Verifier Comparison')
    print(f'{"="*60}')
    print(f'\n--- 27B Q4_K_M (existing) ---')
    print(f'  verdict={v27["result"]["final_verdict"]} confidence={v27["result"]["confidence"]}')
    print(f'  action={v27["result"]["action"]}')

    if "result" in result:
        r = result["result"]
        print(f'\n--- Codestral-22B Q4_K_M ---')
        print(f'  verdict={r.get("final_verdict","?")} confidence={r.get("confidence","?")}')
        print(f'  action={r.get("action","?")}')
        print(f'  time={result["elapsed_s"]}s')
        print(f'\n--- Feedback scores ---')
        for k, fb in r.get("feedback", {}).items():
            print(f'  {k}: score={fb.get("score","?")}')

    # Save
    out = f'{EXPER_DIR}/codestral_verify_test.json'
    with open(out, 'w') as f:
        json.dump({"model": "Codestral-22B Q4_K_M", "compare": "27B Q4_K_M",
                    "codestral": result, "v27b": v27}, f, indent=2, ensure_ascii=False)
    print(f'\nSaved: {out}')
else:
    print("❌ Failed to start")
