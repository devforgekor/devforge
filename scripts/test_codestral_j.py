#!/usr/bin/env python3
"""Codestral-22B Q5_K_M J role test — normal + feedback, vs Qwen14B baseline.
Model: Codestral-22B-v0.1-Q5_K_M.gguf (15.7GB, Mistral arch, 32k context)

Usage: python3 test_codestral_j.py"""
import json, os, sys, time, subprocess, urllib.request
sys.path.insert(0, '/opt/projects/server/scripts')
from lib.llm_client import call_llm, MODEL_REGISTRY
from prj_cycle import kill_all, wait_health, wait_probe, SYS_J, RUBRIC, MODE_FILE_B, log as plog

EXPER_DIR = '/opt/projects/server/data/experiment'
PORT = 8080

MODEL_REGISTRY["Codestral"] = {"port": 8080, "temp": 0.10, "max_tokens": 4096, "timeout": 7200}

RUBRIC_APPEND = f'\n\n{RUBRIC}'
SYS_J_PROMPT = SYS_J + RUBRIC_APPEND

def load_data(suffix):
    p = json.load(open(f'{EXPER_DIR}/exp_p_r1_norubric{suffix}_r1_norubric{suffix}.json'))
    r = json.load(open(f'{EXPER_DIR}/exp_r_r1_norubric{suffix}_r1_norubric{suffix}.json'))
    return p['result'].get('findings', []), r['result'].get('verdicts', [])

p_findings, r_verdicts = load_data('')
fb_p_findings, fb_r_verdicts = load_data('_fb')

def start_model(mode):
    plog(f"  Starting {mode}...")
    with open(MODE_FILE_B, 'w') as f:
        f.write(f'MODE={mode}')
    kill_all()
    subprocess.run(['systemctl', '--user', 'start', 'container-devforge-swap.service'],
                   capture_output=True, timeout=60)
    ok = wait_health(PORT, timeout=900)  # 15.7GB load needs more time
    if ok:
        plog(f"  :{PORT} health OK")
        ok = wait_probe(PORT, mode, timeout=600)
    if ok:
        plog(f"  :{PORT} ready")
    else:
        plog(f"  ❌ Failed to start {mode}")
    return ok

def run_j(model_name, label, p_f, r_v):
    context = (
        f"=== P-R-J CYCLE ===\n"
        f"P_model=Proposer R_model=Reflector J_model={model_name}\n\n"
        f"=== P PROPOSED FINDINGS ({len(p_f)}) ===\n"
        + "\n".join(
            f"  {f.get('id','?')} [{f.get('severity','?')}/{f.get('category','?')}]: {f.get('description','')[:200]}"
            for f in p_f)
        + f"\n\n=== R VERDICTS ({len(r_v)}) ===\n"
        + "\n".join(
            f"  {v.get('id','?')}: {v.get('verdict','?')} — {v.get('reason','')[:150]}"
            for v in r_v)
    )
    t0 = time.monotonic()
    try:
        timeout_s = 3600  # 60min — 22B Q5_K_M on ARM ~1 tok/s
        r = call_llm(
            [{"role": "system", "content": SYS_J_PROMPT},
             {"role": "user", "content": context}],
            model=model_name, max_tokens=2048, temperature=0.1,
            timeout=timeout_s, json_mode=False, return_meta=True)
        content = r["content"].strip()
        # Strip markdown code fences if present
        if content.startswith("```"):
            first_nl = content.find("\n")
            if first_nl != -1:
                content = content[first_nl + 1:]
            if content.endswith("```"):
                content = content[:-3].rstrip()
        # Robust JSON extraction: find first { and last }
        brace_start = content.find("{")
        brace_end = content.rfind("}")
        if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
            content = content[brace_start:brace_end + 1]
        result = json.loads(content)
        elapsed = time.monotonic() - t0
        if "P_score" in result and "consensus_score" in result:
            plog(f"  {model_name} ({label}): P={result['P_score']} R={result.get('R_score')} consensus={result['consensus_score']}")
            return {"model": model_name, "label": label, "result": result,
                    "json_mode": False, "usage": r.get("usage", {}),
                    "elapsed_s": round(elapsed, 1)}
        plog(f"  missing fields: {content[:200]}")
    except Exception as e:
        plog(f"  ❌ {model_name} ({label}) failed: {str(e)[:120]}")
        return {"model": model_name, "label": label, "status": "FAILED", "error": str(e)[:200]}
    return {"model": model_name, "label": label, "status": "FAILED"}

# ── Main ──
results = []
model_file = "Codestral-22B-v0.1-Q5_K_M.gguf"
path = f'/opt/ai_data/models/gguf/{model_file}'
if not os.path.exists(path):
    plog(f'  ⚠️  File not found: {path}')
else:
    if start_model("j-test-codestral"):
        results.append(run_j("Codestral", "normal", p_findings, r_verdicts))
        results.append(run_j("Codestral", "feedback", fb_p_findings, fb_r_verdicts))
        kill_all()

# ── Load Qwen14B baseline ──
baseline_data = json.load(open(f'{EXPER_DIR}/exp_j_baseline.json'))
baseline_results = [{"model": "Qwen14B", **r} for r in baseline_data.get("Qwen14B", [])]
all_results = baseline_results + results

# ── Print ──
print(f'\n{"="*70}')
print('Codestral-22B J Role Test Results')
print(f'{"="*70}')
print(f'{"Model":<14} {"Scenario":<12} {"P(0-30)":<10} {"R(0-30)":<10} {"Consensus":<10} {"Decision":<14} {"Time(s)":<8}')
print(f'{"-"*14} {"-"*12} {"-"*10} {"-"*10} {"-"*10} {"-"*14} {"-"*8}')
for r in all_results:
    if r.get("status") in ("SKIPPED", "FAILED"):
        print(f'{r.get("model","?"):<14} {r.get("label","?"):<12} ❌{r.get("status","?"):<8} {r.get("error",""):<40}')
        continue
    res = r.get("result", {})
    print(f'{r.get("model","?"):<14} {r.get("label","?"):<12} '
          f'{res.get("P_score","?"):<10} {str(res.get("R_score","?")):<10} '
          f'{res.get("consensus_score","?"):<10} {str(res.get("decision","?")):<14} '
          f'{r.get("elapsed_s",0):<8.0f}')

# Save
out = f'{EXPER_DIR}/codestral_j_test.json'
with open(out, 'w') as f:
    json.dump({"model": "Codestral-22B Q5_K_M", "scenarios": ["normal", "feedback"],
               "baseline": "Qwen14B", "results": all_results}, f, indent=2, ensure_ascii=False)
print(f'\nSaved: {out}')
print(f'{"="*70}')
